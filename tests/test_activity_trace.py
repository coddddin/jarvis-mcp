"""Task lineage trace over stored run reports. No model calls, execution or Board writes."""
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis.agents.activity import ActivityError, ActivityStore
from jarvis.core.guardrails import Guardrails


@pytest.fixture
def setup(tmp_path):
    clock = {'wall': 1000., 'mono': 10., 'created': 900.}
    process = SimpleNamespace(create_time=lambda: clock['created'], ppid=lambda: 1,
                              is_running=lambda: True, status=lambda: 'running')
    store = ActivityStore(Guardrails(tmp_path), clock=lambda: clock['wall'],
                          monotonic=lambda: clock['mono'], lookup=lambda pid: process)

    def register(task, owner='codex:test', **links):
        clock['wall'] += 1  # Deterministic registration order.
        return store.register(owner, task, **links)
    return store, register, clock, process


def event(run, seq=1, **kwargs):
    return {'run_id': run['run_id'], 'epoch': run['epoch'], 'seq': seq,
            'kind': 'progress', 'phase': 'working', **kwargs}


def test_normal_task_reports_start_result_and_completion_only_as_self_reports(setup):
    store, register, _, _ = setup
    task, ref = uuid4().hex, uuid4().hex
    run = register(task)
    store.ingest(run['token'], event(run, kind='start'))
    store.ingest(run['token'], event(run, 2, kind='result_received', phase='review', result_ref=ref))
    store.ingest(run['token'], event(run, 3, kind='completed', phase='finished'))
    trace = store.trace(task)
    traced = trace['runs'][0]
    assert trace['status'] == 'observed' and trace['trace_complete'] and trace['links'] == []
    assert traced['start'] == {'source': 'self_reported', 'seq': 1}
    assert traced['end'] == {'source': 'self_reported', 'kind': 'completed', 'seq': 3}
    assert traced['result_refs'] == [{'source': 'self_reported', 'ref': ref, 'seq': 2}]
    assert [e['seq'] for e in traced['events']] == [1, 2, 3]
    assert traced['lineage'] == {'source': 'registered', 'parent_run_id': None, 'step_id': None, 'retry_of': None}
    assert trace['verified'] == [] and traced['verified'] == [] and traced['report']['source'] == 'self_reported'
    assert 'verified_success' not in json.dumps(trace) and 'board_status' not in trace


def test_branch_children_link_to_parent_with_step_labels(setup):
    store, register, _, _ = setup
    task = uuid4().hex
    parent = register(task, 'codex:lead')
    first = register(task, 'codex:a', parent_run_id=parent['run_id'], step_id='p1')
    second = register(task, 'codex:b', parent_run_id=parent['run_id'], step_id='tool-1')
    trace = store.trace(task)
    assert [r['run_id'] for r in trace['runs']] == [parent['run_id'], first['run_id'], second['run_id']]
    assert trace['links'] == [
        {'run_id': first['run_id'], 'type': 'parent', 'target': parent['run_id'], 'step_id': 'p1', 'in_trace': True},
        {'run_id': second['run_id'], 'type': 'parent', 'target': parent['run_id'], 'step_id': 'tool-1', 'in_trace': True}]
    assert first['epoch'] == second['epoch'] == 1
    inspected = {r['run_id']: r for r in store.inspect()['runs']}
    assert inspected[second['run_id']]['lineage']['step_id'] == 'tool-1'
    assert inspected[second['run_id']]['verified'] == []


def test_retry_must_match_owner_step_and_parent_and_fences_old_run(setup):
    store, register, _, _ = setup
    task = uuid4().hex
    parent = register(task, 'codex:lead')
    first = register(task, 'codex:a', parent_run_id=parent['run_id'], step_id='p1')
    store.ingest(first['token'], event(first, kind='failed', phase='finished', reason_code='timeout'))
    for owner, links in [('codex:a', {'parent_run_id': parent['run_id'], 'step_id': 'p2'}),
                         ('codex:a', {'step_id': 'p1'}),
                         ('codex:a', {'parent_run_id': parent['run_id']}),
                         ('codex:b', {'parent_run_id': parent['run_id'], 'step_id': 'p1'})]:
        with pytest.raises(ActivityError, match='Retry'):
            register(task, owner, retry_of=first['run_id'], **links)
    retry = register(task, 'codex:a', parent_run_id=parent['run_id'], step_id='p1', retry_of=first['run_id'])
    assert retry['epoch'] == 2
    with pytest.raises(ActivityError, match='fenced'):
        store.ingest(first['token'], event(first, 2))
    trace = store.trace(task)
    old = trace['runs'][1]
    assert len(trace['runs']) == 3 and old['fenced'] and old['end']['kind'] == 'failed'
    assert {'run_id': retry['run_id'], 'type': 'retry_of', 'target': first['run_id'],
            'step_id': 'p1', 'in_trace': True} in trace['links']
    assert trace['runs'][2]['end'] == {'source': 'unknown', 'kind': None, 'seq': None}


