"""Explicit operator-started, loopback-only, read-only task event service."""
import argparse

import uvicorn

from jarvis.agents.store import TaskStore
from jarvis.app.events import create_event_app, load_event_token
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import DEFAULT_WORKSPACE


def main():
    parser = argparse.ArgumentParser(description='Authenticated local task events; no command/model endpoints.')
    parser.add_argument('--task-id', action='append', required=True)
    parser.add_argument('--origin', action='append', default=None)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose an unprivileged local port.')
    store = TaskStore(Guardrails(DEFAULT_WORKSPACE))
    app = create_event_app(store, token=load_event_token(), task_ids=args.task_id,
                           origins=args.origin or [f'http://127.0.0.1:{args.port}', 'http://127.0.0.1:3000'])
    uvicorn.run(app, host='127.0.0.1', port=args.port, ws='websockets-sansio',
                ws_max_size=1024, ws_max_queue=16, limit_concurrency=32,
                ws_per_message_deflate=False,
                access_log=False, log_level='warning', server_header=False)


if __name__ == '__main__':
    main()
