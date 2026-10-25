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
    observed = dispatch_control.service_state(guard)
    assert observed == {'running': True, 'pid': os.getpid(),
                        'started': pytest.approx(time.time(), abs=60), 'status': 'running',
                        'reason': 'matching_pid', 'source': 'observed'}
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
                                           'exhausted_until': None,
                                           'usage_quality_runs': {'measured': 0, 'partial': 0, 'unknown': 0,
                                                                  'missing': 0, 'unrecorded': 1},
                                           'known_input_tokens': None, 'known_input_runs': 0,
                                           'known_output_tokens': None, 'known_output_runs': 0,
                                           'unrecorded_input_tokens': 10, 'unrecorded_output_tokens': 5}
    assert set(state['recent'][0]) == set(dispatch_control.RUN_FIELDS)
    assert 'secret' not in json.dumps(state)
    dispatch_control.pause(guard)
    paused = dispatch_control.snapshot(guard, config)
    assert paused['paused'] is True and paused['effective_enabled'] is False and paused['config_enabled'] is True


def test_recent_runs_show_model_and_only_known_result_codes(guard, config):
    ledger = DispatchLedger(guard)
    notes = ['executable not found', 'all models backed off', 'cursor-20261026.log',
             'OSError: C:\\Users\\someone\\secret path']
    for note in notes:
        run_id = ledger.start('cursor', pid=None)
        ledger.set_model(run_id, 'gpt-free')
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None, note=note)
    state = dispatch_control.snapshot(guard, config)
    results = [row['result'] for row in reversed(state['recent'])]
    assert results == ['executable_missing', 'models_backed_off', None, None]
    assert {row['model'] for row in state['recent']} == {'gpt-free'}
    assert 'secret' not in json.dumps(state) and '.log' not in json.dumps(state)


class _Created:
    def __init__(self, created):
        self.created = created

    def create_time(self):
        return self.created


def test_working_runs_are_observed_without_settling_the_ledger(guard, config):
    old = DispatchLedger(guard, clock=lambda: 500.0)
    stale = old.start('opencode', pid=None)  # no pid long after the start
    ledger = DispatchLedger(guard, clock=lambda: 1000.0)
    spawning = ledger.start('codex', pid=None)
    alive, gone, denied = (ledger.start(name, pid=None) for name in ('cursor', 'claude-code', 'gemini'))
    for run_id, pid in ((alive, 111), (gone, 222), (denied, 333)):
        ledger.set_pid(run_id, pid)
    ledger.set_model(alive, 'gpt-free')

    def lookup(pid):
        if pid == 333:
            raise psutil.AccessDenied(pid)
        return _Created(900.0)

    state = dispatch_control.snapshot(guard, config, clock=lambda: 1050.0, alive=lambda pid: pid != 222, lookup=lookup)
    by_id = {item['id']: item for item in state['working']}
    assert {run_id: item['state'] for run_id, item in by_id.items()} == {
        stale: 'unknown', spawning: 'starting', alive: 'working', gone: 'ended', denied: 'unknown'}
    assert by_id[alive] == {'id': alive, 'employee': 'cursor', 'model': 'gpt-free', 'started': 1000.0, 'state': 'working'}
    assert set(by_id[gone]) == {'id', 'employee', 'model', 'started', 'state'}  # no pid or note
    # Reading never closes a row: the dead owner's run is still 'running' in the ledger.
    with ledger.connection() as db:
        assert db.execute('SELECT status FROM runs WHERE id=?', (gone,)).fetchone()[0] == 'running'


def test_working_is_empty_when_nothing_runs(guard, config):
    assert dispatch_control.snapshot(guard, config)['working'] == []


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


def test_concurrent_pause_and_resume_never_fail(guard):
    # A double-clicked HUD button sends two POSTs that run in parallel threads; neither may fail.
    from concurrent.futures import ThreadPoolExecutor
    calls = [dispatch_control.pause, dispatch_control.resume] * 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda call: call(guard), calls))
    assert isinstance(read_pause(guard)['paused'], bool)
    leftovers = [p.name for p in (guard.workspace / '.jarvis-dispatch').iterdir() if p.name != 'paused.json']
    assert leftovers == []


