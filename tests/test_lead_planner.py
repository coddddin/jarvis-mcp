"""W5 local lead: idle board -> one PLAN item -> one task + assignment, or an approval request when sensitive."""
import pytest

from jarvis.agents.lead_planner import (LeadLedger, choose_employee, eligible_employees, item_key, plan_candidates,
                                        plan_next, validate_judgment)
from jarvis.core.guardrails import Guardrails
from jarvis.core.roster import load_roster
from jarvis.features.board.service import BoardStore

NOW = 1_800_000_000.0
PLAN = """# PLAN
## 남은 과제
- [x] 끝난 일
- [ ] **HUD 상태 표시** 개선
- [ ] 직원별 출입 토큰 발급
### W5
- [ ] HUD 상태 표시 개선
- [ ] 하루 요약 보고서 초안
"""
ROSTER = load_roster()
FRESH = {'usage': {'primary': {'usedPercent': 10, 'resetsAt': NOW + 3600}}, 'sampled_at': NOW}
LOW = {'usage': {'primary': {'usedPercent': 70, 'resetsAt': NOW + 3600}}, 'sampled_at': NOW}
EXHAUSTED = {'usage': {'primary': {'usedPercent': 100, 'resetsAt': NOW + 3600}}, 'sampled_at': NOW}


class FakeBoard:
    def __init__(self, tasks=()):
        self.tasks = [dict(t) for t in tasks]
        self.sent = []

    def list(self, *, status='', limit=50, agent=''):
        return {'tasks': [t for t in self.tasks if not status or t['status'] == status][:limit]}

    def create(self, agent, title, detail, paths):
        task = {'id': f'{len(self.tasks):032x}', 'title': title, 'detail': detail, 'paths': paths,
                'status': 'open', 'creator': agent}
        self.tasks.append(task)
        return task

    def send(self, agent, to, body, *, task_id=''):
        self.sent.append({'from': agent, 'to': to, 'body': body, 'task_id': task_id})
        return {'id': len(self.sent)}


class judge_returning:
    """Fake Ollama judgment: records payloads, returns a fixed answer."""

    def __init__(self, **answer):
        self.answer, self.calls = {'index': 0, 'capability': 'coding', 'tier': 'standard', **answer}, []

    def __call__(self, payload):
        self.calls.append(payload)
        return self.answer


@pytest.fixture
def ledger(tmp_path):
    return LeadLedger(Guardrails(tmp_path))


def record(ledger, key, at, action='task_created'):
    slot, refusal = ledger.reserve(at, 0, 1000)
    assert refusal is None
    ledger.commit(slot, key, action, 'x')


def run(ledger, board=None, judge=None, plan=PLAN, now=NOW, **kw):
    board = board if board is not None else FakeBoard()
    return plan_next(board, ROSTER, plan, judge=judge or judge_returning(), ledger=ledger, now=now, **kw), board


def judged(text, capability='summary', paths=(), title='', section_line=''):
    candidates = plan_candidates(f'{section_line}\n- [ ] {text}\n')
    result = validate_judgment({'index': 0, 'capability': capability, 'tier': 'light', 'paths': list(paths),
                                'title': title}, candidates)
    assert result is not None
    return result


# --- candidates and keys -------------------------------------------------------------------------------

def test_candidates_are_unchecked_items_deduplicated_by_normalized_text():
    items = plan_candidates(PLAN)
    assert [c.text for c in items] == ['HUD 상태 표시 개선', '직원별 출입 토큰 발급', '하루 요약 보고서 초안']
    assert items[0].section == '남은 과제' and items[0].key == item_key('HUD `상태` 표시   개선')


def test_stable_id_keeps_the_key_when_the_item_is_reworded():
    first = plan_candidates('## A\n- [ ] 공통 어댑터 <!-- plan:w4-adapter -->\n')[0]
    reworded = plan_candidates('## B\n- [ ] 공통 작업자 어댑터 정리 <!-- plan:w4-adapter -->\n')[0]
    assert first.key == reworded.key and first.text == '공통 어댑터'
    assert item_key('공통 어댑터') != first.key


# --- one tick ------------------------------------------------------------------------------------------

def test_idle_board_creates_one_tagged_task_and_assigns_it(ledger):
    result, board = run(ledger, usage_sample=FRESH)
    assert result['action'] == 'task_created' and result['employee'] == 'codex'
    task = board.tasks[0]
    key = item_key('HUD 상태 표시 개선')
    assert task['title'].startswith(f'[PLAN:{key}] ') and f'plan_item_hash: {key}' in task['detail']
    [message] = board.sent
    assert message['to'] == 'codex' and message['task_id'] == task['id'] and '한국어' in message['body']
    assert ledger.keys() == {key}


