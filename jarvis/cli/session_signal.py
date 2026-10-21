"""Fail-open Codex hook receiver. No hook stdout, prompt echo or transcript access."""
import argparse
import json
from pathlib import Path
import sys

# Absolute script commands work even when Codex starts from a project subdirectory.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.agents.session_signals import normalize_codex_event, render_codex_hooks, within_project
from jarvis.core.guardrails import Guardrails

MAX_INPUT_BYTES = 262144


class HookArguments(argparse.ArgumentParser):
    def error(self, message):
        # exit 2 could block a Codex operation; an observer must never do that.
        raise ValueError('Invalid hook receiver configuration.')


def read_event(stream):
    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
            return None
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError, OSError):
        return None


def _report(workspace, signal):
    # The shared store is implemented by the HUD owner; no parent PID registration.
    from jarvis.agents.session_status import SessionStatusStore
    SessionStatusStore(Guardrails(Path(workspace))).report(signal)


def main(argv=None, *, stream=None, report=None):
    try:
        parser = HookArguments(description='JARVIS metadata-only lifecycle observer.')
        parser.add_argument('--workspace', required=True)
        parser.add_argument('--project-root', required=True)
        parser.add_argument('--render-config', action='store_true')
        args = parser.parse_args(argv)
        if args.render_config:
            config = render_codex_hooks(python=Path(sys.executable), script=Path(__file__).resolve(),
                                        workspace=args.workspace, project_root=args.project_root)
            print(json.dumps(config, ensure_ascii=True))
            return 0
        raw = read_event(stream if stream is not None else sys.stdin.buffer)
        if not within_project(raw, args.project_root):
            return 0
        signal = normalize_codex_event(raw)
        if signal is not None:
            (report or _report)(args.workspace, signal)
    except Exception:
        # Observation failures must not block, continue, approve or rewrite an agent operation.
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
