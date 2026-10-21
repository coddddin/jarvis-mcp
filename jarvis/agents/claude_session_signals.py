"""Claude Code hooks -> the shared content-free session signal (board ff9bcf39).

Same contract as the Codex adapter in session_signals.py (imported, not changed): employee, session_id,
event_id, turn_id, event, source. Prompts, tool inputs, responses and transcripts are never kept.

Facts from the official hook reference (code.claude.com/docs/en/hooks, checked 2026-10-21):
- There is no turn_id. Every event carries the common field prompt_id, "the UUID of the user prompt being
  processed", absent until the first user input. It is used as turn_id; a missing or malformed value
  drops the event instead of guessing. Before enabling the hooks, confirm on one real session that
  prompt_id stays the same from UserPromptSubmit to Stop.
- No hook fires on a user interrupt (Stop "does not run if the stoppage occurred due to a user
  interrupt"), so 'interrupted' is not connected. StopFailure replaces Stop when a turn ends on an API
  error and is reported as turn_stopped.
"""
from pathlib import Path
from uuid import uuid4

from jarvis.agents.session_signals import IDENTIFIER, SESSION_EVENTS, within_project  # noqa: F401 - re-exported
from jarvis.core.redaction import redact

EMPLOYEE = 'claude-code'
CLAUDE_EVENTS = {
    'SessionStart': 'session_started', 'UserPromptSubmit': 'turn_started', 'PreToolUse': 'tool_started',
    'PostToolUse': 'tool_finished', 'PostToolUseFailure': 'tool_finished',
    'PermissionRequest': 'permission_requested', 'Stop': 'turn_stopped', 'StopFailure': 'turn_stopped',
    'SessionEnd': 'session_ended',
}
# Shared events with no official Claude Code hook. Stated here so nobody infers them.
UNMAPPED = {'interrupted': 'no hook fires on a user interrupt; Stop does not run then'}
HOOK_TIMEOUT_SECONDS = 5
UNSAFE_PATH_CHARS = '"\'`$%&|<>^!;*?'


def _identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value)) and redact(value) == value


def normalize_claude_event(raw):
    """The six-field signal for a supported hook input, or None. Unknown or incomplete input is ignored."""
    if not isinstance(raw, dict) or not isinstance(raw.get('hook_event_name'), str):
        return None
    event = CLAUDE_EVENTS.get(raw['hook_event_name'])
    if event is None or not _identifier(raw.get('session_id')):
        return None
    turn = None if event in SESSION_EVENTS else raw.get('prompt_id')
    if event not in SESSION_EVENTS and not _identifier(turn):
        return None
    return {'employee': EMPLOYEE, 'session_id': raw['session_id'], 'event_id': uuid4().hex,
            'turn_id': turn, 'event': event, 'source': 'self_reported'}


def _checked_paths(python, script, workspace, project_root):
    paths = [Path(value) for value in (python, script, workspace, project_root)]
    for path in paths:
        text = str(path)
        if (not path.is_absolute() or not path.exists() or '..' in path.parts or text.startswith(('\\\\', '//'))
                or any(ord(c) < 32 or c in UNSAFE_PATH_CHARS for c in text)):
            raise ValueError('Hook paths must be existing safe absolute local paths.')
        cursor = path
        while cursor.parent != cursor:
            if cursor.is_symlink() or cursor.is_junction():
                raise ValueError('Linked hook paths are forbidden.')
            cursor = cursor.parent
    python, script, workspace, project_root = paths
    if (not python.is_file() or not script.is_file() or not workspace.is_dir() or not project_root.is_dir()
            or python.stat().st_nlink != 1 or script.stat().st_nlink != 1):
        raise ValueError('Invalid hook executable, script or directory.')
    return paths


def render_claude_hooks(*, python, script, workspace, project_root):
    """A reviewable settings fragment. Does not install anything; installing needs the user's approval.

    Exec form (command + args) runs without a shell, so no quoting differs between Windows and POSIX.
    async: an observer never makes Claude wait (SessionEnd hooks have a 1.5 s default budget)."""
    python, script, workspace, project_root = _checked_paths(python, script, workspace, project_root)
    handler = {'type': 'command', 'command': str(python),
               'args': [str(script), '--workspace', str(workspace), '--project-root', str(project_root)],
               'async': True, 'timeout': HOOK_TIMEOUT_SECONDS}
    return {'description': 'JARVIS lifecycle metadata only (claude-code); review before enabling. '
                           'Not connected: interrupted (no official hook).',
            'hooks': {name: [{'hooks': [dict(handler)]}] for name in CLAUDE_EVENTS}}
