"""Claude Code statusline script: store the 5-hour and weekly usage numbers, print one short line.

Claude Code runs the statusline command with JSON session data on stdin and shows what it prints. Only the
documented rate_limits windows are kept (W3, board fab78e52); transcript_path, cwd and the rest are ignored.
Installing it means adding a statusLine entry to the user's Claude Code settings, which needs the user's
approval; this script and --render-config never write settings.
"""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.agents.usage_collector import claude_statusline_sample, write_usage_sample
from jarvis.core.guardrails import Guardrails

MAX_INPUT_BYTES = 65536
FALLBACK = 'Claude 사용량 정보 없음'
UNSAFE = '"\'`$%&|<>^!;*?'


class _Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid statusline arguments.')


def _read(stream):
    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return None
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, RecursionError, OSError):
        return None


def line(sample):
    usage = sample['usage']
    text = f"Claude 5h {round(usage['primary']['usedPercent'])}%"
    if 'secondary' in usage:
        text += f" · 주간 {round(usage['secondary']['usedPercent'])}%"
    return text


def render_statusline(*, python, script, workspace):
    """A reviewable statusLine fragment. The statusline command runs in a shell, so every path is validated
    (absolute, existing, no shell metacharacters) and double-quoted. Nothing is installed."""
    paths = [Path(value) for value in (python, script, workspace)]
    for path in paths:
        text = str(path)
        if (not path.is_absolute() or not path.exists() or '..' in path.parts or text.startswith(('\\\\', '//'))
                or any(ord(c) < 32 or c in UNSAFE for c in text)):
            raise ValueError('Statusline paths must be existing safe absolute local paths.')
    python, script, workspace = paths
    if not python.is_file() or not script.is_file() or not workspace.is_dir():
        raise ValueError('Invalid statusline executable, script or workspace.')
    return {'statusLine': {'type': 'command', 'command': f'"{python}" "{script}" --workspace "{workspace}"'}}


def main(argv=None, *, stream=None):
    text = FALLBACK
    try:
        parser = _Arguments(description='JARVIS Claude Code usage statusline (numbers only).')
        parser.add_argument('--workspace', required=True)
        parser.add_argument('--render-config', action='store_true')
        args = parser.parse_args(argv)
        if args.render_config:
            text = json.dumps(render_statusline(python=Path(sys.executable), script=Path(__file__).resolve(),
                                                workspace=args.workspace), ensure_ascii=True, indent=2)
        else:
            sample = claude_statusline_sample(_read(stream if stream is not None else sys.stdin.buffer))
            if sample is not None:
                text = line(sample)
                write_usage_sample(Guardrails(Path(args.workspace)), 'claude-code', sample)
    except Exception:
        pass  # a statusline must always render; storage failures only lose this sample
    try:
        sys.stdout.reconfigure(encoding='utf-8')   # Windows pipes default to the ANSI code page
    except (AttributeError, ValueError, OSError):
        pass
    print(text, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
