"""W5 dispatcher: who to wake, caps, exhaustion backoff, usage parsing and command shapes."""
import json
import time
from pathlib import Path

import pytest

from jarvis.agents.dispatch_policy import (DispatchLedger, DispatcherConfig, EmployeeState, build_command,
                                           detect_limit, due, next_backoff, parse_usage, wake_prompt)
from jarvis.core.guardrails import Guardrails

NOW = 1_800_000_000.0


def state(**changes):
    base = dict(employee='codex', enabled=True, roster_status='active', pending=1, runs_today=0,
                running=False, exhausted_until=None)
    return EmployeeState(**{**base, **changes})


def test_wakes_only_employees_with_addressed_work():
    decisions = due([state(), state(employee='cursor', roster_status='probation', pending=0)], NOW, daily_runs=10)
    assert [d.employee for d in decisions if d.wake] == ['codex']
    assert {d.employee: d.reason for d in decisions}['cursor'] == 'no_pending_work'


@pytest.mark.parametrize('change,reason', [
    ({'enabled': False}, 'disabled'), ({'roster_status': 'candidate'}, 'not_employed'),
    ({'roster_status': 'retired'}, 'not_employed'), ({'running': True}, 'already_running'),
    ({'runs_today': 10}, 'daily_cap'), ({'exhausted_until': NOW + 60}, 'exhausted'),
])
def test_skip_reasons(change, reason):
    decision = due([state(**change)], NOW, daily_runs=10)[0]
    assert not decision.wake and decision.reason == reason


def test_exhaustion_expires_but_is_never_assumed_reset_early():
    assert due([state(exhausted_until=NOW)], NOW, daily_runs=10)[0].wake
    assert next_backoff(None) == 3600 and next_backoff(3600) == 7200 and next_backoff(4 * 3600) == 6 * 3600


@pytest.mark.parametrize('text', ["You've hit your usage limit. Resets at 5am", 'Error 429: rate limit exceeded',
                                  'quota exhausted for this account', 'Claude AI usage limit reached|1792530000'])
def test_limit_detection(text):
    assert detect_limit(text)


def test_ordinary_failures_are_not_limits():
    assert not detect_limit('pytest: 2 failed, 509 passed')


def test_usage_parsing_handles_claude_json_and_jsonl_events():
    claude = json.dumps({'result': 'ok', 'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 100,
                                                   'cache_read_input_tokens': 50, 'output_tokens': 7}})
    assert parse_usage(claude) == (152, 7)
    jsonl = '\n'.join([json.dumps({'type': 'turn.started'}),
                       json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 900, 'cached_input_tokens': 300,
                                                                       'output_tokens': 40}}),
                       'not json'])
    assert parse_usage(jsonl) == (900, 40)
    assert parse_usage('plain text') == (None, None)


def test_prompt_names_the_session_branch_and_merge_rule():
    prompt = wake_prompt('codex', 'C:/p/python.exe')
    assert 'codex:auto' in prompt and 'auto/codex' in prompt
    assert 'board_inbox' in prompt and 'main' in prompt and 'user:heejun' in prompt and 'C:/p/python.exe -m pytest --basetemp=.pytest_cache/basetemp' in prompt
    assert '"' not in prompt and '"' not in wake_prompt('cursor')
    assert '수습' in wake_prompt('cursor') and '수습' not in prompt


def test_commands_are_lean_and_tier_appropriate(tmp_path):
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    worktree, mcp = tmp_path / 'wt', '{"mcpServers":{}}'
    claude = build_command('claude-code', config, worktree, 'P', mcp, executable='claude.exe', python='C:/p/python.exe')
    assert claude[:2] == ['claude.exe', '-p'] and '--strict-mcp-config' in claude and '--setting-sources' in claude
    assert '--dangerously-skip-permissions' not in claude and 'bypassPermissions' not in claude
    assert 'Bash(C:/p/python.exe -m pytest:*)' in claude and 'Bash(C:/p/python.exe:*)' not in claude
    codex = build_command('codex', config, worktree, 'P', mcp, executable='codex.exe')
    assert codex[codex.index('--sandbox') + 1] == 'workspace-write' and '--dangerously-bypass-approvals-and-sandbox' not in codex
    cursor = build_command('cursor', config, worktree, 'P', mcp, executable='agent.cmd')
    assert '--force' not in cursor and '--yolo' not in cursor   # probation: proposals only


def test_ledger_counts_runs_per_local_day_and_tracks_exhaustion(tmp_path):
    clock = [NOW]
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True)
    run = ledger.start('codex', pid=1234)
    assert ledger.running('codex') == run
    ledger.finish(run, status='ok', exit_code=0, input_tokens=100, output_tokens=5, note='')
    assert ledger.running('codex') is None and ledger.runs_today('codex') == 1
    second = ledger.start('codex', pid=1235)
    ledger.finish(second, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='limit')
    assert ledger.exhausted_until('codex') == pytest.approx(NOW + 3600)
    third = ledger.start('codex', pid=1236)
    ledger.finish(third, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='limit')
    assert ledger.exhausted_until('codex') == pytest.approx(NOW + 7200)
    fourth = ledger.start('codex', pid=1237)
    ledger.finish(fourth, status='ok', exit_code=0, input_tokens=1, output_tokens=1, note='')
    assert ledger.exhausted_until('codex') is None
    clock[0] += 24 * 3600
    assert ledger.runs_today('codex') == 0
    summary = ledger.summary()
    assert summary['codex']['total_input_tokens'] == 101


def test_ledger_finished_runs_counts_only_ended_runs_across_employees(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    assert ledger.finished_runs() == 0

    codex_run = ledger.start('codex', pid=1234)
    assert ledger.finished_runs() == 0
    ledger.finish(codex_run, status='ok', exit_code=0, input_tokens=1, output_tokens=1, note='')
    assert ledger.finished_runs() == 1

    cursor_run = ledger.start('cursor', pid=1235)
    ledger.start('claude-code', pid=1236)
    assert ledger.finished_runs() == 1
    ledger.finish(cursor_run, status='error', exit_code=1, input_tokens=None, output_tokens=None, note='')
    assert ledger.finished_runs() == 2


def test_ledger_treats_a_dead_running_pid_as_not_running(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), alive=lambda pid: False)
    ledger.start('cursor', pid=999999)
    assert ledger.running('cursor') is None


def test_codex_may_write_git_metadata_only_when_given(tmp_path):
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    argv = build_command('codex', config, tmp_path, 'P', '{}', executable='codex.exe', git_dir=tmp_path / '.git')
    assert argv[argv.index('--add-dir') + 1] == str(tmp_path / '.git') and argv[-1] == 'P'


def test_try_start_refuses_a_second_live_run_and_the_daily_cap(tmp_path):
    clock = [NOW]
    alive = {'value': True}
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: alive['value'])
    first = ledger.try_start('codex', daily_runs=2)
    ledger.set_pid(first, 4321)
    assert ledger.try_start('codex', daily_runs=2) is None        # one live run per employee
    alive['value'] = False
    second = ledger.try_start('codex', daily_runs=2)              # dead run is marked lost, slot reused
    assert second is not None
    ledger.finish(second, status='ok', exit_code=0, input_tokens=1, output_tokens=1, note='')
    assert ledger.try_start('codex', daily_runs=2) is None        # cap: lost + ok = 2 today
    assert ledger.try_start('cursor', daily_runs=2) is not None   # other employees unaffected


# --- OpenCode employee (free models, probation) ---