def test_read_pause_stays_paused_when_path_check_fails(guard, monkeypatch):
    # The service loop calls read_pause outside its try block; a guard error there must not escape.
    from jarvis.core.guardrails import GuardrailError

    def refuse(*args, **kwargs):
        raise GuardrailError('Path is outside JarvisWorkspace.')
    monkeypatch.setattr(guard, 'validate_path', refuse)
    assert read_pause(guard) == {'paused': True, 'reason': 'unreadable_pause_file'}

BS = chr(92)
EXTENDED = BS * 2 + '?' + BS  # the Windows extended-length path prefix


def test_validate_path_normalizes_extended_length_result(guard, monkeypatch):
    # Windows realpath can return an extended-length path for a file being replaced; still inside the workspace.
    import pathlib
    real = pathlib.Path.resolve

    def extended(self, strict=False):
        result = real(self, strict=strict)
        return pathlib.Path(EXTENDED + str(result)) if 'paused' in self.name else result
    monkeypatch.setattr(pathlib.Path, 'resolve', extended)
    assert guard.validate_path('.jarvis-dispatch/paused.json', must_exist=False) == \
        guard.workspace / '.jarvis-dispatch' / 'paused.json'


def test_validate_path_still_rejects_extended_unc_result(guard, monkeypatch):
    import pathlib
    from jarvis.core.guardrails import GuardrailError
    monkeypatch.setattr(pathlib.Path, 'resolve', lambda self, strict=False: pathlib.Path(EXTENDED + 'UNC' + BS + 'host' + BS + 'share' + BS + 'x'))
    with pytest.raises(GuardrailError):
        guard.validate_path('x.json', must_exist=False)


def test_snapshot_includes_the_process_watch(guard, config):
    run_id = DispatchLedger(guard).start('codex', pid=None)
    state = dispatch_control.snapshot(guard, config)
    assert state['service']['status'] == 'not_registered'
    assert not any(f['kind'] == 'service_down' for f in state['watch']['findings'])
    assert run_id


def test_snapshot_survives_a_failing_watch(guard, config, monkeypatch):
    from jarvis.agents import process_watch
    monkeypatch.setattr(process_watch, 'scan', lambda *a, **k: 1 / 0)
    state = dispatch_control.snapshot(guard, config)
    assert state['watch'] == {'findings': [], 'checked_at': None, 'error': 'watch_unavailable'}
    assert state['effective_enabled'] is True


def test_service_missing_record_is_not_a_stopped_observation(guard):
    state = dispatch_control.service_state(guard)
    assert state['status'] == 'not_registered' and state['reason'] == 'missing_record'
    assert state['running'] is False


@pytest.mark.parametrize('body', ['{broken', '[]', '{"pid":true,"started":10}',
    '{"pid":-1,"started":10}', '{"pid":1,"started":NaN}', '{"pid":1,"started":0}'])
def test_bad_service_record_is_unknown_not_stopped(guard, body):
    info = guard.workspace / '.jarvis-dispatch/service.json'
    info.parent.mkdir(parents=True)
    info.write_text(body, encoding='utf-8')
    state = dispatch_control.service_state(guard)
    assert state['status'] == 'unknown' and state['reason'] == 'invalid_record'
    assert state['pid'] is None and state['running'] is False


@pytest.mark.parametrize('error,status', [(psutil.NoSuchProcess(42),'stopped'),
    (psutil.AccessDenied(42),'unknown'), (OSError('PRIVATE process error'),'unknown')])
def test_service_lookup_missing_and_denied_are_distinct(guard, error, status):
    info = guard.workspace / '.jarvis-dispatch/service.json'
    info.parent.mkdir(parents=True)
    info.write_text('{"pid":42,"started":100}', encoding='utf-8')
    def lookup(pid):
        raise error
    state = dispatch_control.service_state(guard, lookup=lookup)
    assert state['status'] == status and state['running'] is False
    assert 'PRIVATE' not in json.dumps(state)


def test_service_reused_pid_is_not_reported_as_stopped(guard):
    info = guard.workspace / '.jarvis-dispatch/service.json'
    info.parent.mkdir(parents=True)
    info.write_text('{"pid":42,"started":100}', encoding='utf-8')
    class Process:
        def create_time(self): return 200
    state = dispatch_control.service_state(guard, lookup=lambda pid: Process())
    assert state['status'] == 'identity_mismatch' and state['pid'] is None