def test_missing_terminal_and_completed_with_gap_stay_incomplete(setup):
    store, register, _, _ = setup
    task = uuid4().hex
    open_run = register(task, 'codex:a')
    store.ingest(open_run['token'], event(open_run, kind='start'))
    gapped = register(task, 'codex:b')
    store.ingest(gapped['token'], event(gapped, kind='start'))
    store.ingest(gapped['token'], event(gapped, 3, kind='completed', phase='finished'))
    trace = store.trace(task)
    first, second = trace['runs']
    assert first['end']['source'] == 'unknown' and not first['trace_complete']
    assert second['end'] == {'source': 'self_reported', 'kind': 'completed', 'seq': 3}
    assert second['sequence_gap'] and not second['trace_complete'] and second['verified'] == []
    assert not trace['trace_complete']


def test_duplicate_and_reversed_events_trace_in_sequence_order(setup):
    store, register, _, _ = setup
    task, ref = uuid4().hex, uuid4().hex
    run = register(task)
    result = event(run, 2, kind='result_received', result_ref=ref)
    store.ingest(run['token'], result)
    assert store.ingest(run['token'], event(run, kind='start'))['status'] == 'stale_sequence'
    assert store.ingest(run['token'], result)['status'] == 'duplicate'
    traced = store.trace(task)['runs'][0]
    assert [(e['seq'], e['kind']) for e in traced['events']] == [(1, 'start'), (2, 'result_received')]
    assert traced['result_refs'] == [{'source': 'self_reported', 'ref': ref, 'seq': 2}]
    assert not traced['sequence_gap'] and traced['end']['source'] == 'unknown'


def test_reopened_collector_keeps_lineage_and_reports_restart(setup):
    store, register, clock, process = setup
    task = uuid4().hex
    parent = register(task, 'codex:lead')
    child = register(task, 'codex:a', parent_run_id=parent['run_id'], step_id='p1')
    store.ingest(child['token'], event(child, kind='start'))
    reopened = ActivityStore(store.guard, clock=lambda: clock['wall'], monotonic=lambda: clock['mono'],
                             lookup=lambda pid: process)
    trace = reopened.trace(task)
    assert trace['links'] == store.trace(task)['links'] and len(trace['links']) == 1
    assert trace['runs'][1]['heartbeat']['reason'] == 'collector_restarted'
    assert trace['runs'][0]['heartbeat']['reason'] == 'no_report'
    assert trace['runs'][1]['start']['seq'] == 1


def test_migration_keeps_old_runs_and_events_with_unknown_lineage(setup):
    store, register, clock, process = setup
    task = uuid4().hex
    old = register(task)
    store.ingest(old['token'], event(old, kind='start'))
    store.ingest(old['token'], event(old, 2))
    before = store.history(old['run_id'])
    path = store.guard.workspace / store.DIRECTORY / 'activity.sqlite3'
    db = sqlite3.connect(path)
    for column in ('parent_run_id', 'step_id', 'retry_of', 'lineage'):
        db.execute('ALTER TABLE runs DROP COLUMN ' + column)
    db.commit()
    db.close()
    reopened = ActivityStore(store.guard, clock=lambda: clock['wall'], monotonic=lambda: clock['mono'],
                             lookup=lambda pid: process)
    assert reopened.history(old['run_id']) == before
    traced = reopened.trace(task)['runs'][0]
    assert traced['lineage'] == {'source': 'unknown', 'parent_run_id': None, 'step_id': None, 'retry_of': None}
    with pytest.raises(ActivityError, match='Retry'):
        reopened.register('codex:test', task, retry_of=old['run_id'])
    child = reopened.register('codex:a', task, parent_run_id=old['run_id'])
    assert reopened.trace(task)['links'][0]['run_id'] == child['run_id']


