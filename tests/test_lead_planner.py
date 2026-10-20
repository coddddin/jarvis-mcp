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


class MemoryLedger:
    def __init__(self):
        self.rows = {}

    def keys(self):
        return set(self.rows)

    def count_since(self, start):
        return sum(1 for _, at, _ in self.rows.values() if at >= start)

    def add(self, key, action, at, ref=''):
        self.rows[key] = (action, at, ref)


class judge_returning:
    """Fake Ollama judgment: records payloads, returns a fixed answer."""

    def __init__(self, **answer):
        self.answer, self.calls = {'index': 0, 'capability': 'coding', 'tier': 'standard', **answer}, []

    def __call__(self, payload):
        self.calls.append(payload)
        return self.answer


def run(board=None, ledger=None, judge=None, **kw):
    board = board if board is not None else FakeBoard()
    return plan_next(board, ROSTER, PLAN, judge=judge or judge_returning(), ledger=ledger or MemoryLedger(),
                     now=NOW, **kw), board


def test_candidates_are_unchecked_items_deduplicated_by_normalized_text():
    items = plan_candidates(PLAN)
    assert [c.text for c in items] == ['HUD 상태 표시 개선', '직원별 출입 토큰 발급', '하루 요약 보고서 초안']
    assert items[0].section == '남은 과제' and items[0].key == item_key('HUD `상태` 표시   개선')


def test_idle_board_creates_one_tagged_task_and_assigns_it():
    result, board = run(usage_sample=FRESH)
    assert result['action'] == 'task_created' and result['employee'] == 'codex'
    task = board.tasks[0]
    key = item_key('HUD 상태 표시 개선')
    assert task['title'].startswith(f'[PLAN:{key}] ') and f'plan_item_hash: {key}' in task['detail']
    [message] = board.sent
    assert message['to'] == 'codex' and message['task_id'] == task['id'] and '한국어' in message['body']


def test_busy_board_does_nothing_and_never_asks_the_model():
    judge = judge_returning()
    result, board = run(board=FakeBoard([{'id': 'x', 'title': 't', 'status': 'claimed'}]), judge=judge)
    assert result['reason'] == 'board_busy' and not judge.calls and not board.sent


def test_same_plan_item_is_not_created_twice():
    key = item_key('HUD 상태 표시 개선')
    board = FakeBoard([{'id': 'x', 'title': f'[PLAN:{key}] old', 'status': 'done'}])
    judge = judge_returning()
    run(board=board, judge=judge)
    assert [c['text'] for c in judge.calls[0]['candidates']][0] == '직원별 출입 토큰 발급'
    ledger = MemoryLedger()
    ledger.add(item_key('직원별 출입 토큰 발급'), 'approval_requested', NOW - 10)
    judge = judge_returning()
    run(board=board, ledger=ledger, judge=judge)
    assert [c['text'] for c in judge.calls[0]['candidates']] == ['하루 요약 보고서 초안']


def test_daily_cap_counts_tasks_and_approval_requests():
    ledger = MemoryLedger()
    for i in range(3):
        ledger.add(f'{i:012x}', 'task_created', NOW - 60)
    result, board = run(ledger=ledger, daily_cap=3)
    assert result['reason'] == 'daily_cap' and not board.tasks
    ledger.rows = {k: (a, NOW - 3 * 86400, r) for k, (a, _, r) in ledger.rows.items()}
    assert run(ledger=ledger, daily_cap=3)[0]['action'] == 'task_created'


@pytest.mark.parametrize('answer', [
    {'index': 1, 'tier': 'standard'},      # text mentions 토큰: forced sensitive whatever the model says
    {'index': 0, 'tier': 'sensitive'},     # the model may raise the tier
])
def test_sensitive_work_is_only_an_approval_request(answer):
    result, board = run(judge=judge_returning(**answer))
    assert result['action'] == 'approval_requested' and not board.tasks
    [message] = board.sent
    assert message['to'] == 'user:heejun' and message['body'].startswith('[검토]')