def test_opencode_command_is_standalone_free_model_and_cannot_edit(tmp_path):
    from jarvis.agents.dispatch_policy import build_env
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    argv = build_command('opencode', config, tmp_path, 'P', '{}', executable='opencode-cli.exe')
    assert argv[:3] == ['opencode-cli.exe', 'run', '--standalone']
    model = argv[argv.index('-m') + 1]
    assert model.startswith('opencode/') and model.endswith('-free') and argv[-1] == 'P'
    assert '--agent' not in argv            # a custom restricted agent triggered FreeTierError 403
    settings = json.loads(build_env('opencode', project_root=tmp_path, workspace=tmp_path / 'ws')['OPENCODE_CONFIG_CONTENT'])
    assert settings['permission']['edit'] == 'deny' and settings['permission']['webfetch'] == 'deny'
    assert settings['permission']['bash']['*'] == 'deny'
    assert settings['tools'] == {'jarvis_*': False, 'jarvis_board_*': True}   # privacy: board tools only
    assert settings['mcp']['jarvis']['environment']['JARVIS_WORKSPACE'] == str(tmp_path / 'ws')
    assert build_env('codex', project_root=tmp_path, workspace=tmp_path) == {}


def test_opencode_prompt_is_proposal_only_and_privacy_scoped():
    prompt = wake_prompt('opencode')
    assert 'opencode:auto' in prompt and '수습' in prompt and '개인정보' in prompt and '"' not in prompt


def test_free_tier_refusal_is_an_eligibility_backoff(tmp_path):
    from jarvis.agents.dispatch_policy import detect_eligibility
    assert detect_eligibility('{"name":"FreeTierError","message":"free tier can only be used from within OpenCode"}')
    assert not detect_eligibility('pytest: 1 failed')
    clock = [NOW]
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True)
    run = ledger.start('opencode', pid=1)
    ledger.finish(run, status='eligibility', exit_code=1, input_tokens=None, output_tokens=None, note='403')
    assert ledger.exhausted_until('opencode') == pytest.approx(NOW + 3600)


# --- Loop closure: lead planner wiring and the daily digest ---

def test_lead_runs_only_when_no_employee_has_work():
    from jarvis.agents.dispatch_policy import should_run_lead
    idle = [state(pending=0), state(employee='cursor', roster_status='probation', pending=0)]
    assert should_run_lead(idle, lead_enabled=True)
    assert not should_run_lead(idle, lead_enabled=False)
    assert not should_run_lead([state(pending=1)], lead_enabled=True)
    assert not should_run_lead([state(pending=0, running=True)], lead_enabled=True)


def test_an_employee_held_on_unchanged_input_does_not_stop_the_lead():
    # J2 H-02 review P2: unread input that will never wake anyone must not block planning forever.
    from jarvis.agents.dispatch_policy import should_run_lead
    held = state(pending=2, input_hold='unchanged_input')
    assert should_run_lead([held, state(employee='cursor', pending=0)], lead_enabled=True)
    assert not should_run_lead([state(pending=2, input_hold='unchanged_input', running=True)], lead_enabled=True)
    assert not should_run_lead([held, state(employee='cursor', pending=1)], lead_enabled=True)
    # retry_backoff ends by itself; planning meanwhile would only wake the failing employee early
    assert not should_run_lead([state(pending=2, input_hold='retry_backoff')], lead_enabled=True)


def test_digest_is_due_once_per_local_day_after_the_hour():
    import time as _time
    from jarvis.agents.dispatch_policy import digest_due
    base = _time.mktime((2026, 10, 21, 20, 59, 0, 0, 0, -1))
    assert not digest_due(base, hour=21, last_sent_day=None)
    assert digest_due(base + 120, hour=21, last_sent_day=None)
    assert not digest_due(base + 120, hour=21, last_sent_day='2026-10-21')
    assert digest_due(base + 24 * 3600 + 120, hour=21, last_sent_day='2026-10-21')


def test_digest_text_is_short_korean_and_complete():
    from jarvis.agents.dispatch_policy import build_digest
    text = build_digest(
        day='2026-10-21',
        done=[{'title': f'작업 {i}', 'owner': 'codex:main'} for i in range(14)],
        review=[{'title': '머지 대기 A', 'owner': 'claude-code:main'}],
        stale=[{'title': '멈춘 작업', 'owner': 'cursor:main'}],
        runs={'codex': {'ok': 3, 'limit': 1}, 'opencode': {'eligibility': 1}},
        exhausted={'codex': 1_800_000_000.0},
        pause={'paused': False})
    assert text.startswith('[알림] 자비스 하루 요약 2026-10-21')
    assert '완료 14건' in text and '외 4건' in text            # at most 10 titles listed
    assert '머지 대기 A' in text and '멈춘 작업' in text
    assert 'codex' in text and 'opencode' in text and len(text) <= 1000
    assert '"' not in text


def test_config_lead_and_digest_defaults():
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    assert config.lead.enabled and 1 <= config.lead.daily_tasks <= 10
    assert 0 <= config.digest_hour <= 23


def test_ledger_reports_runs_today_by_status_and_tracks_digest(tmp_path):
    clock = [NOW]
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True)
    for status in ('ok', 'ok', 'limit'):
        run = ledger.start('codex', pid=1)
        ledger.finish(run, status=status, exit_code=0, input_tokens=1, output_tokens=1, note='')
    assert ledger.runs_today_by_status() == {'codex': {'ok': 2, 'limit': 1}}
    assert ledger.last_digest_day() is None
    ledger.mark_digest('2026-10-21')
    assert ledger.last_digest_day() == '2026-10-21'


# --- W2 stage 2: owner liveness by pid_at, and pid-less rows that would block an employee forever ---

class _Proc:
    def __init__(self, created):
        self._created = created

    def create_time(self):
        return self._created


def _ledger(tmp_path, clock, created, *, alive=True):
    """Ledger whose liveness is fully injected: alive(pid) and the process creation time."""
    import psutil

    def lookup(pid):
        if created.get(pid) is None:
            raise psutil.NoSuchProcess(pid)
        return _Proc(created[pid])
    return DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: alive, lookup=lookup)


def test_pid_less_running_row_blocks_until_stale_then_is_closed_as_lost(tmp_path):
    clock = [NOW]
    ledger = _ledger(tmp_path, clock, {})
    first = ledger.try_start('codex', daily_runs=5, stale_after=1800)   # service died before set_pid
    clock[0] = NOW + 600                                                 # worktree setup may still be running
    assert ledger.running('codex', stale_after=1800) == first
    assert ledger.try_start('codex', daily_runs=5, stale_after=1800) is None
    clock[0] = NOW + 1801
    assert ledger.running('codex', stale_after=1800) is None
    second = ledger.try_start('codex', daily_runs=5, stale_after=1800)
    assert second is not None and second != first
    row = [r for r in ledger.summary()['recent'] if r['id'] == first][0]
    assert row['status'] == 'lost' and row['note'] == 'no_pid'


def test_without_stale_after_a_pid_less_row_keeps_blocking(tmp_path):
    clock = [NOW]
    ledger = _ledger(tmp_path, clock, {})
    first = ledger.try_start('codex', daily_runs=5)
    clock[0] = NOW + 10 ** 6
    assert ledger.running('codex') == first and ledger.try_start('codex', daily_runs=5) is None


def test_set_pid_records_when_and_a_later_created_pid_is_reused(tmp_path):
    clock = [NOW]
    created = {}
    ledger = _ledger(tmp_path, clock, created)
    run = ledger.try_start('codex', daily_runs=5, stale_after=1800)
    clock[0] = NOW + 30
    ledger.set_pid(run, 4321)
    created[4321] = NOW + 29                 # Popen precedes set_pid: created before pid_at
    assert ledger.running('codex', stale_after=1800) == run
    created[4321] = NOW + 30 + 6             # created after pid_at + 5: someone else's process now
    assert ledger.running('codex', stale_after=1800) is None
    assert [r for r in ledger.summary()['recent'] if r['id'] == run][0]['status'] == 'lost'


