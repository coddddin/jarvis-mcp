"""Injected activity collectors observe proposals without changing provider dispatch."""
import json
import os
from types import SimpleNamespace
import time
from uuid import uuid4

import pytest

from jarvis.agents.activity import ActivityError, ActivityStore
from jarvis.agents.chat import ChatError, ChatStore
from jarvis.agents.team import HybridTeam
from jarvis.core.guardrails import Guardrails


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
