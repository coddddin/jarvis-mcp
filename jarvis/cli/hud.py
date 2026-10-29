"""Operator-started local workspace with bounded chat and read-only task HUD."""
import argparse
import asyncio
import secrets
import webbrowser

import uvicorn

from jarvis.agents.activity import ActivityStore
from jarvis.agents.session_status import SessionStatusStore
from jarvis.agents.store import TaskStore
from jarvis.agents.api_brain import api_providers
from jarvis.agents.claude_brain import ClaudeCliProvider
from jarvis.agents.process_registry import ProcessRegistry
from jarvis.features.board.service import BoardStore
from jarvis.app.events import create_event_app, load_event_token
from jarvis.app.chat import ChatService
from jarvis.core.credentials import WindowsCredentials
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import CONFIG_ROOT, DEFAULT_WORKSPACE
from jarvis.features.google.service import GoogleBriefing


class HUDServer(uvicorn.Server):
    """Open (or hand to the desktop app) only after loopback binding succeeds, avoiding a startup race."""
    def __init__(self, config, *, open_url=None, on_start=None, shell_url=None):
        super().__init__(config)
        self.open_url = open_url
        self.on_start = on_start
        self.shell_url = shell_url

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started and self.on_start:
            self.on_start()
        if self.started and self.shell_url:
            # Read only by the desktop app through its stdout pipe; the one-time code is never written to a log.
            print(f'READY {self.shell_url}', flush=True)
        if self.started and self.open_url:
            try:
                await asyncio.to_thread(webbrowser.open, self.open_url)
            except OSError:
                print('Open the local workspace URL shown above in your browser.', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Local JARVIS chat and task workspace.')
    parser.add_argument('--task-id', action='append', help='Explicit task allowlist; defaults to latest 8 at startup.')
    parser.add_argument('--port', type=int, default=8787)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--open', action='store_true', help='Open the local page in the default browser.')
    mode.add_argument('--shell', action='store_true',
                      help='Started by the desktop app: print one READY line with the connect URL instead of opening a browser.')
    parser.add_argument('--model', default='qwen3.5:9b', help='Installed local Ollama model tag; no automatic downloads.')
    parser.add_argument('--voice', action='store_true',
                        help='Local push-to-talk and spoken replies (needs requirements-voice.txt; see docs/guides/voice.md).')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose an unprivileged local port.')
    store = TaskStore(Guardrails(DEFAULT_WORKSPACE))
    ids = args.task_id or [task.task_id for task in store.recent()]
    url = f'http://127.0.0.1:{args.port}'
    # The browser we open connects by itself with a one-time code in the URL fragment (browsers never send the
    # fragment to the server). The connection file stays the fallback.
    # The desktop app (`--shell`) gets the same one-time code on its private stdout pipe.
    code = secrets.token_urlsafe(32) if args.open or args.shell else None
    sessions = SessionStatusStore(store.guard)
    activity = ActivityStore(store.guard)
    chat = ChatService(store.guard, model=args.model, brains=[ClaudeCliProvider(store.guard),
                       *api_providers(store.guard, WindowsCredentials())])
    chat.team.activity_store = activity
    processes = ProcessRegistry(store.guard)
    processes.register_current(kind='hud', label='JARVIS HUD', max_runtime_seconds=604800)
    voice = None
    if args.voice:
        try:
            from jarvis.features.voice.service import build_default_service
            voice = build_default_service()
        except ImportError as exc:
            print(f'Voice is off: missing package {exc}. Install requirements-voice.txt (docs/guides/voice.md).',
                  flush=True)
    app = create_event_app(store, token=load_event_token(), task_ids=ids, origins=[url], chat=chat,
        processes=processes, board=BoardStore(store.guard), activity=activity, sessions=sessions,
        dispatcher=(store.guard, CONFIG_ROOT / 'dispatcher.json'), briefing=GoogleBriefing(),
        connect_code=code, voice=voice)
    print(f'J.A.R.V.I.S. local workspace: {url}', flush=True)
    print('Local chat enabled; task HUD read-only. Close this process to stop. Connection file: .jarvis-auth/agent-events-token.txt', flush=True)
    config = uvicorn.Config(app, host='127.0.0.1', port=args.port, ws='websockets-sansio',
                ws_max_size=1024, ws_max_queue=16, limit_concurrency=32,
                ws_per_message_deflate=False, access_log=False, log_level='warning', server_header=False)
    # Load the chat model in the background once the port is bound, so the first reply does not wait for it.
    # `--open` lands on the assistant view; its "개발자 화면" link goes to the HUD at / in the same tab and connection.
    target = f'{url}/assistant#connect={code}' if code else None

    def on_start():
        chat.warm_up()
        if voice is not None:
            prefetch_on_listen(voice, chat)
            voice.start()                                   # speech models load in the background
    try:
        HUDServer(config, open_url=target if args.open else None, on_start=on_start,
                  shell_url=target if args.shell else None).run()
    finally:
        if voice is not None:
            voice.close()                                   # closes the mic and ends the speech worker


def prefetch_on_listen(voice, chat):
    """Load the chat model again when the mic opens, so a model Ollama unloaded after 5 idle minutes (ADR 0001) is
    loading while the user speaks. Best effort: a failed warm-up never reaches the voice service."""
    def listener(event):
        if event.get('type') == 'state' and event.get('state') == 'listening':
            try:
                chat.warm_up()
            except Exception:
                pass
    return voice.subscribe(listener)


if __name__ == '__main__':
    main()
