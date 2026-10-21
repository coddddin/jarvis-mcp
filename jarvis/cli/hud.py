"""Operator-started local workspace with bounded chat and read-only task HUD."""
import argparse
import asyncio
import webbrowser

import uvicorn

from jarvis.agents.activity import ActivityStore
from jarvis.agents.store import TaskStore
from jarvis.agents.process_registry import ProcessRegistry
from jarvis.features.board.service import BoardStore
from jarvis.app.events import create_event_app, load_event_token
from jarvis.app.chat import ChatService
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import DEFAULT_WORKSPACE


class HUDServer(uvicorn.Server):
    """Open only after loopback binding succeeds, avoiding a browser startup race."""
    def __init__(self, config, *, open_url=None):
        super().__init__(config)
        self.open_url = open_url

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started and self.open_url:
            try:
                await asyncio.to_thread(webbrowser.open, self.open_url)
            except OSError:
                print('Open the local workspace URL shown above in your browser.', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Local JARVIS chat and task workspace.')
    parser.add_argument('--task-id', action='append', help='Explicit task allowlist; defaults to latest 8 at startup.')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--open', action='store_true', help='Open the local page in the default browser.')
    parser.add_argument('--model', default='qwen2.5-coder:7b', help='Installed local Ollama model tag; no automatic downloads.')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose an unprivileged local port.')
    store = TaskStore(Guardrails(DEFAULT_WORKSPACE))
    ids = args.task_id or [task.task_id for task in store.recent()]
    url = f'http://127.0.0.1:{args.port}'
    activity = ActivityStore(store.guard)
    chat = ChatService(store.guard, model=args.model)
    chat.team.activity_store = activity
    processes = ProcessRegistry(store.guard)
    processes.register_current(kind='hud', label='JARVIS HUD', max_runtime_seconds=604800)
    app = create_event_app(store, token=load_event_token(), task_ids=ids, origins=[url], chat=chat,
        processes=processes, board=BoardStore(store.guard), activity=activity)
    print(f'J.A.R.V.I.S. local workspace: {url}', flush=True)
    print('Local chat enabled; task HUD read-only. Close this process to stop. Connection file: .jarvis-auth/agent-events-token.txt', flush=True)
    config = uvicorn.Config(app, host='127.0.0.1', port=args.port, ws='websockets-sansio',
                ws_max_size=1024, ws_max_queue=16, limit_concurrency=32,
                ws_per_message_deflate=False, access_log=False, log_level='warning', server_header=False)
    HUDServer(config, open_url=url if args.open else None).run()


if __name__ == '__main__':
    main()
