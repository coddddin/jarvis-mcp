"""HUD "오늘 브리핑" route: token gate, whitelist, in-memory cache, failure shapes. No real Google call."""
import json
import logging

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jarvis.app.briefing_routes import briefing_routes
from jarvis.features.google.service import GoogleIntegrationError

TOKEN = 'x' * 43
ORIGIN = 'http://127.0.0.1:8787'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


def raw_briefing():
    return {'date': '2026-10-25', 'timezone': 'Asia/Seoul', 'generated_at': '2026-10-25T09:00:00+09:00',
            'untrusted_content': True, 'partial': False, 'mail_scope': 'Current unread inbox metadata.',
            'calendar': {'status': 'ok', 'has_more': False, 'events': [
                {'id': 'evt-secret-id', 'summary': '팀 회의', 'htmlLink': 'https://calendar.google.com/x',
                 'start': {'dateTime': '2026-10-25T10:00:00+09:00'}, 'end': {'dateTime': '2026-10-25T11:00:00+09:00'},
                 'attendees': ['someone@example.com']},
                {'summary': '휴가', 'start': {'date': '2026-10-25'}, 'end': {'date': '2026-10-26'}}]},
            'gmail': {'status': 'ok', 'has_more': True, 'unread_estimate': 42, 'messages': [
                {'id': 'msg-secret-id', 'from': 'Alice <alice@example.com>', 'subject': '견적 문의', 'date': 'Sat, 25 Oct',
                 'snippet': 'body text must never pass'}]}}


class FakeBriefing:
    def __init__(self, connected=True, result=None, error=None):
        self.connected, self.result, self.error, self.calls = connected, result, error, 0

    def status(self):
        return {'credentials_present': self.connected, 'connection_verified': False}

    def briefing(self, day='', limit=10):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result if self.result is not None else raw_briefing()


def client_for(service, now=None):
    clock = now or [1000.0]
    routes = briefing_routes(service, valid=lambda value: value == TOKEN, origins=frozenset({ORIGIN}),
                             headers={'Cache-Control': 'no-store'}, clock=lambda: clock[0])
    return TestClient(Starlette(routes=routes), base_url=ORIGIN), clock


def test_api_requires_token_origin_and_host_and_no_query():
    service = FakeBriefing()
    client, _ = client_for(service)
    assert client.get('/api/briefing').status_code == 401
    assert client.get('/api/briefing', headers={'Authorization': 'Bearer ' + 'y' * 43}).status_code == 401
    assert client.get('/api/briefing', headers={**AUTH, 'Origin': 'https://example.org'}).status_code == 403
    assert client.get('/api/briefing?day=2026-01-01', headers=AUTH).status_code == 403
    assert client.get('http://evil.test:8787/api/briefing', headers=AUTH).status_code == 403
    assert client.post('/api/briefing', headers=AUTH).status_code == 405
    assert service.calls == 0                                     # nothing reached Google
    assert client.get('/api/briefing', headers={**AUTH, 'Origin': ORIGIN}).status_code == 200


def test_response_is_a_whitelist_without_ids_links_or_bodies():
    client, _ = client_for(FakeBriefing())
    data = client.get('/api/briefing', headers=AUTH).json()
    assert data == {
        'connected': True, 'date': '2026-10-25', 'generated_at': '2026-10-25T09:00:00+09:00', 'partial': False,
        'calendar': {'status': 'ok', 'has_more': False, 'events': [
            {'summary': '팀 회의', 'start': '2026-10-25T10:00:00+09:00', 'end': '2026-10-25T11:00:00+09:00'},
            {'summary': '휴가', 'start': '2026-10-25', 'end': '2026-10-26'}]},
        'gmail': {'status': 'ok', 'has_more': True, 'unread_estimate': 42, 'messages': [
            {'from': 'Alice <alice@example.com>', 'subject': '견적 문의', 'date': 'Sat, 25 Oct'}]}}
    text = json.dumps(data)
    for secret in ('secret-id', 'htmlLink', 'snippet', 'body text', 'attendees'):
        assert secret not in text


