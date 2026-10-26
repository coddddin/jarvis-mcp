"""One adapter per AI employee: how to find its CLI and how to shape its headless commands.

Two modes:
- proposal: one-shot answer with no tools (HybridTeam in team.py). Only codex and claude-code.
- work: an unattended run inside the employee's git worktree with tools inside its tier (the W5 dispatcher).

Employee IDs are the roster IDs in config/agents.json. check_roster() keeps the adapters and the roster in
agreement (no adapter for an unknown or retired employee, no edit-capable work command for a probation
employee). Pure shaping and discovery: nothing is launched here.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
from typing import Callable

OPENCODE_DEFAULT_MODEL = 'opencode/glm-5.3-flash-free'
BOARD_TOOLS = ('list', 'get', 'create', 'claim', 'heartbeat', 'note', 'release', 'send', 'inbox')
CLAUDE_TOOLS = ['Read', 'Edit', 'Write', 'Glob', 'Grep',
                'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git add:*)', 'Bash(git commit:*)',
                *(f'mcp__jarvis__board_{name}' for name in BOARD_TOOLS)]
CODEX_DISABLED_FEATURES = ('shell_tool', 'unified_exec', 'code_mode_host', 'code_mode', 'browser_use',
                           'browser_use_external', 'computer_use', 'in_app_browser', 'apps', 'plugins', 'hooks',
                           'image_generation', 'view_image', 'multi_agent', 'workspace_dependencies', 'skill_search')
# Work mode keeps the shell (tests and git commit run in the sandbox) and also drops remote plugins.
# It also keeps code_mode_host: in codex-cli 0.164.0-alpha.11.1 every tool call (shell and MCP) goes through it,
# and without it the first watched run (2026-10-26) failed every call with "code-mode host is disabled".
# code_mode itself (under development) stays off.
# Every name was checked against `codex features list` of that version (2026-10-26).
CODEX_WORK_KEPT_FEATURES = ('shell_tool', 'unified_exec', 'code_mode_host')
CODEX_WORK_DISABLED_FEATURES = tuple(feature for feature in CODEX_DISABLED_FEATURES
                                     if feature not in CODEX_WORK_KEPT_FEATURES) + ('remote_plugin',)


def _local_app_data():
    return Path(os.environ.get('LOCALAPPDATA', ''))


# --- discovery ----------------------------------------------------------------------------------------

def discover_claude_native():
    """Native Windows Claude Code install, found even before the host refreshes PATH (proposal mode)."""
    home = Path.home()
    candidates = [shutil.which('claude'), str(home / '.local/bin/claude.exe'),
                  str(home / 'AppData/Local/Microsoft/WinGet/Packages/'
                      'Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe/claude.exe')]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if (path.is_absolute() and path.suffix.lower() == '.exe' and path.is_file()
                    and not path.is_symlink() and path.stat().st_nlink == 1):
                return str(path)
    return None


def _work_claude():
    found = shutil.which('claude')
    return [found] if found else None


def _work_codex():
    base = _local_app_data() / 'OpenAI' / 'Codex' / 'bin'
    candidates = sorted(base.glob('*/codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(candidates[0])] if candidates else None


def _work_cursor():
    script = _local_app_data() / 'cursor-agent' / 'cursor-agent.ps1'
    if script.is_file():
        return ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
    return None


def _work_opencode():
    bundled = _local_app_data() / 'Programs' / '@opencodedesktop' / 'resources' / 'opencode-cli.exe'
    found = str(bundled) if bundled.is_file() else shutil.which('opencode')
    return [found] if found else None


def trusted_native_executable(executable, workspace):
    """Path of an installed native .exe outside the data workspace, or ValueError."""
    if not executable:
        raise ValueError('unavailable')
    path = Path(executable)
    if (not path.is_absolute() or not path.is_file() or path.suffix.lower() != '.exe'
            or path.is_symlink() or path.stat().st_nlink != 1
            or path.resolve().is_relative_to(workspace)):
        raise ValueError('External worker must be a trusted installed native executable.')
    return path


# --- proposal mode (no tools) -------------------------------------------------------------------------

def _proposal_codex(executable, schema_path, result_path):
    argv = [str(executable), '--no-daemon', '-a', 'never', 'exec', '--ignore-user-config',
            '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '--color', 'never',
            '-c', 'project_doc_max_bytes=0', '-c', 'web_search="disabled"']
    for feature in CODEX_DISABLED_FEATURES:
        argv += ['--disable', feature]
    return argv + ['--output-schema', str(schema_path), '--output-last-message', str(result_path), '-']


def _proposal_claude(executable, schema_path, result_path):
    return [str(executable), '-p', '--output-format', 'json', '--json-schema', Path(schema_path).read_text(encoding='utf-8'),
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--no-session-persistence', '--max-turns', '1']


# --- work mode (worktree, tools within tier) ----------------------------------------------------------

@dataclass(frozen=True)
class WorkRequest:
    worktree: str
    prompt: str
    mcp_json: str
    executable: str
    max_turns: Callable[[], int]      # read lazily: only employees that use it require its settings
    model: Callable[[], str]
    git_dir: object = None
    python: object = None


def _work_command_claude(r):
    # The interpreter is allowed for `-m pytest` only, which narrows direct use (no `python -c`). pytest still runs
    # repo code such as conftest.py, so this is not a sandbox; dispatch.run_employee checks .git after the run.
    return [r.executable, '-p', r.prompt, '--output-format', 'json', '--setting-sources', '',
            '--strict-mcp-config', '--mcp-config', r.mcp_json, '--no-session-persistence',
            '--permission-mode', 'acceptEdits', '--max-turns', str(r.max_turns()),
            '--allowedTools', *CLAUDE_TOOLS, *([f'Bash({r.python} -m pytest:*)'] if r.python else [])]


def _toml(value):
    """A JSON string or array of strings is also a valid TOML value for codex -c."""
    return json.dumps(value, ensure_ascii=False)


def _codex_jarvis(mcp_json):
    """-c overrides giving a work run the jarvis board tools only, from the same server spec Claude gets.
    The user config is ignored, so these per-tool approvals replace the ones in ~/.codex/config.toml."""
    try:
        server = json.loads(mcp_json)['mcpServers']['jarvis']
        command, args, env = server['command'], server.get('args', []), server.get('env', {})
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(command, str) or not all(isinstance(item, str) for item in args) or not isinstance(env, dict):
        return []
    argv = ['-c', 'mcp_servers.jarvis.command=' + _toml(command), '-c', 'mcp_servers.jarvis.args=' + _toml(args)]
    for key, value in env.items():
        if isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_]+', key) and isinstance(value, str):  # TOML bare key
            argv += ['-c', f'mcp_servers.jarvis.env.{key}=' + _toml(value)]
    argv += ['-c', 'mcp_servers.jarvis.enabled_tools=' + _toml([f'board_{name}' for name in BOARD_TOOLS])]
    for name in BOARD_TOOLS:
        argv += ['-c', f'mcp_servers.jarvis.tools.board_{name}.approval_mode="approve"']
    return argv


def _work_command_codex(r):
    # Unattended (-a never), so nothing beyond the worktree tools may load: no user config (its plugins, other
    # MCP servers and notify hook), the proposal mode's feature list minus the shell it needs for tests and
    # commits, and jarvis with board tools only. The Windows sandbox level the user config set is kept.
    # Sandbox writes stay inside the worktree plus its git metadata; pushes are done by the dispatcher.
    disabled = [part for feature in CODEX_WORK_DISABLED_FEATURES for part in ('--disable', feature)]
    return [r.executable, '-a', 'never', 'exec', '--ignore-user-config', '-C', r.worktree, '--sandbox', 'workspace-write',
            '-c', 'windows.sandbox="elevated"', '--skip-git-repo-check', '--json', '--color', 'never',
            *disabled, *_codex_jarvis(r.mcp_json), *(['--add-dir', str(r.git_dir)] if r.git_dir else []), r.prompt]


def _work_command_cursor(r):
    # Probation: no --force, so Cursor proposes and reviews but cannot apply edits.
    return [r.executable, '-p', '--approve-mcps', '--trust', '--output-format', 'json',
            '--workspace', r.worktree, r.prompt]


def _work_command_opencode(r):
    # Standalone server so the project config (MCP, permissions) applies; default agent, because a custom
    # restricted agent made the free tier answer 403 FreeTierError.
    return [r.executable, 'run', '--standalone', '-m', r.model() or OPENCODE_DEFAULT_MODEL, '--format', 'json', r.prompt]


def _work_env_opencode(project_root, workspace):
    python = Path(project_root) / '.venv' / 'Scripts' / 'python.exe'
    shell = {'*': 'deny', 'git status *': 'allow', 'git diff *': 'allow', 'git log *': 'allow'}
    config = {
        # Defaults per opencode.ai/docs/permissions: most keys allow, external_directory ask. Deny explicitly what a
        # probation run must never reach: paths outside the worktree, the web, and subagents (board 100bb3a1).
        # 'shell' is the current docs name; 'bash' is kept because v2.0.32's schema still lists it.
        'permission': {'edit': 'deny', 'webfetch': 'deny', 'bash': shell, 'shell': dict(shell),
                       'external_directory': 'deny', 'websearch': 'deny', 'task': 'deny'},
        'mcp': {'jarvis': {'type': 'local', 'command': [str(python), str(Path(project_root) / 'server.py')],
                           'environment': {'JARVIS_WORKSPACE': str(workspace)}, 'enabled': True}},
        # Verified 2026-10-21: the underscore glob hides every other jarvis tool (privacy), the dot glob does not.
        'tools': {'jarvis_*': False, 'jarvis_board_*': True},
    }
    return {'OPENCODE_CONFIG_CONTENT': json.dumps(config)}


def _no_env(project_root, workspace):
    return {}


# --- registry -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerAdapter:
    employee: str
    applies_edits: bool                       # False: work mode can only propose and review (probation)
    discover_work: Callable[[], list | None]
    work_command: Callable[[WorkRequest], list]
    work_env: Callable[..., dict] = _no_env
    discover_proposal: Callable[[], str | None] | None = None
    proposal_command: Callable[..., list] | None = None
    workload_name: str | None = None          # name used by workload_policy, if any


ADAPTERS = {
    'codex': WorkerAdapter('codex', True, _work_codex, _work_command_codex,
                           discover_proposal=lambda: shutil.which('codex'), proposal_command=_proposal_codex,
                           workload_name='codex'),
    'claude-code': WorkerAdapter('claude-code', True, _work_claude, _work_command_claude,
                                 discover_proposal=discover_claude_native, proposal_command=_proposal_claude,
                                 workload_name='claude'),
    'cursor': WorkerAdapter('cursor', False, _work_cursor, _work_command_cursor),
    'opencode': WorkerAdapter('opencode', False, _work_opencode, _work_command_opencode, work_env=_work_env_opencode),
}
EMPLOYEES = tuple(ADAPTERS)
PROPOSAL_EMPLOYEES = tuple(name for name, item in ADAPTERS.items() if item.proposal_command)


def adapter(employee):
    try:
        return ADAPTERS[employee]
    except KeyError:
        raise ValueError(f'No headless command for {employee}') from None


READ_ONLY_BASH = frozenset({'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)'})
WRITE_TOOLS = frozenset({'Edit', 'Write', 'MultiEdit', 'NotebookEdit'})
EDIT_PERMISSION_MODES = frozenset({'acceptEdits', 'bypassPermissions'})
FORCE_FLAGS = ('--force', '-f', '--yolo', '--dangerously-skip-permissions', '--dangerously-bypass-approvals-and-sandbox')


def _probe(employee):
    """The real work argv and env this adapter would launch, with placeholder inputs."""
    item = adapter(employee)
    request = WorkRequest('WORKTREE', 'PROMPT', '{}', 'EXECUTABLE', max_turns=lambda: 1, model=lambda: '',
                          git_dir='GIT_DIR', python='PYTHON')
    return [str(part) for part in item.work_command(request)], item.work_env('PROJECT_ROOT', 'WORKSPACE')


def _name(part):
    """Option name of an argv item: '--flag=value' -> '--flag'; non-options are returned unchanged."""
    return part.partition('=')[0] if part.startswith('-') else part


def _values(argv, flag):
    """Every value given to flag, as '--flag value' or '--flag=value' (None when the value is missing)."""
    values = []
    for index, part in enumerate(argv):
        if part.startswith('-') and _name(part) == flag:
            if '=' in part:
                values.append(part.partition('=')[2])
            else:
                values.append(argv[index + 1] if index + 1 < len(argv) else None)
    return values


def _allowed_tools(argv):
    tools = []
    for index, part in enumerate(argv):
        if part.startswith('-') and _name(part) == '--allowedTools':
            listed = [part.partition('=')[2]] if '=' in part else []
            for following in argv[index + 1:]:
                if following.startswith('-'):
                    break
                listed.append(following)
            # Commas only: tool patterns such as 'Bash(git status:*)' contain spaces.
            tools += [tool.strip() for item in listed for tool in item.split(',') if tool.strip()]
    return tools


def edit_markers(employee):
    """What in the employee's real work command or env lets it change files; [] means read-only.

    Flags match in both '--flag value' and '--flag=value' forms. Unknown cases fail closed."""
    argv, env = _probe(employee)
    names = {_name(part) for part in argv if part.startswith('-')}
    markers = [flag for flag in FORCE_FLAGS if flag in names]
    markers += [f'--permission-mode {mode}' for mode in _values(argv, '--permission-mode') if mode in EDIT_PERMISSION_MODES]
    writers = [tool for tool in _allowed_tools(argv)
               if tool in WRITE_TOOLS or (tool.startswith('Bash(') and tool not in READ_ONLY_BASH)]
    if writers:
        markers.append('write tools ' + ', '.join(writers))
    markers += [f'--sandbox {sandbox}' for sandbox in _values(argv, '--sandbox') if sandbox != 'read-only']
    if employee == 'opencode' and '--agent' in names:
        # Not verified whether an agent can override the inline permission config, so treat it as edit-capable.
        markers.append('opencode --agent (may override the permission config)')
    raw = env.get('OPENCODE_CONFIG_CONTENT')
    if raw is None and employee == 'opencode':
        markers.append('opencode config missing (CLI defaults allow edits)')
    elif raw is not None:
        try:
            permission = json.loads(raw).get('permission') or {}
            shells = {key: _rule_all(permission[key]) for key in ('bash', 'shell') if key in permission}
        except (ValueError, AttributeError):
            markers.append('opencode config unreadable')
        else:
            if permission.get('edit') != 'deny':
                markers.append(f"opencode permission.edit={permission.get('edit')!r}")
            if not shells:
                markers.append("opencode permission.bash['*']=None")   # no shell rule: the CLI default allows
            markers += [f"opencode permission.{key}['*']={value!r}" for key, value in shells.items() if value != 'deny']
    return markers


def _rule_all(rule):
    return rule if isinstance(rule, str) else rule.get('*') if isinstance(rule, dict) else None


PRIVACY_DENY = ('external_directory', 'websearch', 'webfetch', 'task')


def privacy_markers(employee):
    """What in an OpenCode-style config lets a run read outside its worktree, reach the web, delegate, or use
    jarvis tools beyond the board; [] means closed. Other employees carry no such config and report []."""
    _, env = _probe(employee)
    raw = env.get('OPENCODE_CONFIG_CONTENT')
    if raw is None:
        return ['opencode config missing (CLI defaults allow reads outside and the web)'] if employee == 'opencode' else []
    try:
        config = json.loads(raw)
        permission, tools = config.get('permission') or {}, config.get('tools') or {}
        markers = [f'opencode permission.{key}={permission.get(key)!r}' for key in PRIVACY_DENY
                   if permission.get(key) != 'deny']
        opened = [name for name, enabled in tools.items() if enabled is True and not name.startswith('jarvis_board_')]
    except (ValueError, AttributeError):
        return ['opencode config unreadable']
    if tools.get('jarvis_*') is not False:
        markers.append(f"opencode tools.jarvis_*={tools.get('jarvis_*')!r}")
    markers += [f'opencode tools.{name} enabled' for name in opened if name.startswith('jarvis')]
    return markers


# Problem kinds between an adapter and the roster. Codes carry no command fragments, so user-facing
# reports (the daily digest) can show them; roster_problems() keeps the detailed text for the service log.
PROBLEM_CODES = ('no_roster', 'retired', 'probation_can_edit', 'label_mismatch', 'privacy_open')


def _roster_findings(roster):
    """{employee: [(code, detail text), ...]}; the single place that judges adapters against the roster."""
    findings = {}
    for name, item in ADAPTERS.items():
        agent = roster.agents.get(name)
        found = []
        if agent is None:
            found.append(('no_roster', f'{name}: adapter without a roster entry'))
        elif agent.status == 'retired':
            found.append(('retired', f'{name}: adapter for a retired employee'))
        else:
            markers = edit_markers(name)
            if agent.status == 'probation' and (markers or item.applies_edits):
                found.append(('probation_can_edit', f'{name}: probation employee with an edit-capable work command'
                              + (f' ({"; ".join(markers)})' if markers else '')))
            elif markers and not item.applies_edits:
                found.append(('label_mismatch',
                              f'{name}: applies_edits=False but the work command can edit ({"; ".join(markers)})'))
            # Separate from editing, and for active employees too: personal data must not leave via a free API.
            privacy = privacy_markers(name)
            if privacy:
                found.append(('privacy_open', f'{name}: reads outside, web or extra tools not denied '
                                              f'({"; ".join(privacy)})'))
        if found:
            findings[name] = found
    return findings


def roster_problems(roster):
    """{employee: [problem, ...]} between adapters and the roster; employees without problems are absent.

    Probation is checked against the real argv and env (edit_markers), not the applies_edits label."""
    return {name: [text for _, text in found] for name, found in _roster_findings(roster).items()}


def roster_problem_codes(roster):
    """{employee: [code, ...]} with codes from PROBLEM_CODES; same judgment as roster_problems."""
    return {name: [code for code, _ in found] for name, found in _roster_findings(roster).items()}


def check_roster(roster):
    """Flat list of roster_problems; an empty list means adapters and roster agree."""
    return [problem for found in roster_problems(roster).values() for problem in found]


def employed(roster):
    """Adapter employees the roster currently employs (active or probation), in adapter order."""
    return tuple(name for name in EMPLOYEES
                 if name in roster.agents and roster.agents[name].status in ('active', 'probation'))
