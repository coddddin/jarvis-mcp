"""W5 integration-4 backend: dispatcher pause/resume and status for the HUD panel."""
import json
import os
import time

import psutil
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jarvis.agents import dispatch_control
from jarvis.agents.dispatch_policy import DispatchLedger
from jarvis.agents.dispatch_service import read_pause
from jarvis.app.dispatcher_routes import dispatcher_routes
from jarvis.core.guardrails import Guardrails

TOKEN = 'x' * 43
ORIGIN = 'http://127.0.0.1:8787'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'workspace').mkdir()
    return Guardrails(tmp_path / 'workspace')


@pytest.fixture
def config(tmp_path):
    path = tmp_path / 'dispatcher.json'
    path.write_text(json.dumps({'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10,
                                'run_timeout_minutes': 30, 'employees': {'codex': {'enabled': True}}}),
                    encoding='utf-8')
    return path


@pytest.fixture
def client(guard, config):
    routes = dispatcher_routes(guard, config, valid=lambda value: value == TOKEN, origins=frozenset({ORIGIN}),
                               headers={'Cache-Control': 'no-store'})
    return TestClient(Starlette(routes=routes), base_url=ORIGIN)


def test_pause_and_resume_follow_the_service_contract(guard):
    assert read_pause(guard) == {'paused': False}
    dispatch_control.pause(guard, '점검', clock=lambda: 100.0)
    assert read_pause(guard) == {'paused': True, 'by': 'user:heejun', 'reason': '점검', 'at': 100.0}
    dispatch_control.resume(guard, clock=lambda: 200.0)
    assert read_pause(guard)['paused'] is False
    assert not list((guard.workspace / '.jarvis-dispatch').glob('*.tmp'))


def test_resume_replaces_a_corrupt_marker_that_reads_as_paused(guard):
    marker = guard.workspace / '.jarvis-dispatch' / 'paused.json'
    marker.parent.mkdir(parents=True)
    marker.write_text('{broken', encoding='utf-8')
    assert read_pause(guard)['paused'] is True
    dispatch_control.resume(guard)
    assert read_pause(guard)['paused'] is False


def test_reason_is_bounded(guard):
    with pytest.raises(ValueError):
        dispatch_control.pause(guard, 'x' * 201)


def test_service_is_running_only_for_a_live_matching_process(guard):
    info = guard.workspace / '.jarvis-dispatch' / 'service.json'
    info.parent.mkdir(parents=True)
    assert dispatch_control.service_state(guard)['running'] is False
    info.write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}), encoding='utf-8')
    assert dispatch_control.service_state(guard) == {'running': True, 'pid': os.getpid(),
                                                     'started': pytest.approx(time.time(), abs=60)}
    # The pid now belongs to a process created after the record: a reused pid, not the service.
    created = psutil.Process(os.getpid()).create_time()
    info.write_text(json.dumps({'pid': os.getpid(), 'started': created - 3600}), encoding='utf-8')
    assert dispatch_control.service_state(guard)['running'] is False
    info.write_text('not json', encoding='utf-8')
    assert dispatch_control.service_state(guard)['running'] is False


def test_snapshot_exposes_whitelisted_fields_only(guard, config):
    ledger = DispatchLedger(guard)
    run_id = ledger.start('codex', pid=None)
    ledger.finish(run_id, status='ok', exit_code=0, input_tokens=10, output_tokens=5, note='codex-secret-log.log')
    state = dispatch_control.snapshot(guard, config, clock=lambda: 5.0)
    assert state['effective_enabled'] is True and state['paused'] is False and state['read_at'] == 5.0
    assert state['employees']['codex'] == {'runs': 1, 'total_input_tokens': 10, 'total_output_tokens': 5,
                                           'exhausted_until': None}
    assert set(state['recent'][0]) == set(dispatch_control.RUN_FIELDS)
    assert 'secret' not in json.dumps(state)
    dispatch_control.pause(guard)
    paused = dispatch_control.snapshot(guard, config)
    assert paused['paused'] is True and paused['effective_enabled'] is False and paused['config_enabled'] is True


def test_api_requires_token_origin_and_host(client):
    assert client.get('/api/dispatcher').status_code == 401
    assert client.get('/api/dispatcher', headers={**AUTH, 'Origin': 'https://example.org'}).status_code == 403
    assert client.get('/api/dispatcher?x=1', headers=AUTH).status_code == 403
    assert client.get('http://evil.test:8787/api/dispatcher', headers=AUTH).status_code == 403
    assert client.get('/api/dispatcher', headers=AUTH).status_code == 200
    # State changes need an Origin header (browsers always send it on POST).
    assert client.post('/api/dispatcher/pause', headers=AUTH).status_code == 403
    assert client.get('/api/dispatcher/pause', headers={**AUTH, 'Origin': ORIGIN}).status_code == 405


def test_api_pause_and_resume(client, guard):
    response = client.post('/api/dispatcher/pause', headers={**AUTH, 'Origin': ORIGIN})
    assert response.status_code == 200 and response.json()['paused'] is True
    assert read_pause(guard)['paused'] is True
    response = client.post('/api/dispatcher/resume', headers={**AUTH, 'Origin': ORIGIN})
    assert response.status_code == 200 and response.json()['effective_enabled'] is True


def test_api_failure_is_a_generic_503(client, config):
    config.write_text('{}', encoding='utf-8')
    response = client.get('/api/dispatcher', headers=AUTH)
    assert response.status_code == 503 and response.json() == {'error': 'dispatcher_unavailable'}