def test_busy_board_does_nothing_and_never_asks_the_model(ledger):
    judge = judge_returning()
    result, board = run(ledger, board=FakeBoard([{'id': 'x', 'title': 't', 'status': 'claimed'}]), judge=judge)
    assert result['reason'] == 'board_busy' and not judge.calls and not board.sent


def test_same_plan_item_is_not_created_twice(ledger):
    key = item_key('HUD 상태 표시 개선')
    board = FakeBoard([{'id': 'x', 'title': f'[PLAN:{key}] old', 'status': 'done'}])
    judge = judge_returning()
    run(ledger, board=board, judge=judge)
    assert [c['text'] for c in judge.calls[0]['candidates']][0] == '직원별 출입 토큰 발급'
    assert ledger.keys() == {item_key('직원별 출입 토큰 발급')}  # approval request recorded
    record(ledger, item_key('직원별 출입 토큰 발급'), NOW - 5)  # duplicate commit is a no-op
    judge = judge_returning()
    run(ledger, board=board, judge=judge)
    assert [c['text'] for c in judge.calls[0]['candidates']] == ['하루 요약 보고서 초안']


def test_daily_cap_counts_tasks_and_approval_requests(ledger):
    for i in range(3):
        record(ledger, f'{i:012x}', NOW - 60)
    result, board = run(ledger, daily_cap=3)
    assert result['reason'] == 'daily_cap' and not board.tasks
    assert run(ledger, daily_cap=3, now=NOW + 2 * 86400)[0]['action'] == 'task_created'


# --- concurrency (review finding 1) --------------------------------------------------------------------

def test_concurrent_tick_is_refused_while_one_is_in_flight(ledger):
    board, inner = FakeBoard(), {}

    def judge_that_races(payload):
        # A second tick starts while the first is waiting on the model.
        inner['result'] = plan_next(board, ROSTER, PLAN, judge=judge_returning(), ledger=ledger, now=NOW)
        return {'index': 0, 'capability': 'coding', 'tier': 'standard'}

    outer, _ = run(ledger, board=board, judge=judge_that_races)
    assert inner['result'] == {'action': 'none', 'reason': 'tick_in_progress'}
    assert outer['action'] == 'task_created' and len(board.tasks) == 1 and len(ledger.keys()) == 1


def test_cap_holds_under_racing_ticks(ledger):
    record(ledger, 'a' * 12, NOW - 60)
    board, inner = FakeBoard(), {}

    def judge_that_races(payload):
        inner['result'] = plan_next(board, ROSTER, PLAN, judge=judge_returning(), ledger=ledger, now=NOW,
                                    daily_cap=2)
        return {'index': 0, 'capability': 'coding', 'tier': 'standard'}

    run(ledger, board=board, judge=judge_that_races, daily_cap=2)
    assert inner['result']['reason'] == 'tick_in_progress' and len(board.tasks) == 1
    assert run(ledger, daily_cap=2, now=NOW + 1)[0]['reason'] == 'daily_cap'


@pytest.mark.parametrize('judge', [judge_returning(index=99), judge_returning(capability='deploy')])
def test_no_action_releases_the_reservation(ledger, judge):
    assert run(ledger, judge=judge)[0]['reason'] == 'judge_invalid'
    assert run(ledger)[0]['action'] == 'task_created'


def test_crashed_tick_reservation_expires(ledger):
    slot, _ = ledger.reserve(NOW, 0, 10)
    assert slot is not None
    assert run(ledger, now=NOW + 60)[0]['reason'] == 'tick_in_progress'
    assert run(ledger, now=NOW + 601)[0]['action'] == 'task_created'


# --- sensitivity (review finding 2) --------------------------------------------------------------------

@pytest.mark.parametrize('text', [
    'main에 병합', '자격증명 정리', '비밀번호 변경', '외부로 전송', '오래된 항목 제거', 'log deletion', 'merging branches',
    'pushing results', 'API 키 교체', '설정 초기화', '결과 업로드', '직원별 출입 토큰 발급', 'restoring snapshots',
])
def test_sensitive_wording_forces_approval(text):
    assert judged(text)[2] == 'sensitive'


def test_section_title_and_paths_are_checked_too():
    plain = 'HUD 상태 표시 개선'
    assert judged(plain)[2] == 'light'
    assert judged(plain, section_line='## W0 백업·복원', paths=['scripts/backup/run.ps1'])[2] == 'sensitive'
    assert judged(plain, title='then merge to main')[2] == 'sensitive'
    for path in ('config/agents.json', '.env', './config/x.json', 'jarvis/core/guardrails.py', 'AGENTS.md'):
        assert judged(plain, paths=[path])[2] == 'sensitive', path


