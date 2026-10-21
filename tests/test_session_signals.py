"""Lifecycle hooks must not leak context or change the agent's control flow."""
import io
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import pytest

from jarvis.agents.session_signals import CODEX_EVENTS, normalize_codex_event, render_codex_hooks, within_project
from jarvis.cli.session_signal import MAX_INPUT_BYTES, main, read_event


def payload(tmp_path, event='PreToolUse', **extra):
    return {'cwd': str(tmp_path), 'session_id': 'session-1', 'turn_id': 'turn-1',
            'hook_event_name': event, 'prompt': 'PRIVATE PROMPT',
            'tool_input': {'command': 'PRIVATE COMMAND', 'password': 'NEVER STORE'},
            'tool_response': 'PRIVATE RESULT', 'transcript_path': 'NEVER OPEN', **extra}


@pytest.mark.parametrize('event', CODEX_EVENTS)
def test_only_fixed_metadata_survives_each_supported_event(tmp_path, event):
    signal = normalize_codex_event(payload(tmp_path, event))
    assert set(signal) == {'employee', 'session_id', 'event_id', 'turn_id', 'event', 'source'}
    assert signal['event'] == CODEX_EVENTS[event] and signal['source'] == 'self_reported'
    assert re.fullmatch('[a-f0-9]{32}', signal['event_id'])
    assert signal['turn_id'] == (None if event in ('SessionStart', 'SessionEnd') else 'turn-1')
    serialized = json.dumps(signal)
    assert all(value not in serialized for value in ('PRIVATE', 'NEVER', str(tmp_path), 'command', 'password'))


@pytest.mark.parametrize('change', [
    {'session_id': 'sk-sensitive'}, {'session_id': 'ghp_secret'}, {'session_id': 'x\nPRIVATE'},
    {'session_id': True}, {'turn_id': None}, {'turn_id': '../PRIVATE'},
    {'hook_event_name': 'SubagentStop'}, {'hook_event_name': []},
])
def test_malformed_missing_and_secret_shaped_identifiers_are_ignored(tmp_path, change):
    assert normalize_codex_event(payload(tmp_path, **change)) is None


def test_project_scope_uses_cwd_but_does_not_accept_other_roots(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    child = project / 'sub'
    child.mkdir()
    assert within_project({'cwd': str(child)}, project)
    assert not within_project({'cwd': str(tmp_path)}, project)
    assert not within_project({'cwd': str(project / '..')}, project)
    assert not within_project({'cwd': 'relative'}, project)


def test_hook_main_reports_without_stdout_or_raw_context(tmp_path, capsys):
    sent = []
    assert main(['--workspace', str(tmp_path), '--project-root', str(tmp_path)],
                stream=io.BytesIO(json.dumps(payload(tmp_path)).encode()),
                report=lambda workspace, signal: sent.append((workspace, signal))) == 0
    assert len(sent) == 1 and sent[0][1]['event'] == 'tool_started'
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('raw', [b'{bad json', b'\xff', b'[]', b'x' * (MAX_INPUT_BYTES + 1)],
                         ids=['bad_json', 'bad_utf8', 'wrong_shape', 'oversized'])
def test_bad_input_never_blocks_or_echoes(tmp_path, raw, capsys):
    assert main(['--workspace', str(tmp_path), '--project-root', str(tmp_path)],
                stream=io.BytesIO(raw), report=lambda *args: pytest.fail('must not report')) == 0
    assert capsys.readouterr() == ('', '')


def test_storage_errors_and_invalid_args_are_fail_open_and_silent(tmp_path, capsys):
    def failure(*args):
        raise RuntimeError('SECRET ERROR CONTENT')
    args = ['--workspace', str(tmp_path), '--project-root', str(tmp_path)]
    assert main(args, stream=io.BytesIO(json.dumps(payload(tmp_path)).encode()), report=failure) == 0
    assert main(['--unsupported']) == 0
    assert capsys.readouterr() == ('', '')


def test_read_event_accepts_only_bounded_utf8_json():
    assert read_event(io.BytesIO(b'{"hook_event_name":"Stop"}')) == {'hook_event_name': 'Stop'}
    assert read_event(io.StringIO('not bytes')) is None


def test_renderer_produces_real_windows_definition_without_installing(tmp_path):
    script = tmp_path / 'hook script.py'
    script.write_text('# observer')
    config = render_codex_hooks(python=Path(sys.executable), script=script,
                                workspace=tmp_path, project_root=tmp_path)
    assert set(config['hooks']) == set(CODEX_EVENTS)
    handler = config['hooks']['Stop'][0]['hooks'][0]
    assert handler['type'] == 'command' and handler['timeout'] == 2
    assert '"' + str(script) + '"' in handler['commandWindows']
    assert 'bypass' not in json.dumps(config)
    assert not (tmp_path / '.codex').exists()


def test_renderer_rejects_command_metacharacters(tmp_path):
    script = tmp_path / 'hook&extra.py'
    script.write_text('# never execute')
    with pytest.raises(ValueError):
        render_codex_hooks(python=Path(sys.executable), script=script, workspace=tmp_path, project_root=tmp_path)


def test_absolute_cli_smoke_does_not_depend_on_working_directory(tmp_path):
    from jarvis.cli import session_signal
    result = subprocess.run([sys.executable, str(Path(session_signal.__file__).resolve()),
                             '--workspace', str(tmp_path), '--project-root', str(tmp_path)],
                            cwd=tmp_path, input=b'{}', capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stdout == b'' and result.stderr == b''
