import json

import pytest

from jarvis.cli.worker_view import read_events
from jarvis.core.guardrails import Guardrails

CONVERSATION, REQUEST = 'a' * 32, 'b' * 32


def progress_file(tmp_path):
    folder = tmp_path / '.jarvis-chat' / f'team-{CONVERSATION}-{REQUEST}'
    folder.mkdir(parents=True)
    return folder / 'worker-progress.jsonl'


def test_reads_complete_events_and_removes_terminal_controls(tmp_path):
    path = progress_file(tmp_path)
    record = {'runtime': 'claude-code', 'name': 'p1', 'event': 'progress', 'text': 'working\x1b\x00'}
    path.write_bytes((json.dumps(record) + '\n{"runtime":').encode())
    result = read_events(Guardrails(tmp_path), CONVERSATION, REQUEST)
    assert result == ['[claude-code / p1 / progress] working']


def test_missing_log_is_waiting_and_paths_are_fixed(tmp_path):
    assert read_events(Guardrails(tmp_path), CONVERSATION, REQUEST) == []
    with pytest.raises(Exception):
        read_events(Guardrails(tmp_path), '../private', REQUEST)


@pytest.mark.parametrize('content', [b'{}\n', b'x' * 8193 + b'\n', b'x' * 262145], ids=['invalid', 'large-line', 'large-file'])
def test_invalid_or_oversized_logs_are_not_displayed(tmp_path, content):
    progress_file(tmp_path).write_bytes(content)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        read_events(Guardrails(tmp_path), CONVERSATION, REQUEST)


def test_visible_console_starts_observer_only(tmp_path, monkeypatch):
    import jarvis.cli.worker_view as viewer
    calls = []
    monkeypatch.setattr(viewer.os, 'name', 'nt')
    monkeypatch.setattr(viewer.subprocess, 'CREATE_NEW_CONSOLE', 16, raising=False)
    monkeypatch.setattr(viewer.subprocess, 'Popen', lambda args, **kwargs: calls.append((args, kwargs)))
    viewer.open_worker_view(Guardrails(tmp_path), CONVERSATION, REQUEST)
    args, options = calls[0]
    assert args[1:3] == ['-m', 'jarvis.cli.worker_view']
    assert options['creationflags'] == 16
    assert options['close_fds'] is True
    with pytest.raises(Exception):
        viewer.open_worker_view(Guardrails(tmp_path), '../bad', REQUEST)
    assert len(calls) == 1
