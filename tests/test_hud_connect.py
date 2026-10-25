"""HUD auto-connect: the one-time code that `hud --open` puts in the URL fragment."""
import json

import pytest
from starlette.testclient import TestClient

from jarvis.agents.models import TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.app import events
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails

ORIGIN = 'http://127.0.0.1:8787'
TOKEN, CODE = 'x' * 43, 'c' * 43
POST = {'Origin': ORIGIN, 'Content-Type': 'application/json'}


def app(tmp_path, **kwargs):
    store = TaskStore(Guardrails(tmp_path))
    task = store.create(TaskRequest(summary='Selected local task'))
    return TestClient(create_event_app(store, token=TOKEN, task_ids=[task.task_id], origins=[ORIGIN], **kwargs),
                      base_url=ORIGIN)


# --- one-time connect code ---

def test_code_is_traded_for_the_token_exactly_once(tmp_path):
    client = app(tmp_path, connect_code=CODE)
    first = client.post('/api/connect', json={'code': CODE}, headers=POST)
    assert first.status_code == 200 and first.json() == {'token': TOKEN}
    assert first.headers['cache-control'] == 'no-store'
    assert client.post('/api/connect', json={'code': CODE}, headers=POST).status_code == 410


def test_no_code_configured_means_no_token(tmp_path):
    client = app(tmp_path)
    response = client.post('/api/connect', json={'code': CODE}, headers=POST)
    assert response.status_code == 410 and TOKEN not in response.text


@pytest.mark.parametrize('headers, body, status', [
    ({'Content-Type': 'application/json'}, {'code': CODE}, 403),                  # Origin required on POST
    ({**POST, 'Origin': 'https://example.org'}, {'code': CODE}, 403),
    ({**POST, 'Content-Type': 'text/plain'}, {'code': CODE}, 415),
])
def test_connect_requires_local_origin_and_json(tmp_path, headers, body, status):
    client = app(tmp_path, connect_code=CODE)
    response = client.post('/api/connect', content=json.dumps(body), headers=headers)
    assert response.status_code == status and TOKEN not in response.text
    assert client.post('/api/connect', json={'code': CODE}, headers=POST).status_code == 200   # code not spent


def test_query_string_and_large_bodies_are_refused(tmp_path):
    client = app(tmp_path, connect_code=CODE)
    assert client.post('/api/connect?code=' + CODE, json={'code': CODE}, headers=POST).status_code == 403
    assert client.post('/api/connect', content='{"code":"' + 'c' * 400 + '"}', headers=POST).status_code == 413


def test_wrong_guesses_burn_the_code(tmp_path):
    client = app(tmp_path, connect_code=CODE)
    for wrong in [{'code': 'd' * 43}, {'code': 'short'}, {'code': CODE, 'extra': 1}, [CODE], {}]:
        assert client.post('/api/connect', json=wrong, headers=POST).status_code == 401
    assert client.post('/api/connect', json={'code': CODE}, headers=POST).status_code == 410


def test_code_expires(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(events.time, 'monotonic', lambda: clock[0])
    client = app(tmp_path, connect_code=CODE)
    clock[0] += events.CONNECT_SECONDS + 1
    assert client.post('/api/connect', json={'code': CODE}, headers=POST).status_code == 410


@pytest.mark.parametrize('code', ['short', 'c' * 42 + '!', 123])
def test_weak_codes_are_refused_at_startup(tmp_path, code):
    with pytest.raises(ValueError):
        app(tmp_path, connect_code=code)


def test_hud_opens_the_browser_with_a_fresh_code_in_the_fragment_only(monkeypatch):
    from jarvis.cli import hud
    captured = {}

    class Server:
        def __init__(self, config, *, open_url=None, on_start=None):
            captured['open_url'] = open_url

        def run(self):
            pass

    def fake_app(*args, **kwargs):
        captured['code'] = kwargs.get('connect_code')
        return object()

    class Chat:
        def __init__(self, *args, **kwargs):
            self.team = type('Team', (), {})()

        def warm_up(self):
            pass

    for name, value in {'HUDServer': Server, 'create_event_app': fake_app, 'ChatService': Chat,
                        'load_event_token': lambda: TOKEN, 'TaskStore': lambda guard: type('S', (), {
                            'guard': guard, 'recent': lambda self: []})(),
                        'SessionStatusStore': lambda guard: None, 'ActivityStore': lambda guard: None,
                        'ProcessRegistry': lambda guard: type('P', (), {'register_current': lambda self, **k: None})(),
                        'BoardStore': lambda guard: None, 'GoogleBriefing': lambda: None,
                        'Guardrails': lambda root: None}.items():
        monkeypatch.setattr(hud, name, value)
    monkeypatch.setattr('sys.argv', ['hud', '--open'])
    hud.main()
    code = captured['code']
    assert isinstance(code, str) and len(code) == 43 and code != TOKEN
    assert captured['open_url'] == 'http://127.0.0.1:8787/#connect=' + code
    monkeypatch.setattr('sys.argv', ['hud'])
    hud.main()
    assert captured['code'] is None and captured['open_url'] is None