def test_legacy_row_without_pid_at_allows_setup_time(tmp_path):
    clock = [NOW]
    created = {77: NOW + 60}
    ledger = _ledger(tmp_path, clock, created)
    run = ledger.start('cursor', pid=77)     # old rows carry pid but no pid_at
    assert ledger.running('cursor') == run   # started + 120 tolerance
    created[77] = NOW + 121
    assert ledger.running('cursor') is None


def test_unreadable_creation_time_counts_as_alive(tmp_path):
    import psutil
    clock = [NOW]

    def denied(pid):
        raise psutil.AccessDenied(pid)
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True, lookup=denied)
    run = ledger.try_start('codex', daily_runs=5)
    ledger.set_pid(run, 5)
    assert ledger.running('codex') == run    # never start a second run on a doubt


def test_old_ledger_file_gains_pid_at(tmp_path):
    import sqlite3
    folder = tmp_path / '.jarvis-dispatch'
    folder.mkdir()
    db = sqlite3.connect(folder / 'ledger.sqlite3')
    db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT NOT NULL, pid INTEGER, '
               'started REAL NOT NULL, ended REAL, status TEXT NOT NULL, exit_code INTEGER, input_tokens INTEGER, '
               'output_tokens INTEGER, note TEXT NOT NULL DEFAULT "")')
    db.execute('INSERT INTO runs(employee, pid, started, status) VALUES ("codex", NULL, ?, "running")', (NOW,))
    db.commit()
    db.close()
    clock = [NOW + 5]
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True, lookup=lambda pid: _Proc(NOW))
    ledger.set_pid(1, 42)
    db = sqlite3.connect(folder / 'ledger.sqlite3')
    assert db.execute('SELECT pid, pid_at FROM runs WHERE id=1').fetchone() == (42, NOW + 5)
    db.close()


def test_concurrent_processes_migrate_the_ledger_once(tmp_path):
    import subprocess
    import sys
    import sqlite3
    folder = tmp_path / '.jarvis-dispatch'
    folder.mkdir()
    db = sqlite3.connect(folder / 'ledger.sqlite3')
    db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT NOT NULL, pid INTEGER, '
               'started REAL NOT NULL, ended REAL, status TEXT NOT NULL, exit_code INTEGER, input_tokens INTEGER, '
               'output_tokens INTEGER, note TEXT NOT NULL DEFAULT "")')
    db.commit()
    db.close()
    code = ('import sys, time\nfrom pathlib import Path\nfrom jarvis.agents.dispatch_policy import DispatchLedger\n'
            'from jarvis.core.guardrails import Guardrails\n'
            'ledger = DispatchLedger(Guardrails(Path(sys.argv[1])))\n'
            'deadline = float(sys.argv[2])\n'
            'while time.time() < deadline: time.sleep(0.005)\n'
            'for _ in range(20): ledger.summary()\n')
    start_at = str(__import__('time').time() + 1.5)
    workers = [subprocess.Popen([sys.executable, '-c', code, str(tmp_path), start_at], stderr=subprocess.PIPE, text=True)
               for _ in range(4)]
    errors = [w.communicate(timeout=60)[1] for w in workers]
    assert all(w.returncode == 0 for w in workers), errors
    db = sqlite3.connect(folder / 'ledger.sqlite3')
    columns = [row[1] for row in db.execute('PRAGMA table_info(runs)')]
    db.close()
    assert columns.count('pid_at') == 1


def test_unverifiable_owner_holds_only_until_stale_after(tmp_path):
    # Main review: a reused pid that belongs to a protected process cannot be inspected (access denied).
    # That doubt may hold the slot only within the run limit; afterwards the row is closed as lost.
    import psutil
    clock = [NOW]

    def denied(pid):
        raise psutil.AccessDenied(pid)
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True, lookup=denied)
    run = ledger.try_start('codex', daily_runs=5, stale_after=1800)
    ledger.set_pid(run, 4)
    clock[0] = NOW + 1800
    assert ledger.running('codex', stale_after=1800) == run
    assert ledger.try_start('codex', daily_runs=5, stale_after=1800) is None
    clock[0] = NOW + 10 * 3600
    assert ledger.running('codex', stale_after=1800) is None
    row = [r for r in ledger.summary()['recent'] if r['id'] == run][0]
    assert row['status'] == 'lost' and row['note'] == 'owner_unverifiable'
    assert ledger.try_start('codex', daily_runs=5, stale_after=1800) is not None


def test_digest_lists_employees_currently_blocked_by_the_adapter_check():
    from jarvis.agents.dispatch_policy import build_digest
    base = dict(day='2026-10-21', done=[], review=[], stale=[], runs={}, exhausted={}, pause={'paused': False})
    assert '점검' not in build_digest(**base)
    assert '점검' not in build_digest(**base, blocked=[])
    text = build_digest(**base, blocked=[('cursor', ['probation_can_edit'], 2),
                                         ('opencode', ['label_mismatch', 'probation_can_edit'], 0)])
    line = [l for l in text.splitlines() if '점검' in l][0]
    assert line == ('- 현재 점검으로 깨우지 않음: cursor(수습인데 파일 수정 가능, 받은 일 2건), '
                    'opencode(수정 불가 표시와 실제 명령 불일치·수습인데 파일 수정 가능)')
    assert '--' not in text and len(text) <= 1000


def test_digest_ignores_unknown_codes_and_stays_bounded():
    from jarvis.agents.dispatch_policy import build_digest
    many = [(f'worker{i}', ['probation_can_edit', 'mystery'], 99) for i in range(200)]
    text = build_digest(day='d', done=[], review=[], stale=[], runs={}, exhausted={}, pause={}, blocked=many)
    assert 'mystery' not in text and len(text) <= 1000


