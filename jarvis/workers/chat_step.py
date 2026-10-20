"""Fixed local conversation inference worker. No tool execution or credential access."""
import json
import importlib.util
from pathlib import Path
import sys

# -I deliberately removes the script directory from import search. Load only this
# fixed, trusted sibling to share the existing offline-model checks.
spec = importlib.util.spec_from_file_location('local_agent_step', Path(__file__).with_name('agent_step.py'))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Chat context too large.')
        payload = json.loads(raw)
        support.local_model(payload['model'])
        data = support.request('/api/chat', {'model': payload['model'], 'stream': False,
            'format': payload['schema'], 'messages': payload['messages'],
            'options': {'temperature': 0.2, 'num_predict': 1024, 'num_ctx': 8192}, 'keep_alive': '2m'})
        message = data['message']
        if (data.get('done') is not True or data.get('done_reason') == 'length'
                or message.get('role') != 'assistant' or message.get('tool_calls')):
            raise ValueError('Incomplete or unexpected model response.')
        result = json.loads(message['content'])
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
