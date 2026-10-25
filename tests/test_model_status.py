"""HUD Ollama card: fixed /api/tags probe, fixed codes, cache and the token gate. No real network or model."""
import http.client
import json
import socket
import threading
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jarvis.app import model_status
from jarvis.app.model_status import (CACHE_SECONDS, MAX_BYTES, ModelStatusObserver, model_status_routes,
                                     observer_for, probe_tags)

TOKEN = 'x' * 43
ORIGIN = 'http://127.0.0.1:8787'
AUTH = {'Authorization': 'Bearer ' + TOKEN}
MODEL = 'qwen2.5-coder:7b'


def tags(*names):
    return json.dumps({'models': [{'name': name, 'digest': 'secret-digest', 'size': 1} for name in names]}).encode()


class FakeResponse:
    def __init__(self, status=200, body=b'', headers=None):
        self.status, self.body, self.headers, self.reads = status, body, headers or {}, []

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, amount):
        self.reads.append(amount)
        return self.body[:amount]


class FakeConnection:
    """Stands in for http.client.HTTPConnection; records every call and whether it was closed."""

    def __init__(self, response=None, error=None, fail_at='getresponse'):
        self.response, self.error, self.fail_at = response, error, fail_at
        self.opened, self.requests, self.closed = None, [], False

    def __call__(self, host, port, timeout):
        self.opened = (host, port, timeout)
        return self

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        if self.error and self.fail_at == 'request':
            raise self.error

    def getresponse(self):
        if self.error and self.fail_at == 'getresponse':
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def run(response=None, **kwargs):
    connection = FakeConnection(response, **kwargs)
    return probe_tags(MODEL, connect=connection), connection


def test_matching_model_is_installed_via_one_fixed_get():
    result, connection = run(FakeResponse(body=tags('llama3:8b', MODEL)))
    assert result == ('responding', True, 'model_listed')
    assert connection.opened == ('127.0.0.1', 11434, 2) and connection.opened[2] <= 2
    assert len(connection.requests) == 1
    method, path, body, headers = connection.requests[0]
    assert (method, path, body) == ('GET', '/api/tags', None)
    assert headers['Accept-Encoding'] == 'identity' and 'Authorization' not in headers
    assert connection.closed


def test_missing_model_is_responding_but_not_installed():
    result, connection = run(FakeResponse(body=tags('llama3:8b')))
    assert result == ('responding', False, 'model_not_listed') and connection.closed
    result, _ = run(FakeResponse(body=tags()))
    assert result == ('responding', False, 'model_not_listed')


@pytest.mark.parametrize('body', [b'not json', b'[]', b'{"models": "x"}', b'{"models": [{"name": 1}]}',
                                  b'{"models": [3]}', b'{}', b'\xff\xfe'])
def test_malformed_tags_are_unknown(body):
    result, connection = run(FakeResponse(body=body))
    assert result == ('unknown', None, 'malformed_response') and connection.closed


def test_oversized_response_is_unknown_and_read_is_bounded():
    response = FakeResponse(body=b' ' * (MAX_BYTES + 1))
    result, connection = run(response)
    assert result == ('unknown', None, 'response_too_large') and connection.closed
    assert response.reads == [MAX_BYTES + 1] and MAX_BYTES <= 262144


@pytest.mark.parametrize('status', [204, 301, 302, 404, 500])
def test_non_200_is_unknown_and_never_followed(status):
    response = FakeResponse(status=status, body=tags(MODEL), headers={'Location': 'http://example.org/'})
    result, connection = run(response)
    assert result == ('unknown', None, 'http_error') and connection.closed
    assert len(connection.requests) == 1 and response.reads == []


@pytest.mark.parametrize('encoding', ['gzip', 'br', 'deflate'])
def test_non_identity_encoding_is_unknown(encoding):
    result, connection = run(FakeResponse(body=tags(MODEL), headers={'Content-Encoding': encoding}))
    assert result == ('unknown', None, 'unsupported_encoding') and connection.closed


def test_refused_connection_is_refused():
    result, connection = run(error=ConnectionRefusedError('refused'), fail_at='request')
    assert result == ('connection_refused', None, 'connection_refused') and connection.closed


@pytest.mark.parametrize('error', [TimeoutError('slow'), socket.timeout('slow')])
def test_timeout_is_unknown(error):
    result, connection = run(error=error)
    assert result == ('unknown', None, 'timeout') and connection.closed


@pytest.mark.parametrize('error', [ConnectionResetError('reset'), http.client.RemoteDisconnected('closed'),
                                   http.client.BadStatusLine('x'), OSError('other')])
def test_connection_errors_are_unknown_and_closed(error):
    result, connection = run(error=error)
    assert result == ('unknown', None, 'connection_error') and connection.closed


def test_unexpected_error_still_closes_connection():
    connection = FakeConnection(error=RuntimeError('boom'))
    with pytest.raises(RuntimeError):
        probe_tags(MODEL, connect=connection)
    assert connection.closed


class CountingProbe:
    def __init__(self, result=('responding', True, 'model_listed'), error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, model):
        self.calls.append(model)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.parametrize('model', ['bad model', 'qwen:cloud', 'gpt-oss:120b-cloud', 'no-tag', '', None, 7])
def test_invalid_or_cloud_model_fails_closed_without_probe(model):
    probe = CountingProbe()
    with pytest.raises(ValueError):
        ModelStatusObserver(model, probe=probe)
    assert probe.calls == []
    assert observer_for(SimpleNamespace(model=model)) is None