def test_send_digest_includes_blocked_employees(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from jarvis.agents import worker_adapters as wa
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import load_roster
    from jarvis.features.board.service import BoardStore
    guard = Guardrails(tmp_path)
    roster = load_roster()
    board = BoardStore(guard, roster=roster)
    board.send('user:heejun', 'cursor', 'review please')
    ledger = DispatchLedger(guard, alive=lambda pid: True)
    config = DispatcherConfig.model_validate({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'digest_hour': 0, 'employees': {'cursor': {}, 'claude-code': {}, 'opencode': {'enabled': False}}})
    original = wa.ADAPTERS['cursor'].work_command
    from dataclasses import replace
    monkeypatch.setitem(wa.ADAPTERS, 'cursor', replace(wa.ADAPTERS['cursor'], work_command=lambda r: original(r) + ['-f']))
    opencode = wa.ADAPTERS['opencode'].work_command
    monkeypatch.setitem(wa.ADAPTERS, 'opencode', replace(wa.ADAPTERS['opencode'], work_command=lambda r: opencode(r) + ['--yolo']))
    sent = []
    monkeypatch.setattr(board, 'send', lambda sender, to, body, **kw: sent.append(body))
    assert cli_dispatch.send_digest_if_due(guard, board, ledger, config, NOW, roster=roster)
    line = [l for l in sent[0].splitlines() if '점검' in l][0]
    assert line == '- 현재 점검으로 깨우지 않음: cursor(수습인데 파일 수정 가능, 받은 일 1건)'   # opencode disabled in config
    assert '-f' not in sent[0]


def test_digest_is_still_sent_when_the_blocked_check_fails(tmp_path, monkeypatch):
    # Main review: the blocked line is extra information; its failure must not hold back the digest.
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import load_roster
    from jarvis.features.board.service import BoardStore
    guard = Guardrails(tmp_path)
    roster = load_roster()
    board = BoardStore(guard, roster=roster)
    ledger = DispatchLedger(guard, alive=lambda pid: True)
    config = DispatcherConfig.model_validate({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'digest_hour': 0, 'employees': {'cursor': {}}})
    sent, logged = [], []
    monkeypatch.setattr(board, 'send', lambda sender, to, body, **kw: sent.append(body))
    monkeypatch.setattr(cli_dispatch, 'blocked_now', lambda *a: (_ for _ in ()).throw(RuntimeError('roster gone')))
    monkeypatch.setattr(cli_dispatch, 'log_service_error', logged.append)
    assert cli_dispatch.send_digest_if_due(guard, board, ledger, config, NOW, roster)
    assert sent and sent[0].startswith('[알림] 자비스 하루 요약') and '점검' not in sent[0]
    assert len(logged) == 1 and 'roster gone' in str(logged[0])
    assert ledger.last_digest_day() is not None          # marked sent: no retry every tick


def test_digest_labels_privacy_open():
    from jarvis.agents.dispatch_policy import build_digest
    text = build_digest(day='d', done=[], review=[], stale=[], runs={}, exhausted={}, pause={},
                        blocked=[('opencode', ['privacy_open'], 1)])
    assert '- 현재 점검으로 깨우지 않음: opencode(외부 읽기·웹 차단 안 됨, 받은 일 1건)' in text


def _digest_setup(tmp_path, monkeypatch, block):
    import json as _json
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import ROSTER_PATH, load_roster
    from jarvis.features.board.service import BoardStore
    data = _json.loads(ROSTER_PATH.read_text(encoding='utf-8'))
    data['promotion'] = block
    (tmp_path / 'agents.json').write_text(_json.dumps(data), encoding='utf-8')
    roster = load_roster(tmp_path / 'agents.json')
    (tmp_path / 'ws').mkdir()
    guard = Guardrails(tmp_path / 'ws')
    board = BoardStore(guard, roster=roster)
    task = board.create('cursor:auto', 'Review', '', ['a.py'])['id']
    board.claim('cursor:auto', task)
    board.release('cursor:auto', task, 'done', 'reviewed', verification='read diff')
    config = DispatcherConfig.model_validate({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'digest_hour': 0, 'employees': {'cursor': {}}})
    sent = []
    monkeypatch.setattr(board, 'send', lambda sender, to, body, **kw: sent.append(body))
    return cli_dispatch, guard, board, DispatchLedger(guard, alive=lambda pid: True), config, roster, sent


def test_send_digest_includes_promotion_candidates(tmp_path, monkeypatch):
    cli_dispatch, guard, board, ledger, config, roster, sent = _digest_setup(
        tmp_path, monkeypatch, {'min_exact_done': 1, 'blocking_problems': []})
    assert cli_dispatch.send_digest_if_due(guard, board, ledger, config, NOW, roster=roster)
    assert '- 승격 후보: cursor(done 1, 재작업 0)' in sent[0].splitlines()


def test_digest_is_still_sent_when_the_promotion_check_fails(tmp_path, monkeypatch):
    cli_dispatch, guard, board, ledger, config, roster, sent = _digest_setup(tmp_path, monkeypatch, {'min_exact_done': 1})
    logged = []
    monkeypatch.setattr(cli_dispatch, 'evaluate', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('board gone')))
    monkeypatch.setattr(cli_dispatch, 'log_service_error', logged.append)
    assert cli_dispatch.send_digest_if_due(guard, board, ledger, config, NOW, roster=roster)
    assert sent and '승격' not in sent[0] and 'board gone' in str(logged[-1])


# --- W5: per-employee daily token cap -----------------------------------------------------------------

def test_token_cap_blocks_waking_only_when_configured_and_reached():
    assert due([state(tokens_today=10**9)], NOW, daily_runs=10)[0].wake            # no cap configured
    assert due([state(tokens_today=4999, daily_tokens=5000)], NOW, daily_runs=10)[0].wake
    decision = due([state(tokens_today=5000, daily_tokens=5000)], NOW, daily_runs=10)[0]
    assert not decision.wake and decision.reason == 'token_cap'
    # The run cap is reported first when both apply; a running employee stays 'already_running'.
    assert due([state(runs_today=10, tokens_today=9000, daily_tokens=5000)], NOW, daily_runs=10)[0].reason == 'daily_cap'
    assert due([state(running=True, tokens_today=9000, daily_tokens=5000)], NOW, daily_runs=10)[0].reason == 'already_running'


def test_daily_tokens_setting_is_optional_and_bounded():
    base = {'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30}
    config = DispatcherConfig.model_validate({**base, 'employees': {'codex': {}, 'cursor': {'daily_tokens': 200000}}})
    assert config.employees['codex'].daily_tokens is None and config.employees['cursor'].daily_tokens == 200000
    for bad in (999, 0, '5000', 1.5):
        with pytest.raises(ValueError):
            DispatcherConfig.model_validate({**base, 'employees': {'codex': {'daily_tokens': bad}}})


def test_usage_detail_keeps_the_cache_share_apart():
    from jarvis.agents.dispatch_policy import parse_usage_detail
    claude = json.dumps({'usage': {'input_tokens': 12, 'cache_creation_input_tokens': 40,
                                   'cache_read_input_tokens': 100, 'output_tokens': 7}})
    assert parse_usage_detail(claude) == {'input': 152, 'cache': 140, 'output': 7, 'quality': 'measured'}
    assert parse_usage(claude) == (152, 7)                                        # unchanged meaning
    codex = '{"type":"x"}\n' + json.dumps({'usage': {'input_tokens': 900, 'output_tokens': 40}})
    assert parse_usage_detail(codex) == {'input': 900, 'cache': 0, 'output': 40, 'quality': 'measured'}
    assert parse_usage_detail('plain text') is None


def test_usage_detail_marks_missing_or_invalid_counts_instead_of_guessing():
    from jarvis.agents.dispatch_policy import parse_usage_detail

    def detail(usage):
        return parse_usage_detail(json.dumps({'type': 'result', 'usage': usage}))
    # bool and negative numbers are reporting errors, not counts
    assert detail({'input_tokens': True, 'output_tokens': -1}) == {
        'input': None, 'cache': 0, 'output': None, 'quality': 'unknown'}
    assert detail({'input_tokens': 1, 'output_tokens': -1}) == {
        'input': 1, 'cache': 0, 'output': None, 'quality': 'partial'}
    # a missing input is unknown, not 0, even when the output was reported
    assert detail({'output_tokens': 5}) == {'input': None, 'cache': 0, 'output': 5, 'quality': 'partial'}
    assert detail({'cache_read_input_tokens': 100, 'output_tokens': 5}) == {
        'input': None, 'cache': 100, 'output': 5, 'quality': 'partial'}
    # an invalid cache field makes the input total unknown too
    assert detail({'input_tokens': 3, 'cache_read_input_tokens': '9', 'output_tokens': 5}) == {
        'input': None, 'cache': None, 'output': 5, 'quality': 'partial'}
    assert parse_usage(json.dumps({'usage': {'output_tokens': 5}})) == (None, 5)


def test_usage_detail_reads_cursor_camel_case_result():
    from jarvis.agents.dispatch_policy import parse_usage_detail
    # Shape of a cursor-agent stream-json `result` event: inputTokens already excludes cache read/write.
    cursor = '\n'.join([json.dumps({'type': 'thinking', 'subtype': 'completed'}),
                        json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok',
                                    'usage': {'inputTokens': 20, 'outputTokens': 8, 'cacheReadTokens': 300,
                                              'cacheWriteTokens': 40}})])
    assert parse_usage_detail(cursor) == {'input': 360, 'cache': 340, 'output': 8, 'quality': 'measured'}
    assert parse_usage(cursor) == (360, 8)
    # mixed shapes are never summed together: a snake cache key switches the event to snake mode
    assert parse_usage_detail(json.dumps({'usage': {'inputTokens': 2, 'outputTokens': 1,
                                                    'cache_read_input_tokens': 50}}))['input'] is None


