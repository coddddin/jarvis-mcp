"""HUD server for a native shell (spike): binds, prints `READY <url>` with a one-time connect code, and records
when the page exchanged the code and first listed conversations, so a driver can time auto connect."""
import argparse
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8797)
    parser.add_argument('--events', help='JSON lines file for timing events')
    args = parser.parse_args()
    import uvicorn
    from jarvis.agents.models import TaskRequest
    from jarvis.agents.store import TaskStore
    from jarvis.app.chat import ChatService
    from jarvis.app.events import create_event_app
    from jarvis.core.guardrails import Guardrails

    def record(name):
        if args.events:
            with open(args.events, 'a', encoding='utf-8') as out:
                out.write(json.dumps({'event': name, 'time': time.time()}) + '\n')

    store = TaskStore(Guardrails(Path(tempfile.mkdtemp(prefix='jarvis-shell-'))))
    task = store.create(TaskRequest(summary='Spike task'))
    url, code = f'http://127.0.0.1:{args.port}', secrets.token_urlsafe(32)
    app = create_event_app(store, token=secrets.token_urlsafe(32), task_ids=[task.task_id], origins=[url],
                           chat=ChatService(store.guard), connect_code=code)

    async def timing(scope, receive, send):
        if scope['type'] == 'http':
            if scope['path'] == '/api/connect':
                record('connect')
            elif scope['path'] == '/api/chat/conversations' and scope['method'] == 'GET':
                record('listing')
        await app(scope, receive, send)

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            record('ready')
            print(f'READY {url}/assistant#connect={code}', flush=True)

    Server(uvicorn.Config(timing, host='127.0.0.1', port=args.port, log_level='warning', access_log=False)).run()


if __name__ == '__main__':
    main()