@pytest.mark.parametrize('raw', [None, 'text', {'index': 9, 'capability': 'coding', 'tier': 'light'},
                                 {'index': True, 'capability': 'coding', 'tier': 'light'},
                                 {'index': 0, 'capability': 'deploy', 'tier': 'light'},
                                 {'index': 0, 'capability': 'coding', 'tier': 'root'}])
def test_invalid_model_output_means_no_action(raw):
    result, board = run(judge=lambda payload: raw)
    assert result['reason'] == 'judge_invalid' and not board.tasks and not board.sent


def test_model_failure_means_no_action():
    def broken(payload):
        raise TimeoutError('ollama')
    result, board = run(judge=broken)
    assert result == {'action': 'none', 'reason': 'judge_error', 'error': 'TimeoutError'} and not board.sent


def test_unsafe_paths_from_the_model_are_dropped():
    candidates = plan_candidates(PLAN)
    def paths_of(paths):
        judged = validate_judgment({'index': 0, 'capability': 'coding', 'tier': 'light', 'paths': paths}, candidates)
        assert judged is not None
        return judged[4]
    for paths in (['../x'], ['C:/x'], ['/etc'], ['a\\b'], 'a.py'):
        assert paths_of(paths) == []
    assert paths_of(['jarvis/a.py']) == ['jarvis/a.py']


def test_roster_eligibility_respects_status_capability_and_tier():
    assert eligible_employees(ROSTER, 'coding', 'standard') == ['codex', 'claude-code']
    assert eligible_employees(ROSTER, 'review', 'standard') == ['codex', 'claude-code', 'cursor']
    assert 'opencode' not in eligible_employees(ROSTER, 'review', 'light')  # candidate, not employed
    assert eligible_employees(ROSTER, 'review', 'sensitive') == ['codex', 'claude-code']


def test_workload_gate_matches_dispatch_service():
    names = ['codex', 'claude-code']
    assert choose_employee(names, FRESH, NOW) == ('codex', 'normal')
    assert choose_employee(names, LOW, NOW) == ('claude-code', 'delegate')
    assert choose_employee(names, EXHAUSTED, NOW) == ('claude-code', 'exhausted')
    assert choose_employee(['codex'], EXHAUSTED, NOW) == (None, 'exhausted')
    # Missing or stale samples never shut Codex out (same as codex_workload_gate).
    assert choose_employee(names, None, NOW) == ('codex', 'no_usage_sample')
    assert choose_employee(names, {**EXHAUSTED, 'sampled_at': NOW - 3600}, NOW) == ('codex', 'stale_usage_sample')
    result, _ = run()
    assert result['employee'] == 'codex' and result['workload'] == 'no_usage_sample'


@pytest.mark.parametrize('capability,paths,tier', [
    ('summary', ['docs/PLAN.md'], 'light'),           # docs-only backup wording stays allowed
    ('summary', [], 'sensitive'),                       # no paths: may change anything
    ('summary', ['docs/PLAN.md', 'scripts/backup/run.ps1'], 'sensitive'),
    ('coding', ['docs/guides/backup.md'], 'sensitive'),
])
def test_backup_restore_words_block_only_code_or_config_changes(capability, paths, tier):
    candidates = plan_candidates('- [ ] 백업 안내 문서 정리\n- [ ] 토큰 발급 설명 문서\n')
    judged = validate_judgment({'index': 0, 'capability': capability, 'tier': 'light', 'paths': paths}, candidates)
    assert judged is not None and judged[2] == tier
    strong = validate_judgment({'index': 1, 'capability': 'summary', 'tier': 'light', 'paths': ['docs/a.md']},
                               candidates)
    assert strong is not None and strong[2] == 'sensitive'


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
    assert ledger.keys() == {first['plan'], second['plan']} and ledger.count_since(NOW - 1) == 2
