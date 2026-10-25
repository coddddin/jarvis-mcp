"""Injected activity collectors observe proposals without changing provider dispatch."""
import json
import os
import sqlite3
import subprocess
from types import SimpleNamespace
import time
from uuid import uuid4

import pytest

from jarvis.agents.activity import ActivityError, ActivityStore
from jarvis.agents.chat import ChatError, ChatStore
from jarvis.agents.team import HybridTeam
from jarvis.core.guardrails import GuardrailError, Guardrails


@pytest.mark.parametrize('mode', ['success', 'failed', 'collector_unavailable', 'headless'])
def test_external_spawn_observed_without_duplicate_calls_or_fake_verification(tmp_path, monkeypatch, mode):
    guard = Guardrails(tmp_path)
    child = SimpleNamespace(pid=os.getpid() + 100, returncode=int(mode == 'failed'),
                            poll=lambda: 0, communicate=lambda *a, **k:
                            (json.dumps({'structured_output': {'summary': 'proposal', 'output': 'complete text'}}).encode(), None))
    observed = SimpleNamespace(create_time=lambda: 1000., ppid=os.getpid,
                               is_running=lambda: True, status=lambda: 'running')
    activity = ActivityStore(guard, lookup=lambda pid: observed)
    team = HybridTeam(ChatStore(guard), None, codex='unused', claude='unused')
    if mode != 'headless':
        team.activity_store = activity
    if mode == 'collector_unavailable':
        def unavailable(*args, **kwargs):
            raise ActivityError('fixture unavailable')
        monkeypatch.setattr(activity, 'register', unavailable)
    folder = tmp_path / '.jarvis-chat' / f'team-{uuid4().hex}-{uuid4().hex}'
    folder.mkdir()
    calls = []
    monkeypatch.setattr(team, '_external_command', lambda *args: ['fixture-not-executed'])
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', lambda *a, **k: calls.append(1) or child)
    monkeypatch.setattr('jarvis.agents.team.observe_spawned', lambda *a, **k: None)
    if mode == 'failed':
        with pytest.raises(ChatError):
            team._external('claude-code', {}, folder, 'p1', time.monotonic() + 30)
    else:
        assert team._external('claude-code', {}, folder, 'p1', time.monotonic() + 30).output == 'complete text'
    assert calls == [1]
    runs = activity.inspect()['runs']
    if mode in ('headless', 'collector_unavailable'):
        assert runs == []
    else:
        assert len(runs) == 1 and runs[0]['verified'] == []
        kinds = [e['event']['kind'] for e in activity.history(runs[0]['run_id'])['events']]
        assert kinds == (['start', 'failed'] if mode == 'failed' else ['start', 'result_received', 'completed'])


def test_parallel_packets_have_distinct_epoch_slots_and_corrections_fence_only_their_packet(tmp_path):
    guard = Guardrails(tmp_path)
    observed = SimpleNamespace(create_time=lambda: 1000., ppid=os.getpid,
                               is_running=lambda: True, status=lambda: 'running')
    activity = ActivityStore(guard, lookup=lambda pid: observed)
    team = HybridTeam(ChatStore(guard), None, codex='unused', claude='unused')
    team.activity_store = activity
    folder = tmp_path / '.jarvis-chat' / f'team-{uuid4().hex}-{uuid4().hex}'
    folder.mkdir()
    process = SimpleNamespace(pid=os.getpid() + 100)
    reporters = []
    try:
        for name in ('p1-r0', 'p2-r0'):
            reporters.append(team._activity_reporter(folder, 'claude-code', process, name))
        assert all(not run['fenced'] for run in activity.inspect()['runs'])
        reporters.append(team._activity_reporter(folder, 'claude-code', process, 'p1-r1'))
        runs = activity.inspect()['runs']
        assert sum(run['fenced'] for run in runs) == 1
        assert not next(run for run in runs if run['owner'].endswith('p2'))['fenced']
    finally:
        for reporter in reporters:
            reporter.close()


