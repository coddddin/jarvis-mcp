"""Run collector boundaries. No model calls or unrelated process discovery."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis.agents.activity import ActivityError, ActivityReporter, ActivityStore
from jarvis.core.guardrails import GuardrailError, Guardrails


@pytest.fixture
def setup(tmp_path):
    clock = {'wall': 1000., 'mono': 10., 'created': 900.}
    process = SimpleNamespace(create_time=lambda: clock['created'], ppid=lambda: 1,
                              is_running=lambda: True, status=lambda: 'running')
    store = ActivityStore(Guardrails(tmp_path), clock=lambda: clock['wall'],
                          monotonic=lambda: clock['mono'], lookup=lambda pid: process)
    credential = store.register('codex:test', uuid4().hex)
    return store, credential, clock, process


def event(run, seq=1, **kwargs):
    return {'run_id': run['run_id'], 'epoch': run['epoch'], 'seq': seq,
            'kind': 'progress', 'phase': 'working', **kwargs}


def test_token_only_returned_once_not_stored_or_projected(setup):
    store, run, _, _ = setup
    store.ingest(run['token'], event(run))
    with store.connection(write=False) as db:
        assert db.execute('SELECT token_hash FROM runs').fetchone()[0] != run['token']
    assert run['token'].encode() not in (store.guard.workspace / store.DIRECTORY / 'activity.sqlite3').read_bytes()
    assert run['token'] not in json.dumps(store.inspect()) + json.dumps(store.history(run['run_id']))


@pytest.mark.parametrize('change', [{'seq': True}, {'source': 'verified'}, {'stdout': 'private'},
                                  {'phase': 'hacking'}, {'epoch': 0}, {'seq': 0},
                                  {'result_ref': '../secret'}, {'kind': 'completed'}])
def test_invalid_or_untrusted_payload_has_no_effect(setup, change):
    store, run, _, _ = setup
    with pytest.raises(ActivityError):
        store.ingest(run['token'], event(run, **change))
    assert store.history(run['run_id'])['events'] == []


def test_unknown_run_wrong_token_and_epoch_are_refused(setup):
    store, run, _, _ = setup
    for token, payload in [('A' * 43, event(run)), (run['token'], event(run, run_id=uuid4().hex)),
                           (run['token'], event(run, epoch=run['epoch'] + 1))]:
        with pytest.raises(ActivityError):
            store.ingest(token, payload)
    assert store.inspect()['runs'][0]['last_seq'] == 0


def test_duplicate_is_idempotent_and_cannot_refresh_connection(setup):
    store, run, clock, _ = setup
    assert store.ingest(run['token'], event(run))['status'] == 'accepted'
    clock['mono'] = 60
    assert store.ingest(run['token'], event(run))['status'] == 'duplicate'
    assert store.inspect()['runs'][0]['heartbeat']['status'] == 'delayed'
    with pytest.raises(ActivityError, match='different content'):
        store.ingest(run['token'], event(run, phase='review'))
    assert len(store.history(run['run_id'])['events']) == 1


def test_late_transition_is_durable_without_rollback_and_repairs_gap(setup):
    store, run, _, _ = setup
    store.ingest(run['token'], event(run, 2, phase='review'))
    cursor = store.history(run['run_id'])['events'][-1]['cursor']
    assert store.inspect()['runs'][0]['sequence_gap']
    assert store.ingest(run['token'], event(run, 1))['status'] == 'stale_sequence'
    current = store.inspect()['runs'][0]
    assert current['report']['event']['phase'] == 'review' and not current['sequence_gap']
    assert [r['event']['seq'] for r in store.history(run['run_id'])['events']] == [2, 1]
    assert [r['event']['seq'] for r in store.history(run['run_id'], after=cursor)['events']] == [1]


def test_new_epoch_fences_previous_owner_run_even_with_valid_token(setup):
    store, run, _, _ = setup
    task = store.inspect()['runs'][0]['task_id']
    replacement = store.register('codex:test', task)
    assert replacement['epoch'] == run['epoch'] + 1
    with pytest.raises(ActivityError, match='fenced'):
        store.ingest(run['token'], event(run))
    assert store.ingest(replacement['token'], event(replacement))['status'] == 'accepted'
    old = next(r for r in store.inspect()['runs'] if r['run_id'] == run['run_id'])
    assert old['fenced'] and old['report']['event'] is None


def test_atomic_registration_gives_distinct_epochs(setup):
    store, _, _, _ = setup
    task = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        credentials = list(pool.map(lambda _: store.register('codex:test', task), range(2)))
    assert sorted(r['epoch'] for r in credentials) == [1, 2]


def test_restart_and_monotonic_regression_never_claim_freshness(setup):
    store, run, clock, process = setup
    store.ingest(run['token'], event(run))
    clock['mono'] = 1
    assert store.inspect()['runs'][0]['heartbeat']['reason'] == 'clock_regressed'
    reopened = ActivityStore(store.guard, clock=lambda: clock['wall'], monotonic=lambda: clock['mono'],
                              lookup=lambda pid: process)
    assert reopened.inspect()['runs'][0]['heartbeat'] == {
        'source': 'observed', 'status': 'unknown', 'reason': 'collector_restarted', 'received_at': 1000.}
    assert len(reopened.history(run['run_id'])['events']) == 1
    reopened.ingest(run['token'], event(run, 2))
    assert reopened.inspect()['runs'][0]['heartbeat']['status'] == 'recent'


def test_pid_reuse_and_reported_success_never_invent_verification(setup):
    store, run, clock, _ = setup
    store.ingest(run['token'], event(run, kind='completed', phase='finished'))
    clock['created'] = 999
    observed = store.inspect()['runs'][0]
    assert observed['process']['status'] == 'identity_mismatch'
    assert observed['report']['event']['kind'] == 'completed'
    assert observed['verified'] == [] and observed['report']['source'] == 'self_reported'


def test_unrelated_pid_registration_is_refused(setup):
    store, _, _, _ = setup
    with pytest.raises(ActivityError, match='direct child'):
        store.register('codex:test', uuid4().hex, pid=os.getpid() + 1)


def test_rate_limit_only_heartbeats_and_capacity_never_evicts_transitions(setup):
    store, run, clock, _ = setup
    store.ingest(run['token'], event(run, kind='heartbeat'))
    assert store.ingest(run['token'], event(run, 2, kind='heartbeat'))['status'] == 'rate_limited'
    assert store.ingest(run['token'], event(run, 2, phase='review'))['status'] == 'accepted'
    store.MAX_EVENTS = 2
    with pytest.raises(ActivityError, match='capacity'):
        store.ingest(run['token'], event(run, 3))
    assert len(store.history(run['run_id'])['events']) == 2


def test_storage_links_stop_and_limits_are_refused(setup, tmp_path):
    store, run, _, _ = setup
    for limit in (0, True, 201):
        with pytest.raises(ActivityError):
            store.inspect(limit=limit)
    target = tmp_path / store.DIRECTORY / 'activity.sqlite3'
    alias = tmp_path / 'alias.sqlite3'
    os.link(target, alias)
    with pytest.raises(GuardrailError):
        store.inspect()
    alias.unlink()
    (tmp_path / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        store.ingest(run['token'], event(run))


def test_monotonic_regression_during_ingest_is_refused_without_refresh(setup):
    store, run, clock, _ = setup
    store.ingest(run['token'], event(run))
    clock['mono'] = 1
    with pytest.raises(ActivityError, match='regressed'):
        store.ingest(run['token'], event(run, 2))
    assert len(store.history(run['run_id'])['events']) == 1


def test_reporter_observer_failure_never_controls_or_retries_work(setup):
    store, run, _, _ = setup
    reporter = ActivityReporter(store, run)
    reporter.start()
    reporter.close()
    assert [item['event']['kind'] for item in store.history(run['run_id'])['events']] == ['start']
    assert reporter.send('completed', phase='finished')
    assert store.inspect()['runs'][0]['verified'] == []
    (store.guard.workspace / '.jarvis-stop').touch()
    assert not reporter.send('progress')
    assert reporter.rejected == 1


def test_terminal_report_cannot_be_overwritten_but_late_transitions_recover(setup):
    store, run, _, _ = setup
    store.ingest(run['token'], event(run, 2, kind='completed', phase='finished'))
    assert store.ingest(run['token'], event(run))['status'] == 'stale_sequence'
    with pytest.raises(ActivityError, match='terminal'):
        store.ingest(run['token'], event(run, 3, kind='heartbeat'))
    assert store.inspect()['runs'][0]['report']['event']['kind'] == 'completed'


def test_actual_owned_child_natural_exit_is_observed_separately_from_task_success(tmp_path):
    store = ActivityStore(Guardrails(tmp_path))
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])
    try:
        run = store.register('local:test', uuid4().hex, pid=child.pid)
        store.ingest(run['token'], event(run, kind='start'))
        assert store.inspect()['runs'][0]['process']['status'] == 'running'
    finally:
        child.wait(timeout=5)
    snapshot = store.inspect()['runs'][0]
    assert snapshot['process']['status'] == 'exited'
    assert snapshot['report']['event']['kind'] == 'start'
    assert snapshot['verified'] == []