def test_usage_detail_review_cases():
    from jarvis.agents.dispatch_policy import parse_usage_detail

    def detail(usage):
        return parse_usage_detail(json.dumps({'type': 'result', 'usage': usage}))
    # Cursor writes 0 for fields it did not get: an all-zero result is not a measured 0
    assert detail({'inputTokens': 0, 'outputTokens': 0, 'cacheReadTokens': 0, 'cacheWriteTokens': 0}) == {
        'input': None, 'cache': 0, 'output': None, 'quality': 'unknown'}
    assert detail({'inputTokens': 0, 'outputTokens': 3, 'cacheReadTokens': 50, 'cacheWriteTokens': 0}) == {
        'input': 50, 'cache': 50, 'output': 3, 'quality': 'measured'}
    # a cache-only event keeps its cache count instead of being dropped
    assert detail({'cache_read_input_tokens': 100}) == {'input': None, 'cache': 100, 'output': None,
                                                        'quality': 'partial'}
    # mixed shapes: snake wins, camel values are ignored rather than mixed in
    assert detail({'input_tokens': 3, 'outputTokens': 4}) == {'input': 3, 'cache': 0, 'output': None,
                                                             'quality': 'partial'}
    # a later partial event does not replace an earlier measured one
    jsonl = '\n'.join(json.dumps({'usage': u}) for u in ({'input_tokens': 900, 'output_tokens': 40},
                                                         {'input_tokens': 5}))
    assert parse_usage_detail(jsonl) == {'input': 900, 'cache': 0, 'output': 40, 'quality': 'measured'}
    only_partial = '\n'.join(json.dumps({'usage': u}) for u in ({'output_tokens': 1}, {'input_tokens': 5}))
    assert parse_usage_detail(only_partial)['input'] == 5


def test_usage_detail_takes_the_last_reporting_event_without_summing():
    from jarvis.agents.dispatch_policy import parse_usage_detail
    jsonl = '\n'.join(json.dumps({'usage': {'input_tokens': n, 'output_tokens': n}}) for n in (5, 7))
    assert parse_usage_detail(jsonl)['input'] == 7


def test_tokens_today_sums_todays_runs_with_a_cache_breakdown(tmp_path):
    clock = [NOW]
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: True)
    clock[0] = NOW - 86400                                                         # yesterday: not counted
    ledger.finish(ledger.start('claude-code', pid=1), status='ok', exit_code=0, input_tokens=10**6,
                  output_tokens=10, cache_tokens=10**6 - 5, note='')
    clock[0] = NOW
    ledger.finish(ledger.start('claude-code', pid=1), status='ok', exit_code=0, input_tokens=1500,
                  output_tokens=200, cache_tokens=1400, note='')
    ledger.finish(ledger.start('claude-code', pid=1), status='ok', exit_code=0, input_tokens=300,
                  output_tokens=50, note='')                                       # older caller: cache unknown
    ledger.finish(ledger.start('claude-code', pid=1), status='error', exit_code=None, input_tokens=None,
                  output_tokens=None, note='')                                     # unreadable usage counts 0
    ledger.finish(ledger.start('codex', pid=1), status='ok', exit_code=0, input_tokens=9, output_tokens=9, note='')
    assert ledger.tokens_today('claude-code') == {'total': 2050, 'input': 1800, 'output': 250, 'cache': 1400,
                                                  'cache_unknown_runs': 1, 'partial_runs': 0,
                                                  'unknown_runs': 0}
    assert ledger.tokens_today('opencode') == {'total': 0, 'input': 0, 'output': 0, 'cache': 0,
                                               'cache_unknown_runs': 0, 'partial_runs': 0, 'unknown_runs': 0}


def test_older_ledger_gains_the_cache_column_in_place(tmp_path):
    import sqlite3
    (tmp_path / '.jarvis-dispatch').mkdir()
    db = sqlite3.connect(tmp_path / '.jarvis-dispatch' / 'ledger.sqlite3')
    with db:
        db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT NOT NULL, pid INTEGER, '
                   'started REAL NOT NULL, ended REAL, status TEXT NOT NULL, exit_code INTEGER, input_tokens INTEGER, '
                   'output_tokens INTEGER, note TEXT NOT NULL DEFAULT "")')
        db.execute("INSERT INTO runs(employee,started,status,input_tokens,output_tokens) VALUES('codex',?,'ok',100,5)",
                   (NOW,))
    db.close()
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    assert ledger.tokens_today('codex') == {'total': 105, 'input': 100, 'output': 5, 'cache': 0,
                                            'cache_unknown_runs': 1, 'partial_runs': 0, 'unknown_runs': 0}


def test_tick_skips_an_employee_over_its_token_cap(tmp_path, monkeypatch):
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import load_roster
    from jarvis.features.board.service import BoardStore
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'claude-code': {'daily_tokens': 1000}, 'codex': {}}, 'lead': {'enabled': False}}),
        encoding='utf-8')
    roster, guard = load_roster(), Guardrails(workspace)
    board = BoardStore(guard, roster=roster)
    board.send('user:heejun', 'claude-code', 'build please')
    board.send('user:heejun', 'codex', 'build please')
    ledger = DispatchLedger(guard, alive=lambda pid: True)
    ledger.finish(ledger.start('claude-code', pid=1), status='ok', exit_code=0, input_tokens=900,
                  output_tokens=100, cache_tokens=800, note='')
    launched = []
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: guard)
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda g: BoardStore(g, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'prepare_run', lambda employee, *a: launched.append(employee) or 'ok')
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'send_alerts', lambda *a, **k: 0, raising=False)
    monkeypatch.setattr(cli_dispatch, 'collect_codex', lambda g: None)
    dry = cli_dispatch.tick(dry_run=True)
    assert {d['employee']: d['reason'] for d in dry['decisions']}['claude-code'] == 'token_cap'
    cli_dispatch.tick()
    assert launched == ['codex']


# --- OpenCode model rotation: settings, ledger, status and digest line --------------------------------------

def _settings(**fields):
    from jarvis.agents.dispatch_policy import EmployeeSettings
    return EmployeeSettings.model_validate(fields)


def test_models_setting_is_ordered_bounded_and_exclusive_with_model():
    from pydantic import ValidationError
    assert _settings(models=['opencode/a-free', 'opencode/b-free']).models == ['opencode/a-free', 'opencode/b-free']
    for bad in ({'models': []}, {'models': [f'opencode/m{i}' for i in range(9)]},
                {'models': ['opencode/a', 'opencode/a']}, {'models': ['']}, {'models': ['bad model']},
                {'models': ['opencode/a'], 'model': 'opencode/b'}, {'models': 'opencode/a'}, {'models': ['-f']}):
        with pytest.raises(ValidationError):
            _settings(**bad)
    assert _settings(model='opencode/b').models is None   # single-model employees keep working as before
    with pytest.raises(ValidationError):
        _settings(model='-f')                             # it follows -m in argv, like models
    assert _settings(model='').model == ''


def test_ledger_backs_off_only_the_model_that_hit_a_limit(tmp_path):
    guard = Guardrails(tmp_path)
    from jarvis.agents.dispatch_policy import DispatchLedger
    now = [1_000_000.0]
    ledger = DispatchLedger(guard, clock=lambda: now[0], alive=lambda pid: False)
    models = ['m/a', 'm/b']
    def run(model, status):
        run_id = ledger.try_start('opencode', daily_runs=100)
        ledger.set_model(run_id, model)
        ledger.finish(run_id, status=status, exit_code=1, input_tokens=None, output_tokens=None, note='')
    assert ledger.pick_model('opencode', models) == 'm/a'
    run('m/a', 'limit')
    assert ledger.pick_model('opencode', models) == 'm/b'
    assert ledger.models_exhausted_until('opencode', models) is None and ledger.exhausted_until('opencode') is None
    run('m/b', 'model_missing')
    assert ledger.pick_model('opencode', models) is None
    assert ledger.models_exhausted_until('opencode', models) == now[0] + 3600
    run('m/a', 'limit')                                   # a second limit doubles that model's backoff
    assert ledger.models_backed_off('opencode')['m/a'] == now[0] + 7200
    now[0] += 3601                                        # m/b recovers first
    assert ledger.pick_model('opencode', models) == 'm/b'
    run('m/b', 'ok')
    assert set(ledger.models_backed_off('opencode')) == {'m/a'}


