"""The /voice WebSocket: same gate as the task stream, one client at a time, ops forwarded to the service."""
import threading

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.agents.models import TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails
from jarvis.features.voice.service import VoiceError

ORIGIN = 'http://127.0.0.1:3000'
TOKEN = 'x' * 43


class FakeVoice:
    def __init__(self):
        self.calls, self.listeners = [], set()
        self.refuse_listen = False
        self.cancelled = threading.Event()

    def subscribe(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def emit(self, event):
        for listener in list(self.listeners):
            listener(event)

    def status(self):
        return {'state': 'ready', 'error': None, 'device': 'cpu', 'model': 'fake', 'voice': 'fake'}

    def listen(self):
        self.calls.append('listen')
        if self.refuse_listen:
            raise VoiceError('not_ready')
        self.emit({'type': 'state', 'state': 'listening'})

    def stop_listening(self):
        self.calls.append('stop')

    def cancel(self):
        self.calls.append('cancel')
        self.cancelled.set()

    def hush(self):
        self.calls.append('hush')

    def speak(self, text, final=False):
        self.calls.append(('speak', text, final))
        return True


@pytest.fixture
def voice_app(tmp_path):
    tasks = TaskStore(Guardrails(tmp_path))
    task = tasks.create(TaskRequest(summary='voice route test'))
    tasks.interrupt(task.task_id, 'Cancelled', cancelled=True)
    voice = FakeVoice()
    app = create_event_app(tasks, token=TOKEN, task_ids=[task.task_id], origins=[ORIGIN], voice=voice)
    return TestClient(app, base_url=ORIGIN), voice


def connect(client, origin=ORIGIN, path='/voice', host='127.0.0.1:3000'):
    return client.websocket_connect(path, headers={'origin': origin, 'host': host})


def test_voice_route_exists_only_with_a_service(tmp_path):
    tasks = TaskStore(Guardrails(tmp_path))
    task = tasks.create(TaskRequest(summary='no voice'))
    app = create_event_app(tasks, token=TOKEN, task_ids=[task.task_id], origins=[ORIGIN])
    with TestClient(app, base_url=ORIGIN) as client, pytest.raises(WebSocketDisconnect):
        with connect(client) as socket:
            socket.receive_json()


@pytest.mark.parametrize('bad', ['origin', 'host', 'query', 'token', 'shape', 'silent'])
def test_voice_gate_refuses_other_pages_and_wrong_credentials(voice_app, bad):
    client, voice = voice_app
    with client, pytest.raises(WebSocketDisconnect):
        with connect(client, origin='https://attacker.invalid' if bad == 'origin' else ORIGIN,
                     path='/voice?token=' + TOKEN if bad == 'query' else '/voice',
                     host='attacker.invalid:3000' if bad == 'host' else '127.0.0.1:3000') as socket:
            if bad == 'token':
                socket.send_json({'token': 'y' * 43})
            elif bad == 'shape':
                socket.send_json({'token': TOKEN, 'op': 'listen'})
            elif bad == 'silent':
                pass                                        # no credentials within 3 s
            else:
                socket.send_json({'token': TOKEN})
            socket.receive_json()
    assert voice.calls == [] and not voice.listeners


def test_ops_reach_the_service_and_events_come_back(voice_app):
    client, voice = voice_app
    with client, connect(client) as socket:
        socket.send_json({'token': TOKEN})
        assert socket.receive_json() == {'type': 'hello', **voice.status()}
        socket.send_json({'op': 'listen'})
        assert socket.receive_json() == {'type': 'state', 'state': 'listening'}
        for op in ('stop', 'hush'):
            socket.send_json({'op': op})
        socket.send_json({'op': 'speak', 'text': '안녕하세요.', 'final': True})
        voice.emit({'type': 'transcript', 'text': '들었어요'})
        assert socket.receive_json() == {'type': 'transcript', 'text': '들었어요'}
        voice.refuse_listen = True
        socket.send_json({'op': 'listen'})
        assert socket.receive_json() == {'type': 'error', 'code': 'not_ready'}
    assert voice.calls[:4] == ['listen', 'stop', 'hush', ('speak', '안녕하세요.', True)]
    assert voice.cancelled.wait(2) and not voice.listeners      # leaving the page closes the mic


@pytest.mark.parametrize('message', [{'op': 'shout'}, {'op': 'listen', 'extra': 1}, {'op': 'speak', 'text': 'a'},
                                     {'op': 'speak', 'text': 1, 'final': True}, 'not json'])
def test_malformed_messages_close_the_connection(voice_app, message):
    client, voice = voice_app
    with client, connect(client) as socket:
        socket.send_json({'token': TOKEN})
        socket.receive_json()
        socket.send_text(message) if isinstance(message, str) else socket.send_json(message)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert voice.calls == ['cancel']


def test_a_new_page_replaces_the_old_one(voice_app):
    client, voice = voice_app
    with client, connect(client) as first:
        first.send_json({'token': TOKEN})
        first.receive_json()
        with connect(client) as second:
            second.send_json({'token': TOKEN})
            second.receive_json()
            with pytest.raises(WebSocketDisconnect):
                first.receive_json()
            assert 'cancel' not in voice.calls                  # only the current page's exit cancels
            voice.emit({'type': 'state', 'state': 'speaking'})
            assert second.receive_json() == {'type': 'state', 'state': 'speaking'}
    assert voice.cancelled.wait(2)
