"""Read-only console for one admitted request's external-worker observations."""
import argparse
import json
import os
import subprocess
import sys
import time
import unicodedata

from jarvis.agents.chat import ChatStore
from jarvis.app.runtime import load_guardrails
from jarvis.core.redaction import redact

MAX_BYTES = 262144
EVENTS = {'start', 'progress', 'result', 'failed', 'completed'}


def open_worker_view(guard, conversation, request):
    """Open an observer console, never another model invocation."""
    guard.check_running()
    ChatStore.identifier(conversation)
    ChatStore.identifier(request)
    if os.name != 'nt':
        raise OSError('Visible worker console is currently supported on Windows only.')
    from jarvis.core.paths import PROJECT_ROOT
    return subprocess.Popen([sys.executable, '-m', 'jarvis.cli.worker_view',
        '--conversation-id', conversation, '--request-id', request],
        cwd=PROJECT_ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True)


def read_events(guard, conversation, request):
    ChatStore.identifier(conversation)
    ChatStore.identifier(request)
    relative = f'.jarvis-chat/team-{conversation}-{request}/worker-progress.jsonl'
    path = guard.validate_path(relative, must_exist=False)
    if not path.exists():
        return []
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > MAX_BYTES:
        raise ValueError('Unsafe worker progress file.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Worker progress exceeds limit.')
    lines = raw.split(b'\n')[:-1]  # A writer may still be appending the last line.
    result = []
    for line in lines:
        if len(line) > 8192:
            raise ValueError('Oversized worker event.')
        record = json.loads(line)
        if (not isinstance(record, dict) or record.get('runtime') not in {'codex', 'claude-code'}
                or record.get('event') not in EVENTS or not isinstance(record.get('text'), str)
                or not isinstance(record.get('name'), str) or len(record['name']) > 80):
            raise ValueError('Invalid worker event.')
        text = redact(f"[{record['runtime']} / {record['name']} / {record['event']}] {record['text']}")
        result.append(''.join(c for c in text if c in '\n\t' or not unicodedata.category(c).startswith('C')))
    return result


def main():
    parser = argparse.ArgumentParser(description='실제 외부 작업 기록 표시. 호출·재실행 기능 없음.')
    parser.add_argument('--conversation-id', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--seconds', type=int, default=660)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 660:
        parser.error('seconds must be between 1 and 660')
    try:
        guard = load_guardrails()
        seen = 0
        deadline = time.monotonic() + args.seconds
        print('JARVIS: 실제 외부 CLI 작업 기록을 기다립니다. 이 창은 작업을 실행하지 않습니다.', flush=True)
        while time.monotonic() < deadline:
            guard.check_running()
            records = read_events(guard, args.conversation_id, args.request_id)
            if len(records) < seen:
                raise ValueError('Worker progress was truncated.')
            for record in records[seen:]:
                print(record, flush=True)
            seen = len(records)
            time.sleep(0.5)
        print('표시 기한 종료. 실제 작업의 완료 여부와는 별개입니다.', flush=True)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print('작업 기록 확인 불가: ' + type(exc).__name__, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