def test_pipe_cleanup_timeout_does_not_block_reporter_close_or_repeat_provider(tmp_path, monkeypatch, caplog):
    guard = Guardrails(tmp_path)
    team = HybridTeam(ChatStore(guard), None, codex='unused', claude='unused')
    folder = tmp_path / '.jarvis-chat' / f'team-{uuid4().hex}-{uuid4().hex}'
    folder.mkdir()
    timeouts, spawned, closed = [], [], []
    def communicate(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        if kwargs['timeout'] == 2:
            raise subprocess.TimeoutExpired('fixture-cli', 2)
        return b'{"structured_output":{"summary":"ok","output":"proposal"}}', None
    child = SimpleNamespace(returncode=0, poll=lambda: 0, communicate=communicate)
    reporter = SimpleNamespace(send=lambda *a, **k: True, close=lambda: closed.append(True))
    monkeypatch.setattr(team, '_activity_reporter', lambda *args: reporter)
    monkeypatch.setattr(team, '_external_command', lambda *args: ['fixture-not-executed'])
    monkeypatch.setattr('jarvis.agents.team.observe_spawned', lambda *a, **k: None)
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', lambda *a, **k: spawned.append(True) or child)
    assert team._external('claude-code', {}, folder, 'p1-r0', time.monotonic() + 30).output == 'proposal'
    assert timeouts == [0.2, 2] and closed == [True] and spawned == [True]
    assert 'cleanup exceeded' in caplog.text


PID = os.getpid() + 100
OBSERVED = SimpleNamespace(create_time=lambda: 1000., ppid=os.getpid,
                           is_running=lambda: True, status=lambda: 'running')


def observed_team(guard, request):
    activity = ActivityStore(guard, lookup=lambda pid: OBSERVED)
    team = HybridTeam(ChatStore(guard), None, codex='unused', claude='unused')
    team.activity_store = activity
    folder = guard.workspace / '.jarvis-chat' / f'team-{"c" * 32}-{request}'
    folder.mkdir(exist_ok=True)
    return activity, team, folder


def fake_spawn(team, monkeypatch, returncode=0):
    calls = []
    child = SimpleNamespace(pid=PID, returncode=returncode, poll=lambda: 0, communicate=lambda *a, **k:
                            (json.dumps({'structured_output': {'summary': 'proposal', 'output': 'complete text'}}).encode(), None))
    monkeypatch.setattr(team, '_external_command', lambda *args: ['fixture-not-executed'])
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', lambda *a, **k: calls.append(1) or child)
    monkeypatch.setattr('jarvis.agents.team.observe_spawned', lambda *a, **k: None)
    return calls


def lineage(step, retry_of=None):
    return {'source': 'registered', 'parent_run_id': None, 'step_id': step, 'retry_of': retry_of}


def test_packets_are_separate_steps_and_corrections_retry_their_latest_run(tmp_path, monkeypatch):
    request = uuid4().hex
    activity, team, folder = observed_team(Guardrails(tmp_path), request)
    calls = fake_spawn(team, monkeypatch)
    for name in ('p1-r0', 'p2-r0', 'p1-r1', 'p1-r2'):
        assert team._external('claude-code', {}, folder, name, time.monotonic() + 30).output == 'complete text'
    assert len(calls) == 4
    trace = activity.trace(request)
    runs = {(run['owner'], run['epoch']): run for run in trace['runs']}
    assert len(runs) == 4  # No parent or extra run is fabricated.
    p1 = [runs['claude-code:jarvis-p1', epoch] for epoch in (1, 2, 3)]
    p2 = runs['claude-code:jarvis-p2', 1]
    assert [run['lineage'] for run in p1] == [lineage('p1'), lineage('p1', p1[0]['run_id']),
                                              lineage('p1', p1[1]['run_id'])]
    assert p2['lineage'] == lineage('p2') and not p2['fenced']
    assert [run['fenced'] for run in p1] == [True, True, False]
    assert sorted((link['run_id'], link['target']) for link in trace['links']) == sorted(
        [(p1[1]['run_id'], p1[0]['run_id']), (p1[2]['run_id'], p1[1]['run_id'])])
    assert all(link['type'] == 'retry_of' and link['step_id'] == 'p1' and link['in_trace'] for link in trace['links'])
    assert all(run['end'] == {'source': 'self_reported', 'kind': 'completed', 'seq': 3}
               and run['report']['source'] == 'self_reported' and run['verified'] == [] for run in trace['runs'])
    assert trace['verified'] == [] and trace['control_supported'] is False


def test_reopened_collector_and_new_team_resolve_prior_run_from_durable_trace(tmp_path):
    guard, request = Guardrails(tmp_path), uuid4().hex
    _, first, folder = observed_team(guard, request)
    first._activity_reporter(folder, 'codex', SimpleNamespace(pid=PID), 'p1-r0').close()
    reopened, second, _ = observed_team(guard, request)
    reporter = second._activity_reporter(folder, 'codex', SimpleNamespace(pid=PID), 'p1-r1')
    try:
        trace = reopened.trace(request)
    finally:
        reporter.close()
    old, retry = sorted(trace['runs'], key=lambda run: run['epoch'])
    assert old['lineage'] == lineage('p1') and retry['lineage'] == lineage('p1', old['run_id'])
    assert trace['links'] == [{'run_id': retry['run_id'], 'type': 'retry_of', 'target': old['run_id'],
                               'step_id': 'p1', 'in_trace': True}]
    assert old['fenced'] and not retry['fenced'] and old['heartbeat']['reason'] == 'collector_restarted'
    # Unobserved endings stay unknown; nothing is marked complete or verified.
    assert old['end'] == retry['end'] == {'source': 'unknown', 'kind': None, 'seq': None}
    assert not trace['trace_complete'] and trace['verified'] == []


@pytest.mark.parametrize('history', ['missing', 'legacy', 'foreign_request', 'other_runtime', 'other_step', 'parented'])
def test_correction_without_matching_registered_history_never_guesses_retry(tmp_path, history):
    request = uuid4().hex
    activity, team, folder = observed_team(Guardrails(tmp_path), request)
    owner = 'claude-code:jarvis-p1'
    if history == 'legacy':
        # Same owner and step, but lineage unknown (registered before lineage existed).
        activity.register(owner, request, pid=PID, step_id='p1')
        db = sqlite3.connect(tmp_path / ActivityStore.DIRECTORY / 'activity.sqlite3')
        db.execute('UPDATE runs SET lineage=NULL')
        db.commit()
        db.close()
    elif history == 'foreign_request':
        activity.register(owner, uuid4().hex, pid=PID, step_id='p1')
    elif history == 'other_runtime':
        activity.register('codex:jarvis-p1', request, pid=PID, step_id='p1')
    elif history == 'other_step':
        activity.register(owner, request, pid=PID, step_id='p2')
    elif history == 'parented':
        parent = activity.register('claude-code:jarvis-lead', request, pid=PID)
        activity.register(owner, request, pid=PID, step_id='p1', parent_run_id=parent['run_id'])
    before = {run['run_id'] for run in activity.trace(request)['runs']}
    reporter = team._activity_reporter(folder, 'claude-code', SimpleNamespace(pid=PID), 'p1-r1')
    try:
        trace = activity.trace(request)
    finally:
        reporter.close()
    new = [run for run in trace['runs'] if run['run_id'] not in before]
    assert len(new) == 1 and len(trace['runs']) == len(before) + 1
    assert new[0]['lineage'] == lineage('p1') and new[0]['owner'] == owner
    assert all(link['run_id'] != new[0]['run_id'] for link in trace['links'])


def test_initial_packet_never_infers_retry_even_with_prior_row(tmp_path):
    request = uuid4().hex
    activity, team, folder = observed_team(Guardrails(tmp_path), request)
    for name in ('p1-r0', 'p1-r0', 'p1'):
        team._activity_reporter(folder, 'claude-code', SimpleNamespace(pid=PID), name).close()
    trace = activity.trace(request)
    assert [run['lineage'] for run in trace['runs']] == [lineage('p1')] * 3 and trace['links'] == []


@pytest.mark.parametrize('returncode', [0, 1])
@pytest.mark.parametrize('error', [ActivityError, GuardrailError, OSError])
def test_step_read_failure_observes_nothing_and_provider_runs_once(tmp_path, monkeypatch, caplog, error, returncode):
    request = uuid4().hex
    activity, team, folder = observed_team(Guardrails(tmp_path), request)
    prior = activity.register('claude-code:jarvis-p1', request, pid=PID, step_id='p1')
    read, registered = activity.trace, []
    def unreadable(task_id):
        raise error('fixture trace unavailable')
    monkeypatch.setattr(activity, 'trace', unreadable)
    monkeypatch.setattr(activity, 'register', lambda *a, **k: registered.append(1))
    calls = fake_spawn(team, monkeypatch, returncode)
    if returncode:
        with pytest.raises(ChatError, match='External worker failed'):
            team._external('claude-code', {}, folder, 'p1-r1', time.monotonic() + 30)
    else:
        assert team._external('claude-code', {}, folder, 'p1-r1', time.monotonic() + 30).output == 'complete text'
    assert calls == [1] and registered == []  # No registration without lineage, no repeated provider call.
    assert 'observation unavailable' in caplog.text
    progress = (folder / 'worker-progress.jsonl').read_text(encoding='utf-8').splitlines()
    assert [json.loads(line)['event'] for line in progress] == ['start', 'failed' if returncode else 'result']
    trace = read(request)
    assert [run['run_id'] for run in trace['runs']] == [prior['run_id']] and trace['runs'][0]['events'] == []


def test_step_read_failure_rechecks_stop_before_returning(tmp_path, monkeypatch):
    request = uuid4().hex
    activity, team, folder = observed_team(Guardrails(tmp_path), request)
    registered = []
    def stopped(task_id):
        (tmp_path / '.jarvis-stop').write_text('stop')
        raise OSError('fixture trace unavailable')
    monkeypatch.setattr(activity, 'trace', stopped)
    monkeypatch.setattr(activity, 'register', lambda *a, **k: registered.append(1))
    with pytest.raises(GuardrailError):
        team._activity_reporter(folder, 'claude-code', SimpleNamespace(pid=PID), 'p1-r1')
    assert registered == []
