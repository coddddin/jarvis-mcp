"""Fail-open Claude Code hook receiver. Prints nothing, always exits 0, keeps metadata only.

Silent stdout matters: on SessionStart and UserPromptSubmit, Claude Code adds plain-text hook stdout to the
conversation context, and exit code 2 would block the action.
"""
import json
from pathlib import Path
import sys

# Absolute script commands work even when Claude Code starts from a project subdirectory.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.agents.claude_session_signals import normalize_claude_event, render_claude_hooks, within_project
from jarvis.cli.session_signal import HookArguments, read_event
from jarvis.core.guardrails import Guardrails


def _report(workspace, signal):
    from jarvis.agents.session_status import SessionStatusStore
    SessionStatusStore(Guardrails(Path(workspace)), busy_timeout=0.2).report(signal)


def main(argv=None, *, stream=None, report=None):
    try:
        parser = HookArguments(description='JARVIS metadata-only Claude Code lifecycle observer.')
        parser.add_argument('--workspace', required=True)
        parser.add_argument('--project-root', required=True)
        parser.add_argument('--render-config', action='store_true')
        args = parser.parse_args(argv)
        if args.render_config:
            config = render_claude_hooks(python=Path(sys.executable), script=Path(__file__).resolve(),
                                         workspace=args.workspace, project_root=args.project_root)
            print(json.dumps(config, ensure_ascii=True, indent=2))
            return 0
        raw = read_event(stream if stream is not None else sys.stdin.buffer)
        if not within_project(raw, args.project_root):
            return 0
        signal = normalize_claude_event(raw)
        if signal is not None:
            (report or _report)(args.workspace, signal)
    except Exception:
        # Observation failures must never block, approve or rewrite a Claude Code action.
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
