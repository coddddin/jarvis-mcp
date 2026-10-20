"""External worker observations use mocks, never paid model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from types import SimpleNamespace
import time
from uuid import uuid4

import pytest

from jarvis.agents.chat import ChatError, ChatStore
from jarvis.agents.team import HybridTeam
from jarvis.core.guardrails import Guardrails


@pytest.fixture
def worker(tmp_path, monkeypatch):
    team = HybridTeam(ChatStore(Guardrails(tmp_path)), None, codex='missing', claude='missing')
    folder = tmp_path / 'observations'
    folder.mkdir()
    monkeypatch.setattr(team, '_external_command', lambda *args: ['fixture-never-executed'])
    monkeypatch.setattr('jarvis.agents.team.observe_spawned', lambda *args, **kwargs: None)
    return team, folder


def records(folder):
    return [json.loads(line) for line in (folder / 'worker-progress.jsonl').read_text(encoding='utf-8').splitlines()]


@pytest.mark.parametrize('failed', [False, True])
def test_real_call_boundary_records_only_fixed_status_not_provider_output(worker, monkeypatch, failed):
    team, folder = worker
    raw = json.dumps({'structured_output': {'summary': 'sk-sensitive', 'output': 'private proposal'}}).encode()
    process = SimpleNamespace(returncode=int(failed), poll=lambda: 0)
    def communicate(*args, **kwargs):
        assert records(folder)[0]['event'] == 'start'
        return raw, None
    process.communicate = communicate
    calls = []
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', lambda *args, **kwargs: calls.append(args) or process)
    if failed:
        with pytest.raises(ChatError):
            team._external('claude-code', {'request': 'SECRET REQUEST'}, folder, 'p1-version-0', time.monotonic() + 30)
    else:
        team._external('claude-code', {'request': 'SECRET REQUEST'}, folder, 'p1-version-0', time.monotonic() + 30)
    assert len(calls) == 1
    assert [r['event'] for r in records(folder)] == ['start', 'failed' if failed else 'result']
    text = (folder / 'worker-progress.jsonl').read_text()
    assert all(secret not in text for secret in ('sk-sensitive', 'private proposal', 'SECRET REQUEST'))


def test_concurrent_events_remain_complete_json_lines(worker):
    team, folder = worker
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert all(pool.map(lambda _: team._worker_event(folder, 'codex', 'p1', 'start'), range(30)))
    assert len(records(folder)) == 30


def test_oversized_or_hardlinked_log_is_never_appended(worker, tmp_path):
    team, folder = worker
    path = folder / 'worker-progress.jsonl'
    path.write_bytes(b'x' * 262144)
    assert not team._worker_event(folder, 'codex', 'p1', 'start')
    assert path.stat().st_size == 262144
    path.unlink()
    target = tmp_path / 'keep.txt'
    target.write_text('unchanged')
    os.link(target, path)
    assert not team._worker_event(folder, 'codex', 'p1', 'start')
    assert target.read_text() == 'unchanged'


def test_stopped_or_untrusted_labels_do_not_write(worker):
    team, folder = worker
    assert not team._worker_event(folder, 'codex', '\x1b[31msecret', 'start')
    (team.guard.workspace / '.jarvis-stop').touch()
    assert not team._worker_event(folder, 'codex', 'p1', 'start')
    assert not (folder / 'worker-progress.jsonl').exists()


def test_logging_failure_does_not_retry_model_or_mask_original_error(worker, monkeypatch):
    team, folder = worker
    (folder / 'worker-progress.jsonl').mkdir()
    calls = []
    def spawn(*args, **kwargs):
        calls.append(1)
        raise OSError('fixture spawn denied')
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', spawn)
    with pytest.raises(OSError, match='fixture spawn denied'):
        team._external('claude-code', {}, folder, 'p1', time.monotonic() + 30)
    assert len(calls) == 1


def test_injected_view_opens_once_for_real_request_folder_and_never_retries(worker):
    team, _ = worker
    conversation, request = uuid4().hex, uuid4().hex
    folder = team.guard.workspace / '.jarvis-chat' / f'team-{conversation}-{request}'
    folder.mkdir()
    calls = []
    def viewer(*args):
        calls.append(args)
        raise OSError('observer unavailable')
    team.worker_view = viewer
    team._notify_worker_view(folder, 'codex')
    assert calls == []
    team._notify_worker_view(folder, 'claude-code')
    team._notify_worker_view(folder, 'claude-code')
    assert calls == [(conversation, request)]


def test_view_callback_observes_spawn_and_start_before_result(worker, monkeypatch):
    team, _ = worker
    conversation, request = uuid4().hex, uuid4().hex
    folder = team.guard.workspace / '.jarvis-chat' / f'team-{conversation}-{request}'
    folder.mkdir()
    calls = []
    def spawn(*args, **kwargs):
        calls.append('spawn')
        return SimpleNamespace(returncode=0, poll=lambda: 0, communicate=lambda *a, **k:
            (b'{"structured_output":{"summary":"ok","output":"proposal"}}', None))
    def viewer(*args):
        assert records(folder)[0]['event'] == 'start'
        calls.append('view')
    team.worker_view = viewer
    monkeypatch.setattr('jarvis.agents.team.subprocess.Popen', spawn)
    team._external('claude-code', {}, folder, 'p1', time.monotonic() + 30)
    assert calls == ['spawn', 'view']
    assert records(folder)[-1]['event'] == 'result'
