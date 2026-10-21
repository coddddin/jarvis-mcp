"""W5 dispatcher: who to wake, caps, exhaustion backoff, usage parsing and command shapes."""
import json
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
    assert 'board_inbox' in prompt and 'main' in prompt and 'user:heejun' in prompt and 'C:/p/python.exe -m pytest' in prompt
    assert '"' not in prompt and '"' not in wake_prompt('cursor')
    assert '수습' in wake_prompt('cursor') and '수습' not in prompt


def test_commands_are_lean_and_tier_appropriate(tmp_path):
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    worktree, mcp = tmp_path / 'wt', '{"mcpServers":{}}'
    claude = build_command('claude-code', config, worktree, 'P', mcp, executable='claude.exe', python='C:/p/python.exe')
    assert claude[:2] == ['claude.exe', '-p'] and '--strict-mcp-config' in claude and '--setting-sources' in claude
    assert '--dangerously-skip-permissions' not in claude and 'bypassPermissions' not in claude
    assert 'Bash(C:/p/python.exe:*)' in claude
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