def test_missing_cross_task_and_self_links_roll_back_without_epoch_change(setup):
    store, register, _, _ = setup
    task, other_task = uuid4().hex, uuid4().hex
    base = register(task)
    foreign = register(other_task)
    for links in [{'parent_run_id': uuid4().hex}, {'parent_run_id': foreign['run_id']},
                  {'retry_of': uuid4().hex}, {'retry_of': foreign['run_id']},
                  {'parent_run_id': base['run_id'], 'retry_of': base['run_id']}]:
        with pytest.raises(ActivityError):
            register(task, **links)
    assert [r['run_id'] for r in store.trace(task)['runs']] == [base['run_id']]
    assert store.ingest(base['token'], event(base))['status'] == 'accepted'
    assert register(task)['epoch'] == 2


@pytest.mark.parametrize('links', [{'step_id': ''}, {'step_id': 'a' * 49}, {'step_id': 'p 1'},
                                   {'step_id': 'p1\n'}, {'step_id': 'p/1'}, {'step_id': 'ü1'},
                                   {'step_id': True}, {'step_id': 1}, {'parent_run_id': 'A' * 32},
                                   {'parent_run_id': True}, {'parent_run_id': 'x'},
                                   {'parent_run_id': uuid4().hex + '\n'}, {'retry_of': 1}])
def test_malformed_lineage_is_refused_without_registration(setup, links):
    store, register, _, _ = setup
    task = uuid4().hex
    with pytest.raises(ActivityError, match='Invalid'):
        register(task, **links)
    assert store.trace(task)['runs'] == []
    assert register(task, step_id='a' * 48)['epoch'] == 1


def test_trace_filters_task_in_storage_and_bounds_output(setup):
    store, register, _, _ = setup
    task = uuid4().hex
    first, second = register(task, 'codex:a'), register(task, 'codex:b')
    for run in (first, second):
        store.ingest(run['token'], event(run, kind='start'))
        store.ingest(run['token'], event(run, 2))
    for _ in range(3):
        register(uuid4().hex)
    assert task not in {r['task_id'] for r in store.inspect(limit=1)['runs']}
    assert [r['run_id'] for r in store.trace(task)['runs']] == [first['run_id'], second['run_id']]
    store.MAX_EVENTS = 3
    trace = store.trace(task)
    assert [len(r['events']) for r in trace['runs']] == [2, 1]
    assert trace['runs'][1]['events_has_more'] and trace['has_more'] and not trace['trace_complete']
    store.MAX_RUNS = 1
    trace = store.trace(task)
    assert len(trace['runs']) == 1 and trace['has_more']


def test_unknown_or_invalid_task_and_trace_never_writes(setup):
    store, register, _, _ = setup
    unknown = store.trace(uuid4().hex)
    assert unknown['status'] == 'unknown' and unknown['runs'] == [] and not unknown['trace_complete']
    assert unknown['verified'] == []
    for task in (True, None, 'A' * 32, 'x', 1):
        with pytest.raises(ActivityError):
            store.trace(task)
    task = uuid4().hex
    run = register(task)
    store.ingest(run['token'], event(run, kind='start'))
    path = store.guard.workspace / store.DIRECTORY / 'activity.sqlite3'
    before = path.read_bytes()
    store.trace(task)
    assert path.read_bytes() == before


def test_secret_token_absent_from_trace(setup):
    store, register, _, _ = setup
    task = uuid4().hex
    parent = register(task, 'codex:lead')
    child = register(task, 'codex:a', parent_run_id=parent['run_id'], step_id='p1')
    store.ingest(child['token'], event(child, kind='start'))
    dumped = json.dumps(store.trace(task))
    assert parent['token'] not in dumped and child['token'] not in dumped
    assert 'token' not in dumped


def test_truncated_run_cannot_claim_complete_or_leak_hidden_result_refs(setup):
    store, register, _, _ = setup
    task = uuid4().hex
    run = register(task)
    store.ingest(run['token'], event(run, kind='start'))
    reference = uuid4().hex
    store.ingest(run['token'], event(run, 2, kind='completed', phase='finished', result_ref=reference))
    store.MAX_EVENTS = 1
    trace = store.trace(task)
    item = trace['runs'][0]
    assert item['events_has_more'] and not item['trace_complete']
    assert item['result_refs'] == []
    assert trace['has_more'] and not trace['trace_complete']
