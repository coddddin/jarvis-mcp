"""worker_adapters: one roster-based home for employee CLI discovery and headless commands.

The LEGACY_* functions below are verbatim copies of the code at 99f8369 before it moved here. The tests pin
the new code to them byte for byte, so the move cannot change any launched command line.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis.agents import worker_adapters as wa
from jarvis.agents.dispatch_policy import (EMPLOYEES, PROBATION_PROMPT, DispatcherConfig, build_command, build_env,
                                           wake_prompt)
from jarvis.agents.team import HybridTeam, _discover_claude
from jarvis.cli import dispatch as cli_dispatch
from jarvis.core.roster import load_roster

LEGACY_OPENCODE_DEFAULT_MODEL = 'opencode/glm-5.3-flash-free'
LEGACY_CLAUDE_TOOLS = ['Read', 'Edit', 'Write', 'Glob', 'Grep',
                       'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git add:*)', 'Bash(git commit:*)',
                       *(f'mcp__jarvis__board_{name}' for name in
                         ('list', 'get', 'create', 'claim', 'heartbeat', 'note', 'release', 'send', 'inbox'))]


def legacy_build_command(employee, config, worktree, prompt, mcp_json, *, executable, git_dir=None, python=None):
    worktree = str(worktree)
    if employee == 'claude-code':
        return [executable, '-p', prompt, '--output-format', 'json', '--setting-sources', '',
                '--strict-mcp-config', '--mcp-config', mcp_json, '--no-session-persistence',
                '--permission-mode', 'acceptEdits', '--max-turns', str(config.employees[employee].max_turns),
                '--allowedTools', *LEGACY_CLAUDE_TOOLS, *([f'Bash({python}:*)'] if python else [])]
    if employee == 'codex':
        return [executable, '-a', 'never', 'exec', '-C', worktree, '--sandbox', 'workspace-write',
                '--skip-git-repo-check', '--json', '--color', 'never',
                *(['--add-dir', str(git_dir)] if git_dir else []), prompt]
    if employee == 'cursor':
        return [executable, '-p', '--approve-mcps', '--trust', '--output-format', 'json',
                '--workspace', worktree, prompt]
    if employee == 'opencode':
        model = config.employees[employee].model or LEGACY_OPENCODE_DEFAULT_MODEL
        return [executable, 'run', '--standalone', '-m', model, '--format', 'json', prompt]
    raise ValueError(f'No headless command for {employee}')


def legacy_build_env(employee, *, project_root, workspace):
    if employee != 'opencode':
        return {}
    python = Path(project_root) / '.venv' / 'Scripts' / 'python.exe'
    config = {
        'permission': {'edit': 'deny', 'webfetch': 'deny',
                       'bash': {'*': 'deny', 'git status *': 'allow', 'git diff *': 'allow', 'git log *': 'allow'}},
        'mcp': {'jarvis': {'type': 'local', 'command': [str(python), str(Path(project_root) / 'server.py')],
                           'environment': {'JARVIS_WORKSPACE': str(workspace)}, 'enabled': True}},
        'tools': {'jarvis_*': False, 'jarvis_board_*': True},
    }
    return {'OPENCODE_CONFIG_CONTENT': json.dumps(config)}


def legacy_executable(employee):
    if employee == 'claude-code':
        found = shutil.which('claude')
        return [found] if found else None
    if employee == 'codex':
        base = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI' / 'Codex' / 'bin'
        candidates = sorted(base.glob('*/codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True)
        return [str(candidates[0])] if candidates else None
    if employee == 'cursor':
        script = Path(os.environ.get('LOCALAPPDATA', '')) / 'cursor-agent' / 'cursor-agent.ps1'
        if script.is_file():
            return ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
    if employee == 'opencode':
        bundled = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / '@opencodedesktop' / 'resources' / 'opencode-cli.exe'
        found = str(bundled) if bundled.is_file() else shutil.which('opencode')
        return [found] if found else None
    return None


def as_bytes(argv):
    """The exact Windows command line the process receives."""
    return subprocess.list2cmdline([str(part) for part in argv]).encode('utf-8')


def config(**employees):
    base = {'codex': {}, 'claude-code': {'max_turns': 37}, 'cursor': {}, 'opencode': {}}
    for name, values in employees.items():
        base[name] = values
    return DispatcherConfig.model_validate({'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10,
                                            'run_timeout_minutes': 30, 'employees': base})


# --- work mode: dispatch_policy delegation is byte-identical -------------------------------------------

@pytest.mark.parametrize('employee', ['codex', 'claude-code', 'cursor', 'opencode'])
@pytest.mark.parametrize('python', [None, 'C:/p y/.venv/Scripts/python.exe'])
@pytest.mark.parametrize('git_dir', [None, Path('C:/repo/.git')])
@pytest.mark.parametrize('model', ['', 'opencode/other-free'])
def test_build_command_is_byte_identical(employee, python, git_dir, model):
    cfg = config(opencode={'model': model})
    prompt = '너는 직원이다.\n2. "따옴표"와 공백 포함'
    args = (employee, cfg, Path('C:/wt/my tree'), prompt, '{"mcpServers":{}}')
    kwargs = dict(executable='C:/bin/tool.exe', git_dir=git_dir, python=python)
    new, old = build_command(*args, **kwargs), legacy_build_command(*args, **kwargs)
    assert new == old
    assert as_bytes(new) == as_bytes(old)


def test_build_command_errors_match():
    cfg = config()
    for call in (build_command, legacy_build_command):
        with pytest.raises(ValueError, match='No headless command for gemini'):
            call('gemini', cfg, 'w', 'p', '{}', executable='x')
    # Settings are read only for employees that need them, exactly as before.
    partial = DispatcherConfig.model_validate({'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10,
                                               'run_timeout_minutes': 30, 'employees': {'codex': {}}})
    assert build_command('cursor', partial, 'w', 'p', '{}', executable='x') == \
        legacy_build_command('cursor', partial, 'w', 'p', '{}', executable='x')
    for call in (build_command, legacy_build_command):
        with pytest.raises(KeyError):
            call('claude-code', partial, 'w', 'p', '{}', executable='x')


@pytest.mark.parametrize('employee', ['codex', 'claude-code', 'cursor', 'gemini', 'unknown'])
def test_build_env_is_identical(employee):
    kwargs = dict(project_root=Path('C:/proj root'), workspace=Path('C:/ws'))
    assert build_env(employee, **kwargs) == legacy_build_env(employee, **kwargs)


# 100bb3a1: the only intended change to OpenCode's env is the added privacy and web rules.
PRIVACY_RULES = {'external_directory': 'deny', 'websearch': 'deny', 'task': 'deny'}


def test_opencode_env_changes_only_by_the_privacy_rules():
    kwargs = dict(project_root=Path('C:/proj root'), workspace=Path('C:/ws'))
    new = json.loads(build_env('opencode', **kwargs)['OPENCODE_CONFIG_CONTENT'])
    old = json.loads(legacy_build_env('opencode', **kwargs)['OPENCODE_CONFIG_CONTENT'])
    permission = new['permission']
    assert {key: permission.pop(key) for key in PRIVACY_RULES} == PRIVACY_RULES
    assert permission.pop('shell') == old['permission']['bash']   # current docs name; bash kept for v2.0.32
    assert new == old


@pytest.mark.parametrize('layout', ['empty', 'installed'])
@pytest.mark.parametrize('employee', ['codex', 'claude-code', 'cursor', 'opencode', 'gemini'])
def test_work_discovery_is_identical(tmp_path, monkeypatch, layout, employee):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    found = {'claude': 'C:/bin/claude.exe', 'opencode': 'C:/bin/opencode.exe'} if layout == 'installed' else {}
    monkeypatch.setattr(shutil, 'which', lambda name: found.get(name))
    if layout == 'installed':
        for relative in ('OpenAI/Codex/bin/1.0/codex.exe', 'OpenAI/Codex/bin/2.0/codex.exe',
                         'cursor-agent/cursor-agent.ps1', 'Programs/@opencodedesktop/resources/opencode-cli.exe'):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        newest = tmp_path / 'OpenAI/Codex/bin/2.0/codex.exe'
        os.utime(newest, (newest.stat().st_atime, newest.stat().st_mtime + 100))
    assert cli_dispatch.executable(employee) == legacy_executable(employee)


# --- proposal mode: adapter matches the unchanged team.py ----------------------------------------------

@pytest.mark.parametrize('employee', ['codex', 'claude-code'])
def test_proposal_command_matches_team(tmp_path, employee):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    executable = tmp_path / (uuid4().hex + '.exe')
    executable.write_bytes(b'fixture; never executed')
    schema = tmp_path / 'schema.json'
    schema.write_text('{"type": "object", "x": "따옴표\\"포함"}', encoding='utf-8')
    fake_team = SimpleNamespace(executables={employee: str(executable)}, guard=SimpleNamespace(workspace=workspace))
    expected = HybridTeam._external_command(fake_team, employee, schema, tmp_path / 'answer.json')
    trusted = wa.trusted_native_executable(str(executable), workspace)
    actual = wa.adapter(employee).proposal_command(trusted, schema, tmp_path / 'answer.json')
    assert actual == expected and as_bytes(actual) == as_bytes(expected)


def test_untrusted_proposal_executables_are_refused(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    inside = workspace / 'danger.exe'
    inside.write_bytes(b'x')
    script = tmp_path / 'tool.cmd'
    script.write_bytes(b'x')
    for value in (None, '', 'relative.exe', str(inside), str(script), str(tmp_path / 'missing.exe')):
        with pytest.raises(ValueError):
            wa.trusted_native_executable(value, workspace)


def test_claude_native_discovery_matches_team(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    assert wa.discover_claude_native() is None and _discover_claude() is None
    installed = tmp_path / 'AppData/Local/Microsoft/WinGet/Packages/Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe/claude.exe'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b'fixture')
    assert wa.discover_claude_native() == _discover_claude() == str(installed)


# --- registry and roster ------------------------------------------------------------------------------

def test_employee_lists_come_from_the_adapters():
    assert EMPLOYEES == ('codex', 'claude-code', 'cursor', 'opencode')
    assert PROBATION_PROMPT == ('cursor', 'opencode')
    assert wa.PROPOSAL_EMPLOYEES == ('codex', 'claude-code')
    literal = DispatcherConfig.model_fields['employees'].annotation.__args__[0].__args__
    assert tuple(literal) == EMPLOYEES  # the config schema accepts exactly the adapter employees
    assert {name: item.workload_name for name, item in wa.ADAPTERS.items() if item.workload_name} == \
        {'codex': 'codex', 'claude-code': 'claude'}


def test_wake_prompt_unchanged_for_probation_employees():
    for employee in EMPLOYEES:
        text = wake_prompt(employee)
        assert ('수습 직원이므로' in text) == (employee in ('cursor', 'opencode'))


def test_current_roster_agrees_with_the_adapters():
    roster = load_roster()
    assert wa.check_roster(roster) == []
    assert wa.employed(roster) == ('codex', 'claude-code', 'cursor', 'opencode')


def test_roster_disagreements_are_reported():
    roster = load_roster()
    agents = dict(roster.agents)
    agents['cursor'] = agents['cursor'].model_copy(update={'status': 'retired'})
    agents['claude-code'] = agents['claude-code'].model_copy(update={'status': 'probation'})
    del agents['opencode']
    changed = roster.model_copy(update={'agents': agents})
    problems = wa.check_roster(changed)
    assert 'cursor: adapter for a retired employee' in problems
    assert any(p.startswith('claude-code: probation employee with an edit-capable work command (') for p in problems)
    assert 'opencode: adapter without a roster entry' in problems
    assert wa.employed(changed) == ('codex', 'claude-code')


# --- review fix: probation checks inspect the real command and environment ----------------------------

def tampered(monkeypatch, employee, **changes):
    from dataclasses import replace
    monkeypatch.setitem(wa.ADAPTERS, employee, replace(wa.ADAPTERS[employee], **changes))


def problems_for(roster, employee):
    return [p for p in wa.check_roster(roster) if p.startswith(employee + ':')]


def test_probation_cursor_with_force_is_caught(monkeypatch):
    original = wa.ADAPTERS['cursor'].work_command
    tampered(monkeypatch, 'cursor', work_command=lambda r: original(r) + ['--force'])
    assert any('--force' in p for p in problems_for(load_roster(), 'cursor'))


@pytest.mark.parametrize('permission,marker', [
    ({'edit': 'allow', 'bash': {'*': 'deny'}}, 'edit'),
    ({'edit': 'deny', 'bash': {'*': 'allow'}}, 'bash'),
    ({'edit': 'deny', 'bash': 'allow'}, 'bash'),
    ({'bash': {'*': 'deny'}}, 'edit'),               # missing edit rule falls back to the CLI default
])
def test_probation_opencode_edit_or_shell_permission_is_caught(monkeypatch, permission, marker):
    def env(project_root, workspace):
        return {'OPENCODE_CONFIG_CONTENT': json.dumps({'permission': permission, 'tools': {}})}
    tampered(monkeypatch, 'opencode', work_env=env)
    assert any(marker in p for p in problems_for(load_roster(), 'opencode'))


def test_probation_opencode_without_its_config_is_caught(monkeypatch):
    tampered(monkeypatch, 'opencode', work_env=lambda project_root, workspace: {})
    assert any('config' in p for p in problems_for(load_roster(), 'opencode'))


def test_probation_claude_is_caught_by_its_command_not_its_label(monkeypatch):
    roster = load_roster()
    agents = dict(roster.agents)
    agents['claude-code'] = agents['claude-code'].model_copy(update={'status': 'probation'})
    roster = roster.model_copy(update={'agents': agents})
    tampered(monkeypatch, 'claude-code', applies_edits=False)  # the label lies; the argv does not
    found = problems_for(roster, 'claude-code')
    assert any('acceptEdits' in p for p in found) and any('Edit' in p and 'Write' in p for p in found)


def test_claude_read_only_tools_are_not_flagged(monkeypatch):
    def read_only(r):
        return [r.executable, '-p', r.prompt, '--permission-mode', 'default', '--allowedTools', 'Read', 'Grep',
                'Bash(git status:*)', 'mcp__jarvis__board_list', '--max-turns', '3']
    roster = load_roster()
    agents = dict(roster.agents)
    agents['claude-code'] = agents['claude-code'].model_copy(update={'status': 'probation'})
    roster = roster.model_copy(update={'agents': agents})
    tampered(monkeypatch, 'claude-code', applies_edits=False, work_command=read_only)
    assert problems_for(roster, 'claude-code') == []


def test_current_adapters_report_their_real_edit_markers():
    assert wa.edit_markers('cursor') == [] and wa.edit_markers('opencode') == []
    assert any('acceptEdits' in m for m in wa.edit_markers('claude-code'))
    assert any('workspace-write' in m for m in wa.edit_markers('codex'))


# --- review fix: the dispatcher refuses to wake an employee whose adapter fails the check -------------

def test_blocked_employees_are_not_woken():
    from jarvis.agents.dispatch_policy import Decision, block_unsafe
    decisions = [Decision('codex', True, 'pending_work'), Decision('cursor', True, 'pending_work'),
                 Decision('opencode', False, 'no_pending_work')]
    out = block_unsafe(decisions, {'cursor': ['cursor: probation employee with an edit-capable work command (--force)']})
    assert [(d.employee, d.wake, d.reason) for d in out] == [
        ('codex', True, 'pending_work'), ('cursor', False, 'adapter_check_failed'), ('opencode', False, 'no_pending_work')]


def test_tick_skips_and_logs_an_unsafe_employee(tmp_path, monkeypatch):
    from jarvis.core.guardrails import Guardrails
    from jarvis.features.board.service import BoardStore
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'cursor': {}, 'claude-code': {}}, 'lead': {'enabled': False}}), encoding='utf-8')
    roster = load_roster()
    board = BoardStore(Guardrails(workspace), roster=roster)
    board.send('user:heejun', 'cursor', 'review please')
    board.send('user:heejun', 'claude-code', 'build please')
    launched, logged = [], []
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: Guardrails(workspace))
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda guard: BoardStore(guard, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'run_employee', lambda employee, *a: launched.append(employee) or 'ok')
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'log_service_error', logged.append)
    original = wa.ADAPTERS['cursor'].work_command
    tampered(monkeypatch, 'cursor', work_command=lambda r: original(r) + ['--force'])
    report = cli_dispatch.tick()
    assert launched == ['claude-code']
    assert {d['employee']: d['reason'] for d in report['decisions']}['cursor'] == 'adapter_check_failed'
    assert len(logged) == 1 and 'cursor' in str(logged[0]) and '--force' in str(logged[0])
    dry = cli_dispatch.tick(dry_run=True)
    assert {d['employee']: d['reason'] for d in dry['decisions']}['cursor'] == 'adapter_check_failed'
    assert len(logged) == 1  # dry-run reports but does not write the error log


# --- review fix 2: short and '=' forms of the same flags, and OpenCode agent switching ----------------

@pytest.mark.parametrize('extra', [['-f'], ['--force=true'], ['--yolo=1'], ['--dangerously-skip-permissions=true']])
def test_probation_cursor_force_aliases_are_caught(monkeypatch, extra):
    original = wa.ADAPTERS['cursor'].work_command
    tampered(monkeypatch, 'cursor', work_command=lambda r: original(r) + extra)
    assert problems_for(load_roster(), 'cursor')


@pytest.mark.parametrize('argv', [
    ['--permission-mode=acceptEdits'], ['--permission-mode=bypassPermissions'],
    ['--sandbox=workspace-write'], ['--sandbox=danger-full-access'],
    ['--allowedTools=Edit'], ['--allowedTools=Read', 'Write'],
])
def test_equals_forms_of_edit_flags_are_caught(monkeypatch, argv):
    tampered(monkeypatch, 'cursor', work_command=lambda r: [r.executable, '-p', r.prompt, *argv])
    assert problems_for(load_roster(), 'cursor'), argv


@pytest.mark.parametrize('extra', [['--agent', 'build'], ['--agent=build']])
def test_probation_opencode_agent_switch_fails_closed(monkeypatch, extra):
    original = wa.ADAPTERS['opencode'].work_command
    tampered(monkeypatch, 'opencode', work_command=lambda r: original(r)[:2] + extra + original(r)[2:])
    assert any('--agent' in p for p in problems_for(load_roster(), 'opencode'))


def test_read_only_equals_forms_and_prompt_text_are_not_flagged(monkeypatch):
    tampered(monkeypatch, 'cursor', work_command=lambda r: [
        r.executable, '-p', '--sandbox=read-only', '--permission-mode=default', '--output-format=json',
        '이 프롬프트에는 -f 와 --force 라는 글자가 들어 있다'])
    assert problems_for(load_roster(), 'cursor') == []


def test_tick_wakes_an_employee_whose_pid_less_row_outlived_the_run_limit(tmp_path, monkeypatch):
    # W2 stage 2: the service died between try_start and set_pid hours ago; the employee must not stay blocked.
    import time
    from jarvis.agents.dispatch_policy import DispatchLedger
    from jarvis.core.guardrails import Guardrails
    from jarvis.features.board.service import BoardStore
    workspace, config_root = tmp_path / 'ws', tmp_path / 'config'
    workspace.mkdir(), config_root.mkdir()
    (config_root / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10, 'run_timeout_minutes': 30,
        'employees': {'claude-code': {}}, 'lead': {'enabled': False}}), encoding='utf-8')
    roster = load_roster()
    guard = Guardrails(workspace)
    stuck = DispatchLedger(guard, clock=lambda: time.time() - 2 * 3600).try_start('claude-code', daily_runs=10)
    board = BoardStore(guard, roster=roster)
    board.send('user:heejun', 'claude-code', 'build please')
    launched = []
    monkeypatch.setattr(cli_dispatch, 'CONFIG_ROOT', config_root)
    monkeypatch.setattr(cli_dispatch, 'load_guardrails', lambda: Guardrails(workspace))
    monkeypatch.setattr(cli_dispatch, 'load_roster', lambda **kw: roster)
    monkeypatch.setattr(cli_dispatch, 'BoardStore', lambda guard: BoardStore(guard, roster=roster))
    monkeypatch.setattr(cli_dispatch, 'run_employee', lambda employee, *a: launched.append(employee) or 'ok')
    monkeypatch.setattr(cli_dispatch, 'send_digest_if_due', lambda *a: None)
    monkeypatch.setattr(cli_dispatch, 'log_service_error', lambda exc: None)
    cli_dispatch.tick()
    assert launched == ['claude-code']
    rows = {r['id']: r for r in DispatchLedger(guard).summary()['recent']}
    assert rows[stuck]['status'] == 'lost' and rows[stuck]['note'] == 'no_pid'


# --- 00bb1b4d: structured problem codes for the daily digest (no command fragments) --------------------

def test_problem_codes_match_the_problem_texts_and_carry_no_command_fragments(monkeypatch):
    roster = load_roster()
    assert wa.roster_problem_codes(roster) == {}
    original = wa.ADAPTERS['cursor'].work_command
    tampered(monkeypatch, 'cursor', work_command=lambda r: original(r) + ['--force'])
    codes = wa.roster_problem_codes(roster)
    assert codes == {'cursor': ['probation_can_edit']}
    assert '--force' in ' '.join(wa.roster_problems(roster)['cursor'])   # the log text keeps its detail
    assert set(code for found in codes.values() for code in found) <= set(wa.PROBLEM_CODES)


def test_problem_codes_cover_every_kind(monkeypatch):
    from dataclasses import replace
    roster = load_roster()
    agents = dict(roster.agents)
    agents['codex'] = replace(agents['codex'], status='retired') if hasattr(agents['codex'], '__dataclass_fields__') \
        else agents['codex'].model_copy(update={'status': 'retired'})
    agents.pop('claude-code')
    changed = SimpleNamespace(agents=agents)
    original = wa.ADAPTERS['opencode'].work_command
    tampered(monkeypatch, 'opencode', applies_edits=False, work_command=lambda r: original(r) + ['--yolo'])
    codes = wa.roster_problem_codes(changed)
    assert codes['codex'] == ['retired'] and codes['claude-code'] == ['no_roster']
    assert codes['opencode'] == ['probation_can_edit']


def test_current_adapters_have_no_privacy_markers():
    assert all(wa.privacy_markers(name) == [] for name in wa.ADAPTERS)
    assert wa.roster_problem_codes(load_roster()) == {}


def _opencode_env(**changes):
    """The real OpenCode config with permission or tools entries changed (None removes the entry)."""
    def env(project_root, workspace):
        config = json.loads(wa._work_env_opencode(project_root, workspace)['OPENCODE_CONFIG_CONTENT'])
        for section, key, value in changes.get('entries', []):
            if value is None:
                config[section].pop(key, None)
            else:
                config[section][key] = value
        return {'OPENCODE_CONFIG_CONTENT': json.dumps(config)}
    return env


@pytest.mark.parametrize('section,key,value,marker', [
    ('permission', 'external_directory', None, 'external_directory'),       # default ask: not proven blocked
    ('permission', 'external_directory', 'ask', 'external_directory'),
    ('permission', 'external_directory', {'*': 'deny', 'C:/Users/**': 'allow'}, 'external_directory'),
    ('permission', 'websearch', None, 'websearch'),
    ('permission', 'webfetch', 'allow', 'webfetch'),
    ('permission', 'task', None, 'task'),
    ('tools', 'jarvis_*', True, 'jarvis'),
    ('tools', 'jarvis_search_local_documents', True, 'jarvis'),
])
def test_opencode_privacy_or_web_opening_is_caught(monkeypatch, section, key, value, marker):
    tampered(monkeypatch, 'opencode', work_env=_opencode_env(entries=[(section, key, value)]))
    assert any(marker in m for m in wa.privacy_markers('opencode'))
    assert wa.roster_problem_codes(load_roster())['opencode'] == ['privacy_open']
    assert wa.edit_markers('opencode') == []          # a privacy problem is not an edit problem


def test_unreadable_or_missing_opencode_config_is_a_privacy_problem(monkeypatch):
    tampered(monkeypatch, 'opencode', work_env=lambda project_root, workspace: {'OPENCODE_CONFIG_CONTENT': '{oops'})
    assert wa.privacy_markers('opencode')
    tampered(monkeypatch, 'opencode', work_env=lambda project_root, workspace: {})
    assert wa.privacy_markers('opencode')


def test_shell_key_is_checked_like_bash(monkeypatch):
    tampered(monkeypatch, 'opencode', work_env=_opencode_env(entries=[('permission', 'shell', {'*': 'allow'})]))
    assert any('shell' in m for m in wa.edit_markers('opencode'))


def test_privacy_open_blocks_an_active_employee_too(monkeypatch):
    from dataclasses import replace
    roster = load_roster()
    agents = dict(roster.agents)
    agents['opencode'] = agents['opencode'].model_copy(update={'status': 'active'})
    active = SimpleNamespace(agents=agents)
    tampered(monkeypatch, 'opencode', work_env=_opencode_env(entries=[('permission', 'websearch', 'allow')]))
    assert wa.roster_problem_codes(active)['opencode'] == ['privacy_open']
    from jarvis.agents.dispatch_policy import Decision, block_unsafe
    out = block_unsafe([Decision('opencode', True, 'pending_work')], wa.roster_problems(active))
    assert (out[0].wake, out[0].reason) == (False, 'adapter_check_failed')
    assert replace  # dataclasses import kept local to this test


# --- OpenCode model rotation: the probation check holds for every configured model -------------------

def _rotation_models():
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    return config.employees['opencode'].models


def test_config_rotates_seven_free_opencode_models():
    models = _rotation_models()
    assert len(models) == 7 and all(m.startswith('opencode/') and m.endswith('-free') for m in models)


@pytest.mark.parametrize('model', _rotation_models())
def test_probation_check_passes_with_each_rotation_model(monkeypatch, model):
    from dataclasses import replace
    original = wa.ADAPTERS['opencode'].work_command
    tampered(monkeypatch, 'opencode', work_command=lambda r: original(replace(r, model=lambda: model)))
    assert problems_for(load_roster(), 'opencode') == []
    argv, _ = wa._probe('opencode')
    assert argv[argv.index('-m') + 1] == model


def test_rotation_changes_only_the_model_value_in_the_opencode_argv():
    config = DispatcherConfig.model_validate(json.loads(Path('config/dispatcher.json').read_text(encoding='utf-8')))
    base, _ = wa._probe('opencode')
    for model in _rotation_models():
        argv = build_command('opencode', config, 'WORKTREE', 'PROMPT', '{}', executable='EXECUTABLE', model=model)
        index = argv.index('-m') + 1
        assert argv[:index] + argv[index + 1:] == base[:index] + base[index + 1:] and argv[index] == model