def test_a_run_without_a_model_still_backs_off_the_employee(tmp_path):
    guard = Guardrails(tmp_path)
    from jarvis.agents.dispatch_policy import DispatchLedger
    ledger = DispatchLedger(guard, clock=lambda: 1_000_000.0, alive=lambda pid: False)
    run_id = ledger.try_start('opencode', daily_runs=10)
    ledger.finish(run_id, status='model_missing', exit_code=1, input_tokens=None, output_tokens=None, note='')
    assert ledger.exhausted_until('opencode') == 1_000_000.0 + 3600
    assert ledger.models_backed_off('opencode') == {}


def test_run_status_uses_opencode_wording_for_a_missing_model():
    from jarvis.agents.dispatch_policy import run_status
    # Wording from opencode-cli: error class ProviderModelNotFoundError, message 'Model not found: <provider>/<model>'.
    assert run_status('Model not found: opencode/old-free\nDid you mean: opencode/new-free', failed=True, employee='opencode') == 'model_missing'
    assert run_status('{"type":"error","error":{"name":"ProviderModelNotFoundError"}}', failed=True, employee='opencode') == 'model_missing'
    # A successful run that merely quotes the wording (e.g. it read these tests) is not a missing model.
    assert run_status('read file: Model not found: opencode/old-free', failed=False, employee='opencode') == 'ok'
    assert run_status('{"name":"FreeTierError"} Model not found: a/b', failed=True, employee='opencode') == 'eligibility'
    assert run_status('rate limit exceeded', failed=True, employee='opencode') == 'limit'
    assert run_status('the model was not found in the docs', failed=True, employee='opencode') == 'error'
    assert run_status('fine', failed=False, employee='opencode') == 'ok'


def test_only_opencode_runs_can_be_free_tier_refusals():
    from jarvis.agents.dispatch_policy import run_status
    # Codex run 5 read this test file, quoted 'FreeTierError' in its log, and was backed off as 'eligibility'.
    quoted = 'command output: # a custom restricted agent triggered FreeTierError 403'
    assert run_status(quoted, failed=False, employee='codex') == 'ok'
    assert run_status(quoted, failed=True, employee='claude-code') == 'error'
    assert run_status(quoted, failed=False, employee='opencode') == 'eligibility'


def test_models_line_has_a_fixed_format_and_no_free_text():
    from jarvis.agents.dispatch_policy import build_digest, models_line
    until = time.mktime((2026, 10, 24, 14, 20, 0, 0, 0, -1))
    line = models_line({'opencode': (['m/a', 'm/b', 'm/c'], {'m/a': until})})
    assert line == '- opencode 모델: 지금 m/b, 소진 m/a(14:20까지)'
    assert models_line({'opencode': (['m/a'], {'m/a': until})}) == '- opencode 모델: 모두 소진, 소진 m/a(14:20까지)'
    assert models_line({}) is None
    text = build_digest(day='2026-10-24', done=[], review=[], stale=[], runs={}, exhausted={}, pause={}, models=line)
    assert line in text.splitlines()


def test_build_command_puts_the_picked_model_into_the_opencode_argv():
    from jarvis.agents.dispatch_policy import DispatcherConfig, build_command
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    argv = build_command('opencode', config, 'W', 'P', '{}', executable='oc', model='opencode/glm-5.3-flash-free')
    assert argv[argv.index('-m') + 1] == 'opencode/glm-5.3-flash-free'


def test_eligibility_backs_off_the_rotating_employee_not_the_model(tmp_path):
    from jarvis.agents.dispatch_policy import DispatchLedger
    guard = Guardrails(tmp_path)
    now = [1_000_000.0]
    ledger = DispatchLedger(guard, clock=lambda: now[0], alive=lambda pid: False)
    models = ['m/a', 'm/b']
    run_id = ledger.try_start('opencode', daily_runs=100)
    ledger.set_model(run_id, 'm/a')
    ledger.finish(run_id, status='eligibility', exit_code=1, input_tokens=None, output_tokens=None, note='')
    assert ledger.models_backed_off('opencode') == {}            # 403 FreeTierError is not the model's fault
    assert ledger.rotation_exhausted_until('opencode', models) == now[0] + 3600
    for model in models:                                        # all models out later: wait for the later time
        run_id = ledger.try_start('opencode', daily_runs=100)
        ledger.set_model(run_id, model)
        ledger.finish(run_id, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='')
    assert ledger.rotation_exhausted_until('opencode', models) == now[0] + 3600
    now[0] += 3601
    assert ledger.rotation_exhausted_until('opencode', models) is None


# --- J2 H-02: the same unread input never wakes an employee again for nothing ------------------------------

def _finish(ledger, employee, status, key, *, model=None):
    run_id = ledger.try_start(employee, daily_runs=100, input_key=key)
    if model:
        ledger.set_model(run_id, model)
    ledger.finish(run_id, status=status, exit_code=0 if status == 'ok' else 1, input_tokens=None,
                  output_tokens=None, note='')


def test_due_holds_unchanged_input_and_retry_backoff_after_other_reasons():
    held = due([state(input_hold='unchanged_input')], NOW, daily_runs=10)[0]
    assert (held.wake, held.reason) == (False, 'unchanged_input')
    held = due([state(input_hold='retry_backoff')], NOW, daily_runs=10)[0]
    assert (held.wake, held.reason) == (False, 'retry_backoff')
    # no pending work and the caps still win, so earlier reasons keep their meaning
    assert due([state(pending=0, input_hold='unchanged_input')], NOW, daily_runs=10)[0].reason == 'no_pending_work'
    assert due([state(runs_today=10, input_hold='unchanged_input')], NOW, daily_runs=10)[0].reason == 'daily_cap'
    assert due([state()], NOW, daily_runs=10)[0].reason == 'pending_work'


def test_input_key_is_order_free_and_empty_without_pending():
    from jarvis.agents.dispatch_policy import input_key
    assert input_key([3, 1, 2]) == input_key([1, 2, 3]) != input_key([1, 2])
    assert input_key([]) is None and len(input_key([1])) == 16


@pytest.mark.parametrize('status', ['ok', 'stopped'])
def test_same_input_after_ok_or_stopped_is_not_woken_again(tmp_path, status):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: False)
    assert ledger.input_hold('codex', 'k1') == ''
    _finish(ledger, 'codex', status, 'k1')
    assert ledger.input_hold('codex', 'k1') == 'unchanged_input'
    assert ledger.input_hold('codex', 'k2') == ''          # a new message changes the key
    assert ledger.input_hold('codex', None) == ''


@pytest.mark.parametrize('status', ['error', 'timeout', 'lost'])
def test_same_input_after_a_failure_waits_a_growing_backoff(tmp_path, status):
    clock = [NOW]
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: clock[0], alive=lambda pid: False)
    _finish(ledger, 'codex', status, 'k1')
    assert ledger.input_hold('codex', 'k1') == 'retry_backoff'
    assert ledger.input_hold('codex', 'k2') == ''
    clock[0] += 3600
    assert ledger.input_hold('codex', 'k1') == ''          # first retry after one hour (next_backoff start)
    _finish(ledger, 'codex', status, 'k1')
    clock[0] += 3600
    assert ledger.input_hold('codex', 'k1') == 'retry_backoff'
    clock[0] += 3600
    assert ledger.input_hold('codex', 'k1') == ''          # second retry after two hours
    assert ledger.exhausted_until('codex') is None        # an ordinary failure is not a limit


@pytest.mark.parametrize('status', ['limit', 'eligibility', 'model_missing'])
def test_limit_like_statuses_are_left_to_the_exhaustion_ledger(tmp_path, status):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: False)
    _finish(ledger, 'codex', status, 'k1')
    assert ledger.input_hold('codex', 'k1') == ''