@pytest.mark.parametrize('capability,paths,tier', [
    ('summary', ['docs/PLAN.md'], 'light'),           # docs-only backup wording stays allowed
    ('summary', [], 'sensitive'),                       # no paths: may change anything
    ('summary', ['docs/PLAN.md', 'scripts/run.ps1'], 'sensitive'),
    ('coding', ['docs/guides/backup.md'], 'sensitive'),
])
def test_backup_restore_words_block_only_code_or_config_changes(capability, paths, tier):
    assert judged('백업 안내 문서 정리', capability, paths)[2] == tier


def test_sensitive_work_is_only_an_approval_request(ledger):
    result, board = run(ledger, judge=judge_returning(index=1, tier='standard'))
    assert result['action'] == 'approval_requested' and not board.tasks
    [message] = board.sent
    assert message['to'] == 'user:heejun' and message['body'].startswith('[검토]')


# --- model output --------------------------------------------------------------------------------------

@pytest.mark.parametrize('raw', [None, 'text', {'index': 9, 'capability': 'coding', 'tier': 'light'},
                                 {'index': True, 'capability': 'coding', 'tier': 'light'},
                                 {'index': 0, 'capability': 'deploy', 'tier': 'light'},
                                 {'index': 0, 'capability': 'coding', 'tier': 'root'}])
def test_invalid_model_output_means_no_action(ledger, raw):
    result, board = run(ledger, judge=lambda _: raw)
    assert result['reason'] == 'judge_invalid' and not board.tasks and not board.sent


def test_model_failure_means_no_action(ledger):
    def broken(_):
        raise TimeoutError('ollama')
    result, board = run(ledger, judge=broken)
    assert result == {'action': 'none', 'reason': 'judge_error', 'error': 'TimeoutError'} and not board.sent


def test_unsafe_paths_from_the_model_are_dropped():
    for paths in (['../x'], ['C:/x'], ['/etc'], ['a\\b']):
        assert judged('HUD 개선', paths=paths)[4] == []
    assert judged('HUD 개선', paths=['jarvis/a.py'])[4] == ['jarvis/a.py']


# --- roster and workload -------------------------------------------------------------------------------

def test_roster_eligibility_uses_roster_facts_only():
    assert eligible_employees(ROSTER, 'coding', 'standard') == ['codex', 'claude-code']
    # Probation (cursor) and candidates (opencode) are never auto-assigned, even for 'review'.
    assert eligible_employees(ROSTER, 'review', 'light') == ['codex', 'claude-code']


def test_workload_gate_matches_dispatch_service():
    names = ['codex', 'claude-code']
    assert choose_employee(names, FRESH, NOW) == ('codex', 'normal')
    assert choose_employee(names, LOW, NOW) == ('claude-code', 'delegate')
    assert choose_employee(names, EXHAUSTED, NOW) == ('claude-code', 'exhausted')
    assert choose_employee(['codex'], EXHAUSTED, NOW) == (None, 'exhausted')
    # Missing, stale or unparsed samples never shut Codex out (same as codex_workload_gate).
    assert choose_employee(names, None, NOW) == ('codex', 'no_usage_sample')
    assert choose_employee(names, {**EXHAUSTED, 'sampled_at': NOW - 3600}, NOW) == ('codex', 'stale_usage_sample')
    assert choose_employee(names, {'usage': {'primary': 'junk'}, 'sampled_at': NOW}, NOW) == (
        'codex', 'unparsed_usage_sample')


# --- real board ----------------------------------------------------------------------------------------

def test_real_board_and_ledger_round_trip(tmp_path):
    guard = Guardrails(tmp_path)
    board, ledger = BoardStore(guard, clock=lambda: NOW, roster=ROSTER), LeadLedger(guard)
    first = plan_next(board, ROSTER, PLAN, judge=judge_returning(paths=['jarvis/hud.py']), ledger=ledger,
                      now=NOW, usage_sample=FRESH)
    assert first['action'] == 'task_created'
    task = board.get(first['task_id'])
    assert task['creator'] == 'local:lead' and task['paths'] == ['jarvis/hud.py']
    assert board.pending_for_employee('codex') == 1
    # The new task keeps the board busy; once it is done the next item comes up, never the same one.
    assert plan_next(board, ROSTER, PLAN, judge=judge_returning(), ledger=ledger, now=NOW)['reason'] == 'board_busy'
    board.claim('codex:main', task['id'])
    board.release('codex:main', task['id'], 'done', 'ok', verification='pytest ok')
    second = plan_next(board, ROSTER, PLAN, judge=judge_returning(), ledger=ledger, now=NOW + 1)
    assert second['action'] == 'approval_requested' and second['plan'] != first['plan']
    assert ledger.keys() == {first['plan'], second['plan']}
