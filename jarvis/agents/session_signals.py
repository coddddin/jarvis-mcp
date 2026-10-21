"""Normalize lifecycle metadata; never retain prompts, arguments or transcript data."""
from pathlib import Path
import re
from uuid import uuid4

from jarvis.core.redaction import redact

CODEX_EVENTS = {
    'SessionStart': 'session_started', 'UserPromptSubmit': 'turn_started',
    'PreToolUse': 'tool_started', 'PostToolUse': 'tool_finished',
    'PermissionRequest': 'permission_requested', 'Stop': 'turn_stopped',
    'Interrupt': 'interrupted', 'SessionEnd': 'session_ended',
}
SESSION_EVENTS = {'session_started', 'session_ended'}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}')


def _identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value)) and redact(value) == value


def normalize_codex_event(raw):
    """Unknown or incomplete events are ignored, not guessed from their contents."""
    if not isinstance(raw, dict):
        return None
    event = CODEX_EVENTS.get(raw.get('hook_event_name')) if isinstance(raw.get('hook_event_name'), str) else None
    if event is None or not _identifier(raw.get('session_id')):
        return None
    turn = None if event in SESSION_EVENTS else raw.get('turn_id')
    if event not in SESSION_EVENTS and not _identifier(turn):
        return None
    return {'employee': 'codex', 'session_id': raw['session_id'], 'event_id': uuid4().hex,
            'turn_id': turn, 'event': event, 'source': 'self_reported'}


def within_project(raw, project_root):
    """Use cwd only for scope checking. It must never enter the stored signal."""
    if not isinstance(raw, dict) or not isinstance(raw.get('cwd'), str):
        return False
    try:
        candidate, root = Path(raw['cwd']), Path(project_root)
        if not candidate.is_absolute() or not root.is_absolute() or '..' in candidate.parts:
            return False
        return candidate.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (ValueError, OSError, RuntimeError):
        return False


def render_codex_hooks(*, python, script, workspace, project_root):
    """Render a reviewable definition. Does not install or trust hooks."""
    paths = [Path(value) for value in (python, script, workspace, project_root)]
    for path in paths:
        value = str(path)
        if (not path.is_absolute() or not path.exists() or '..' in path.parts
                or value.startswith(('\\\\', '//'))
                or any(ord(c) < 32 or c in '\"\'`$%&|<>^!' for c in value)):
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
    # POSIX and Windows forms differ; all values are prevalidated and always quoted.
    arguments = [str(python), str(script), '--workspace', str(workspace), '--project-root', str(project_root)]
    windows = ' '.join('"' + value + '"' for value in arguments)
    posix = ' '.join("'" + value + "'" for value in arguments)
    return {'description': 'JARVIS lifecycle metadata only; review and trust before enabling.',
            'hooks': {name: [{'hooks': [{'type': 'command', 'command': posix,
                'commandWindows': windows, 'timeout': 2}]}] for name in CODEX_EVENTS}}