def test_input_backoff_is_per_employee_and_separate_from_model_rotation(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: False)
    _finish(ledger, 'opencode', 'error', 'k1', model='m/a')
    assert ledger.input_hold('opencode', 'k1') == 'retry_backoff'
    assert ledger.input_hold('codex', 'k1') == ''              # another employee with the same key is free
    assert ledger.models_backed_off('opencode') == {}          # an error never backs off a model
    assert ledger.pick_model('opencode', ['m/a', 'm/b']) == 'm/a'
    assert ledger.exhausted_until('opencode') is None


def test_legacy_rows_without_a_key_never_hold(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: False)
    ledger.finish(ledger.start('codex', pid=1), status='ok', exit_code=0, input_tokens=1, output_tokens=1, note='')
    assert ledger.input_hold('codex', 'k1') == ''


def test_older_ledger_gains_the_input_key_column_in_place(tmp_path):
    import sqlite3
    folder = tmp_path / '.jarvis-dispatch'
    folder.mkdir()
    db = sqlite3.connect(folder / 'ledger.sqlite3')
    db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY, employee TEXT NOT NULL, pid INTEGER, started REAL NOT NULL, '
               'ended REAL, status TEXT NOT NULL, exit_code INTEGER, input_tokens INTEGER, output_tokens INTEGER, note TEXT)')
    db.execute("INSERT INTO runs(employee,started,ended,status) VALUES('codex',1,2,'ok')")
    db.commit()
    db.close()
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: False)
    assert ledger.input_hold('codex', 'k1') == ''
    _finish(ledger, 'codex', 'ok', 'k1')
    assert ledger.input_hold('codex', 'k1') == 'unchanged_input'


def _h02_tick(tmp_path, monkeypatch, finish_status='ok'):
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import load_roster
    from jarvis.features.board.service import BoardStore
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'claude-code': {}}, 'lead': {'enabled': False}}), encoding='utf-8')
    roster, guard = load_roster(), Guardrails(workspace)
    board = BoardStore(guard, roster=roster)
    launched, alerted = [], []

    def run(employee, run_id, config, guard, ledger, board):   # the employee never reads its inbox
        launched.append(employee)
        ledger.finish(run_id, status=finish_status, exit_code=0, input_tokens=1, output_tokens=1, note='')
        return finish_status
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: guard)
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda g: BoardStore(g, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'prepare_run', run)
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'send_alerts', lambda *a, **k: alerted.append(k.get('unchanged')) or 0)
    monkeypatch.setattr(cli_dispatch, 'collect_codex', lambda g: None)
    return cli_dispatch, board, launched, alerted


def test_tick_does_not_wake_again_for_the_same_unread_input(tmp_path, monkeypatch):
    cli_dispatch, board, launched, alerted = _h02_tick(tmp_path, monkeypatch)
    board.send('user:heejun', 'claude-code', 'first request')
    cli_dispatch.tick()
    assert launched == ['claude-code']
    cli_dispatch.tick()
    assert launched == ['claude-code']                                    # same input: no second model run
    reasons = {d['employee']: d['reason'] for d in cli_dispatch.tick(dry_run=True)['decisions']}
    assert reasons['claude-code'] == 'unchanged_input'
    assert alerted[-1] == ['claude-code']                                 # the user hears about it
    board.send('user:heejun', 'claude-code', 'second request')
    cli_dispatch.tick()
    assert launched == ['claude-code', 'claude-code']                     # new input wakes normally


def test_tick_retries_a_failed_run_only_after_backoff(tmp_path, monkeypatch):
    cli_dispatch, board, launched, alerted = _h02_tick(tmp_path, monkeypatch, finish_status='error')
    board.send('user:heejun', 'claude-code', 'first request')
    cli_dispatch.tick()
    cli_dispatch.tick()
    assert launched == ['claude-code']
    reasons = {d['employee']: d['reason'] for d in cli_dispatch.tick(dry_run=True)['decisions']}
    assert reasons['claude-code'] == 'retry_backoff'
    assert alerted[-1] == []                                              # failures already alert per run


def test_tick_runs_the_lead_past_a_held_employee_and_a_new_message_releases_it(tmp_path, monkeypatch):
    cli_dispatch, board, launched, alerted = _h02_tick(tmp_path, monkeypatch)
    config = cli_dispatch.CONFIG_ROOT / 'dispatcher.json'
    data = json.loads(config.read_text(encoding='utf-8'))
    data['employees']['codex'] = {}
    data['lead'] = {'enabled': True}
    config.write_text(json.dumps(data), encoding='utf-8')
    leads = []
    monkeypatch.setattr(cli_dispatch, 'run_lead', lambda *a: leads.append(1) or {'action': 'none', 'reason': 'test'})
    board.send('user:heejun', 'claude-code', 'first request')
    cli_dispatch.tick()                       # pending work: the lead waits, the employee runs (never reads)
    assert launched == ['claude-code'] and leads == []
    cli_dispatch.tick()                       # held on unchanged input, codex idle: the lead may plan
    assert launched == ['claude-code'] and leads == [1]
    board.send('user:heejun', 'claude-code', 'second request')
    cli_dispatch.tick()                       # new input releases the hold; the lead waits again
    assert launched == ['claude-code', 'claude-code'] and leads == [1]


# J2 usage quality (0f1bde4c): the parser's quality is kept per run; NULL means not recorded.

def _quality_run(ledger, employee, quality, inp, out, cache=None):
    run = ledger.start(employee, pid=1)
    ledger.finish(run, status='ok', exit_code=0, input_tokens=inp, output_tokens=out, cache_tokens=cache,
                  note='', usage_quality=quality)
    return run


