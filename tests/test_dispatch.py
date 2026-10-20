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