def test_enabled_snapshot_does_not_imply_registered_service(client):
    data = client.get('/api/dispatcher', headers=AUTH).json()
    assert data['config_enabled'] is True and data['effective_enabled'] is True
    assert data['service']['status'] == 'not_registered'
    assert data['service']['running'] is False


def test_oversized_or_nonfinite_service_record_is_unknown(guard):
    info = guard.workspace / '.jarvis-dispatch/service.json'
    info.parent.mkdir(parents=True)
    for body in ('x'*16385, json.dumps({'pid':42,'started':10**400})):
        info.write_text(body, encoding='utf-8')
        assert dispatch_control.service_state(guard)['status'] == 'unknown'


def test_unreadable_record_is_unknown_without_raw_error(guard, monkeypatch):
    from pathlib import Path
    info = guard.workspace / '.jarvis-dispatch/service.json'
    info.parent.mkdir(parents=True)
    info.write_text('{"pid":42,"started":100}', encoding='utf-8')
    original = Path.read_text
    def unreadable(path, *args, **kwargs):
        if path == info: raise PermissionError('PRIVATE read failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', unreadable)
    state = dispatch_control.service_state(guard)
    assert state['status'] == 'unknown' and state['reason'] == 'unreadable_record'
    assert 'PRIVATE' not in json.dumps(state)


def test_snapshot_shows_a_rotating_employee_waiting_only_when_every_model_is_backed_off(guard, config):
    data = json.loads(config.read_text(encoding='utf-8'))
    data['employees']['opencode'] = {'enabled': True, 'models': ['m/a', 'm/b']}
    config.write_text(json.dumps(data), encoding='utf-8')
    ledger = DispatchLedger(guard)
    def backed_off(model):
        run_id = ledger.try_start('opencode', daily_runs=100)
        ledger.set_model(run_id, model)
        ledger.finish(run_id, status='limit', exit_code=1, input_tokens=None, output_tokens=None, note='')
    backed_off('m/a')
    assert dispatch_control.snapshot(guard, config)['employees']['opencode']['exhausted_until'] is None
    backed_off('m/b')
    until = dispatch_control.snapshot(guard, config)['employees']['opencode']['exhausted_until']
    assert until == ledger.models_exhausted_until('opencode', ['m/a', 'm/b']) and until > time.time()


def test_snapshot_adds_quality_counts_and_known_sums_beside_the_old_totals(guard, config):
    ledger = DispatchLedger(guard)
    for inp, out, quality in ((100, 5, 'measured'), (None, 7, 'partial'), (None, None, 'missing')):
        ledger.finish(ledger.start('cursor', pid=None), status='ok', exit_code=0, input_tokens=inp,
                      output_tokens=out, note='cursor-secret-log.log', usage_quality=quality)
    ledger.finish(ledger.start('codex', pid=None), status='ok', exit_code=0, input_tokens=None, output_tokens=None,
                  note='', usage_quality='unknown')
    state = dispatch_control.snapshot(guard, config, clock=lambda: 5.0)
    cursor, codex = state['employees']['cursor'], state['employees']['codex']
    assert cursor['total_input_tokens'] == 100 and cursor['total_output_tokens'] == 12   # unchanged meaning
    assert cursor['usage_quality_runs'] == {'measured': 1, 'partial': 1, 'unknown': 0, 'missing': 1, 'unrecorded': 0}
    assert (cursor['known_input_tokens'], cursor['known_input_runs']) == (100, 1)
    assert (cursor['known_output_tokens'], cursor['known_output_runs']) == (12, 2)
    assert codex['total_input_tokens'] == 0 and codex['known_input_tokens'] is None   # 0 shown, not measured
    assert codex['usage_quality_runs']['unknown'] == 1
    assert [row['usage_quality'] for row in state['recent']] == ['unknown', 'missing', 'partial', 'measured']
    assert 'secret' not in json.dumps(state)
    # a value written outside finish() is not passed through as text
    with ledger.connection() as db:
        db.execute("UPDATE runs SET usage_quality='secret-text' WHERE employee='codex'")
    assert dispatch_control.snapshot(guard, config)['recent'][0]['usage_quality'] is None