def test_odd_values_are_dropped_or_capped_instead_of_passed_through():
    raw = raw_briefing()
    raw['calendar']['events'] = [{'summary': {'nested': 'x'}, 'start': 'not-a-dict', 'end': {'dateTime': 5}},
                                 'not-an-event'] + [{'summary': 'x' * 1000}] * 20
    raw['gmail']['messages'] = [{'from': None, 'subject': 'y' * 1000, 'date': 3}]
    raw['gmail']['unread_estimate'] = 'many'
    client, _ = client_for(FakeBriefing(result=raw))
    data = client.get('/api/briefing', headers=AUTH).json()
    assert data['calendar']['events'][0] == {'summary': '', 'start': '', 'end': ''}
    assert len(data['calendar']['events']) == 10 and len(data['calendar']['events'][1]['summary']) == 300
    assert data['gmail']['messages'] == [{'from': '', 'subject': 'y' * 300, 'date': ''}]
    assert data['gmail']['unread_estimate'] is None


def test_not_connected_reports_setup_without_calling_google():
    service = FakeBriefing(connected=False)
    client, _ = client_for(service)
    assert client.get('/api/briefing', headers=AUTH).json() == {'connected': False}
    assert service.calls == 0


def test_a_failing_side_keeps_its_own_error_and_the_other_side():
    raw = raw_briefing()
    raw['partial'] = True
    raw['gmail'] = {'status': 'error', 'error': 'Google API returned HTTP 403; check consent and enabled APIs.'}
    client, _ = client_for(FakeBriefing(result=raw))
    data = client.get('/api/briefing', headers=AUTH).json()
    assert data['partial'] is True and data['calendar']['status'] == 'ok'
    assert data['gmail'] == {'status': 'error', 'error': 'Google API returned HTTP 403; check consent and enabled APIs.'}


def test_an_expired_connection_is_a_readable_error_not_a_crash():
    error = GoogleIntegrationError('Invalid Google credentials; reconnect using setup helper.')
    client, _ = client_for(FakeBriefing(error=error))
    response = client.get('/api/briefing', headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {'connected': True,
                               'error': 'Invalid Google credentials; reconnect using setup helper.'}


def test_an_unexpected_failure_is_a_generic_503_that_logs_no_content(caplog):
    client, _ = client_for(FakeBriefing(error=RuntimeError('subject: 견적 문의 from alice@example.com')))
    with caplog.at_level(logging.DEBUG):
        response = client.get('/api/briefing', headers=AUTH)
    assert response.status_code == 503 and response.json() == {'error': 'briefing_unavailable'}
    assert '견적' not in caplog.text and 'alice' not in caplog.text


def test_results_are_cached_in_memory_for_sixty_seconds():
    service = FakeBriefing()
    client, clock = client_for(service)
    first = client.get('/api/briefing', headers=AUTH).json()
    clock[0] += 59
    assert client.get('/api/briefing', headers=AUTH).json() == first and service.calls == 1
    clock[0] += 2
    client.get('/api/briefing', headers=AUTH)
    assert service.calls == 2


def test_a_503_is_not_cached():
    service = FakeBriefing(error=RuntimeError('down'))
    client, _ = client_for(service)
    assert client.get('/api/briefing', headers=AUTH).status_code == 503
    service.error = None
    assert client.get('/api/briefing', headers=AUTH).status_code == 200 and service.calls == 2


def test_the_hud_app_serves_the_route_and_the_asset(tmp_path):
    from jarvis.app.events import create_event_app
    from jarvis.agents.store import TaskStore
    from jarvis.core.guardrails import Guardrails
    store = TaskStore(Guardrails(tmp_path))
    app = create_event_app(store, token=TOKEN, task_ids=[], origins=[ORIGIN], chat=object(),
                           briefing=FakeBriefing())
    client = TestClient(app, base_url=ORIGIN)
    assert client.get('/api/briefing', headers=AUTH).json()['connected'] is True
    assert client.get('/assets/briefing.js').status_code == 200
    without = TestClient(create_event_app(store, token=TOKEN, task_ids=[], origins=[ORIGIN], chat=object()),
                         base_url=ORIGIN)
    assert without.get('/api/briefing', headers=AUTH).status_code == 404
