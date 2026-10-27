"""Assistant view <-> local voice service over one WebSocket at /voice.

Same gate as the task stream: exact local Origin, matching Host, no query, and the HUD token as the first message
({"token": ...}, within 3 s). One client at a time: a new connection (for example a reload) replaces the old one.
When the client goes away the service cancels, so the mic never stays open without a page.

Client -> server, one JSON object each (the server caps messages at 1 KB):
  {"op": "listen"}            open the mic (stops speaking first)
  {"op": "stop"}              end push-to-talk; a transcript follows
  {"op": "cancel"}            Esc: close the mic without transcribing, stop speaking
  {"op": "speak", "text": str (<= 300 chars), "final": bool}   reply text, sentence by sentence
  {"op": "hush"}              stop speaking, keep listening state as is
Server -> client: {"type": "hello", state, device, model, voice}, then the service's own events
(state, transcript, frame, error). Frames are dropped, never queued, when the client is slow.
"""
import asyncio
import json
from urllib.parse import urlsplit

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from jarvis.features.voice.service import VoiceError

OPS = {'listen', 'stop', 'cancel', 'speak', 'hush'}
FRAME_BACKLOG = 8


def voice_routes(service, *, valid, origins):
    hosts = {urlsplit(item).netloc for item in origins}
    current = {'socket': None}

    async def refuse(websocket):
        try:
            await websocket.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def run(op, message):
        if op == 'listen':
            await asyncio.to_thread(service.listen)
        elif op == 'stop':
            await asyncio.to_thread(service.stop_listening)
        elif op == 'cancel':
            await asyncio.to_thread(service.cancel)
        elif op == 'hush':
            await asyncio.to_thread(service.hush)
        else:
            await asyncio.to_thread(service.speak, message['text'], message['final'])

    def well_formed(message):
        if not isinstance(message, dict) or message.get('op') not in OPS:
            return False
        if message['op'] == 'speak':
            return (set(message) == {'op', 'text', 'final'} and isinstance(message['text'], str)
                    and isinstance(message['final'], bool))
        return set(message) == {'op'}

    async def voice(websocket):
        if (websocket.headers.get('origin') not in origins or websocket.headers.get('host') not in hosts
                or websocket.url.query):
            await refuse(websocket)
            return
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=3)
            credentials = json.loads(raw) if len(raw) <= 128 else None
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, RuntimeError):
            await refuse(websocket)
            return
        if not isinstance(credentials, dict) or set(credentials) != {'token'} or not valid(credentials['token']):
            await refuse(websocket)
            return

        previous, current['socket'] = current['socket'], websocket
        if previous is not None:
            await refuse(previous)
        loop = asyncio.get_running_loop()
        outbox: asyncio.Queue = asyncio.Queue()

        def deliver(event):
            if event.get('type') == 'frame' and outbox.qsize() >= FRAME_BACKLOG:
                return                                      # a slow page skips frames, never states
            outbox.put_nowait(event)

        unsubscribe = service.subscribe(lambda event: loop.call_soon_threadsafe(deliver, event))

        async def send():
            await websocket.send_json({'type': 'hello', **service.status()})
            while True:
                await websocket.send_json(await outbox.get())

        sender = asyncio.create_task(send())
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except ValueError:
                    message = None
                if not well_formed(message):
                    await refuse(websocket)
                    break
                try:
                    await run(message['op'], message)
                except VoiceError as exc:
                    deliver({'type': 'error', 'code': exc.code})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            unsubscribe()
            sender.cancel()
            if current['socket'] is websocket:
                current['socket'] = None
                # No page, no open mic and no speech. Called directly (it is quick) so a cancelled handler, for
                # example during server shutdown, cannot skip it.
                service.cancel()

    return [WebSocketRoute('/voice', voice)]