def test_observer_for_needs_chat_with_model():
    assert observer_for(None) is None and observer_for(object()) is None
    assert observer_for(SimpleNamespace(model=MODEL)).model == MODEL


def test_body_is_a_fixed_whitelist():
    observer = ModelStatusObserver(MODEL, probe=CountingProbe(), clock=lambda: 5.0, wall=lambda: 1234.5)
    assert observer.observe() == {'source': 'observed', 'observed_at': 1234.5, 'model': MODEL,
                                  'status': 'responding', 'model_installed': True, 'reason': 'model_listed'}


def test_cache_shares_one_probe_and_expires():
    probe, now = CountingProbe(), [100.0]
    observer = ModelStatusObserver(MODEL, probe=probe, clock=lambda: now[0])
    first = observer.observe()
    now[0] += CACHE_SECONDS - 0.1
    assert observer.observe() == first and len(probe.calls) == 1
    now[0] += 0.2
    observer.observe()
    assert len(probe.calls) == 2


def test_concurrent_polls_wait_for_the_same_probe():
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(model):
        calls.append(model)
        started.set()
        release.wait(5)
        return 'responding', False, 'model_not_listed'

    observer = ModelStatusObserver(MODEL, probe=slow, clock=lambda: 50.0)
    results = []
    threads = [threading.Thread(target=lambda: results.append(observer.observe())) for _ in range(6)]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1 and len(results) == 6
    assert all(item['reason'] == 'model_not_listed' for item in results)


def test_a_failed_probe_is_not_cached():
    probe, now = CountingProbe(error=RuntimeError('raw /api/tags body')), [0.0]
    observer = ModelStatusObserver(MODEL, probe=probe, clock=lambda: now[0])
    with pytest.raises(RuntimeError):
        observer.observe()
    probe.error = None
    assert observer.observe()['status'] == 'responding' and len(probe.calls) == 2


def client_for(observer):
    routes = model_status_routes(observer, valid=lambda value: value == TOKEN, origins=frozenset({ORIGIN}),
                                 headers={'Cache-Control': 'no-store'})
    return TestClient(Starlette(routes=routes), base_url=ORIGIN)


def test_gate_rejects_before_any_probe():
    probe = CountingProbe()
    client = client_for(ModelStatusObserver(MODEL, probe=probe))
    assert client.get('/api/model-status').status_code == 401
    assert client.get('/api/model-status', headers={'Authorization': 'Bearer ' + 'y' * 43}).status_code == 401
    assert client.get('/api/model-status', headers={**AUTH, 'Origin': 'https://example.org'}).status_code == 403
    assert client.get('/api/model-status?model=llama3:8b', headers=AUTH).status_code == 403
    assert client.get('/api/model-status?host=10.0.0.1', headers=AUTH).status_code == 403
    assert client.get('http://evil.test:8787/api/model-status', headers=AUTH).status_code == 403
    assert client.post('/api/model-status', headers=AUTH).status_code == 405
    assert probe.calls == []
    response = client.get('/api/model-status', headers={**AUTH, 'Origin': ORIGIN})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['model_installed'] is True and probe.calls == [MODEL]


def test_route_failures_are_fixed_503_without_raw_text():
    client = client_for(ModelStatusObserver(MODEL, probe=CountingProbe(error=RuntimeError('secret body'))))
    response = client.get('/api/model-status', headers=AUTH)
    assert response.status_code == 503 and response.json() == {'error': 'model_status_unavailable'}
    assert client_for(None).get('/api/model-status', headers=AUTH).status_code == 503


@pytest.fixture
def hud_store(tmp_path):
    from jarvis.agents.models import TaskRequest
    from jarvis.agents.store import TaskStore
    from jarvis.core.guardrails import Guardrails
    store = TaskStore(Guardrails(tmp_path))
    return store, store.create(TaskRequest(summary='Selected local task'))


def test_hud_app_wires_route_from_chat_model(hud_store, monkeypatch):
    from jarvis.app.events import create_event_app
    store, _ = hud_store
    probe = CountingProbe(('connection_refused', None, 'connection_refused'))
    monkeypatch.setattr(model_status, 'probe_tags', probe)
    app = create_event_app(store, token=TOKEN, task_ids=[], origins=[ORIGIN], chat=SimpleNamespace(model=MODEL))
    client = TestClient(app, base_url=ORIGIN)
    assert client.get('/api/model-status').status_code == 401 and probe.calls == []
    response = client.get('/api/model-status', headers=AUTH)
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['status'] == 'connection_refused' and probe.calls == [MODEL]


@pytest.mark.parametrize('chat', [None, SimpleNamespace(model='qwen:cloud'), object()])
def test_hud_app_without_valid_chat_model_is_503_without_probe(hud_store, monkeypatch, chat):
    from jarvis.app.events import create_event_app
    store, selected = hud_store
    probe = CountingProbe()
    monkeypatch.setattr(model_status, 'probe_tags', probe)
    app = create_event_app(store, token=TOKEN, task_ids=[selected.task_id], origins=[ORIGIN], chat=chat)
    response = TestClient(app, base_url=ORIGIN).get('/api/model-status', headers=AUTH)
    assert response.status_code == 503 and response.json() == {'error': 'model_status_unavailable'}
    assert probe.calls == []