def test_finish_stores_the_usage_quality_and_rejects_unknown_values(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    for quality in ('measured', 'partial', 'unknown', 'missing'):
        _quality_run(ledger, 'codex', quality, None, None)
    ledger.finish(ledger.start('codex', pid=1), status='error', exit_code=None, input_tokens=None,
                  output_tokens=None, note='executable not found')            # older caller: not recorded
    assert [row['usage_quality'] for row in reversed(ledger.summary()['recent'])] == [
        'measured', 'partial', 'unknown', 'missing', None]
    with pytest.raises(ValueError):
        ledger.finish(ledger.start('codex', pid=1), status='ok', exit_code=0, input_tokens=1, output_tokens=1,
                      note='', usage_quality='guessed')


def test_summary_counts_runs_per_quality_and_keeps_known_sums_apart(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    _quality_run(ledger, 'claude-code', 'measured', 1500, 200, cache=1400)   # input already includes the cache
    _quality_run(ledger, 'claude-code', 'partial', None, 30)
    _quality_run(ledger, 'claude-code', 'missing', None, None)
    _quality_run(ledger, 'claude-code', 'unknown', None, None)
    totals = ledger.summary()['claude-code']
    # existing fields keep their meaning: unread numbers still add 0
    assert totals['runs'] == 4 and totals['total_input_tokens'] == 1500 and totals['total_output_tokens'] == 230
    assert totals['usage_quality_runs'] == {'measured': 1, 'partial': 1, 'unknown': 1, 'missing': 1, 'unrecorded': 0}
    assert (totals['known_input_tokens'], totals['known_input_runs']) == (1500, 1)   # cache not added again
    assert (totals['known_output_tokens'], totals['known_output_runs']) == (230, 2)
    assert totals['unrecorded_input_tokens'] is None and totals['unrecorded_output_tokens'] is None


def test_summary_of_runs_with_no_readable_usage_has_no_known_sum(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    _quality_run(ledger, 'cursor', 'missing', None, None)
    _quality_run(ledger, 'cursor', 'unknown', None, None)
    totals = ledger.summary()['cursor']
    assert totals['total_input_tokens'] == 0 and totals['total_output_tokens'] == 0   # unchanged legacy view
    assert totals['known_input_tokens'] is None and totals['known_output_tokens'] is None
    assert totals['known_input_runs'] == 0 and totals['known_output_runs'] == 0
    assert totals['usage_quality_runs']['missing'] == 1 and totals['usage_quality_runs']['unknown'] == 1


def test_a_measured_zero_is_a_known_zero_not_a_missing_value(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    _quality_run(ledger, 'codex', 'measured', 0, 0)
    totals = ledger.summary()['codex']
    assert (totals['known_input_tokens'], totals['known_input_runs']) == (0, 1)
    assert (totals['known_output_tokens'], totals['known_output_runs']) == (0, 1)
    assert totals['usage_quality_runs']['measured'] == 1


def test_tokens_today_counts_partial_and_unreadable_runs_without_changing_the_cap_total(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    _quality_run(ledger, 'codex', 'measured', 100, 5, cache=0)
    before = ledger.tokens_today('codex')['total']
    _quality_run(ledger, 'codex', 'partial', None, 7)
    _quality_run(ledger, 'codex', 'missing', None, None)
    _quality_run(ledger, 'codex', 'unknown', None, None)
    ledger.finish(ledger.start('codex', pid=1), status='error', exit_code=None, input_tokens=None,
                  output_tokens=None, note='')                                # not recorded: not counted
    today = ledger.tokens_today('codex')
    assert before == 105 and today['total'] == 112                          # known numbers only, no guess
    assert today['partial_runs'] == 1 and today['unknown_runs'] == 2


def test_older_ledger_gains_the_quality_column_and_old_rows_are_never_promoted(tmp_path):
    import sqlite3
    (tmp_path / '.jarvis-dispatch').mkdir()
    db = sqlite3.connect(tmp_path / '.jarvis-dispatch' / 'ledger.sqlite3')
    with db:
        db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT NOT NULL, pid INTEGER, '
                   'started REAL NOT NULL, ended REAL, status TEXT NOT NULL, exit_code INTEGER, input_tokens INTEGER, '
                   'output_tokens INTEGER, note TEXT NOT NULL DEFAULT "")')
        db.execute("INSERT INTO runs(employee,started,ended,status,input_tokens,output_tokens) "
                   "VALUES('codex',?,?,'ok',100,5)", (NOW, NOW))
        db.execute("INSERT INTO runs(employee,started,ended,status) VALUES('codex',?,?,'ok')", (NOW, NOW))
    db.close()
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    totals = ledger.summary()['codex']
    # both numbers are present on the first row, but nothing recorded how they were read
    assert totals['usage_quality_runs'] == {'measured': 0, 'partial': 0, 'unknown': 0, 'missing': 0, 'unrecorded': 2}
    # their numbers stay apart from the known sums, and the old totals still include them
    assert (totals['known_input_tokens'], totals['known_input_runs']) == (None, 0)
    assert (totals['unrecorded_input_tokens'], totals['unrecorded_output_tokens']) == (100, 5)
    assert totals['total_input_tokens'] == 100 and totals['total_output_tokens'] == 5
    assert [row['usage_quality'] for row in ledger.summary()['recent']] == [None, None]
    today = ledger.tokens_today('codex')
    assert today['total'] == 105 and today['partial_runs'] == 0 and today['unknown_runs'] == 0
    ledger.finish(ledger.start('codex', pid=1), status='ok', exit_code=0, input_tokens=7, output_tokens=2,
                  note='', usage_quality='measured')
    mixed = ledger.summary()['codex']
    assert (mixed['known_input_tokens'], mixed['known_input_runs']) == (7, 1)
    assert (mixed['unrecorded_input_tokens'], mixed['total_input_tokens']) == (100, 107)


def test_usage_quality_survives_a_ledger_reopen(tmp_path):
    ledger = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    _quality_run(ledger, 'codex', 'partial', None, 3)
    reopened = DispatchLedger(Guardrails(tmp_path), clock=lambda: NOW, alive=lambda pid: True)
    assert reopened.summary()['recent'][0]['usage_quality'] == 'partial'
    assert reopened.summary()['codex']['usage_quality_runs']['partial'] == 1


def test_wake_prompt_names_the_waking_ids_instead_of_the_whole_inbox():
    prompt = wake_prompt('codex', 'C:/p/python.exe', ids=[12, 15])
    assert "board_inbox(agent='codex:auto', ids=[12, 15])" in prompt
    assert "board_inbox(agent='codex:auto')" not in prompt and '"' not in prompt
    assert wake_prompt('codex', 'C:/p/python.exe') == wake_prompt('codex', 'C:/p/python.exe', ids=())


def _two_employee_tick(tmp_path, monkeypatch):
    from jarvis.cli import dispatch as cli_dispatch
    from jarvis.core.roster import load_roster
    from jarvis.features.board.service import BoardStore
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'claude-code': {}, 'codex': {}}, 'lead': {'enabled': False}}), encoding='utf-8')
    roster, guard = load_roster(), Guardrails(workspace)
    board = BoardStore(guard, roster=roster)
    board.send('user:heejun', 'claude-code', 'build please')
    board.send('user:heejun', 'codex', 'build please')
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: guard)
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda g: BoardStore(g, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'send_alerts', lambda *a, **k: 0, raising=False)
    monkeypatch.setattr(cli_dispatch, 'collect_codex', lambda g: None)
    return cli_dispatch, guard


def test_tick_prepares_every_run_before_any_starts_and_pushes_only_after_all_ended(tmp_path, monkeypatch):
    import threading
    cli_dispatch, guard = _two_employee_tick(tmp_path, monkeypatch)
    events, lock = [], threading.Lock()

    def prepare(employee, run_id, *a):
        events.append(('prepare', employee))
        return {'employee': employee, 'run_id': run_id}

    def execute(run, guard, ledger, board):
        with lock:
            events.append(('start', run['employee']))
        time.sleep(0.2 if run['employee'] == 'codex' else 0)   # codex is still running when claude-code ends
        ledger.finish(run['run_id'], status='ok', exit_code=0, input_tokens=1, output_tokens=1, note='')
        with lock:
            events.append(('end', run['employee']))
        return 'ok', run

    monkeypatch.setattr(cli_dispatch, 'prepare_run', prepare)
    monkeypatch.setattr(cli_dispatch, 'execute_run', execute)
    monkeypatch.setattr(cli_dispatch, 'push_after_runs',
                        lambda guard, board, ready: events.append(('push', sorted(r['employee'] for r in ready))))
    report = cli_dispatch.tick()
    assert report['launched'] == {'claude-code': 'ok', 'codex': 'ok'}
    kinds = [kind for kind, _ in events]
    assert kinds[:2] == ['prepare', 'prepare'] and kinds[-1] == 'push'
    assert events[-1] == ('push', ['claude-code', 'codex']) and events[-2] == ('end', 'codex')


def test_tick_starts_nothing_when_a_later_check_paused_the_dispatcher(tmp_path, monkeypatch):
    from jarvis.agents import dispatch_control
    cli_dispatch, guard = _two_employee_tick(tmp_path, monkeypatch)
    started = []

    def prepare(employee, run_id, config, guard, ledger, board):
        if started == ['first']:   # the second employee's check finds a change and pauses
            dispatch_control.pause(guard, 'git changed')
            ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                          note='git changed since last run')
            return 'error'
        started.append('first')
        return {'employee': employee, 'run_id': run_id}

    monkeypatch.setattr(cli_dispatch, 'prepare_run', prepare)
    monkeypatch.setattr(cli_dispatch, 'execute_run', lambda *a: pytest.fail('no run may start'))
    report = cli_dispatch.tick()
    assert sorted(report['launched'].values()) == ['error', 'error']
    notes = {row['note'] for row in DispatchLedger(guard).summary()['recent']}
    assert notes == {'dispatcher paused', 'git changed since last run'}
