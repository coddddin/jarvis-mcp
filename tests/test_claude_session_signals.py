"""Claude Code hooks -> the shared content-free session signal (ff9bcf39). Codex modules are only imported."""
import io
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from jarvis.agents.claude_session_signals import (CLAUDE_EVENTS, UNMAPPED, normalize_claude_event,
                                                  render_claude_hooks)
from jarvis.cli.claude_session_signal import main

PROMPT_ID = '550e8400-e29b-41d4-a716-446655440000'
SESSION_EVENTS = ('SessionStart', 'SessionEnd')


def payload(tmp_path, event='PreToolUse', **extra):
    """Shape of the documented Claude Code hook input, with private content that must never survive."""
    return {'session_id': 'abc123', 'prompt_id': PROMPT_ID, 'transcript_path': 'NEVER OPEN',
            'cwd': str(tmp_path), 'permission_mode': 'default', 'hook_event_name': event,
            'prompt': 'PRIVATE PROMPT', 'tool_name': 'Bash',
            'tool_input': {'command': 'PRIVATE COMMAND', 'password': 'NEVER STORE'},
            'tool_response': 'PRIVATE RESULT', 'last_assistant_message': 'PRIVATE ANSWER', **extra}


@pytest.mark.parametrize('event', CLAUDE_EVENTS)
def test_only_fixed_metadata_survives_each_supported_event(tmp_path, event):
    signal = normalize_claude_event(payload(tmp_path, event))
    assert set(signal) == {'employee', 'session_id', 'event_id', 'turn_id', 'event', 'source'}
    assert signal['employee'] == 'claude-code' and signal['source'] == 'self_reported'
    assert signal['event'] == CLAUDE_EVENTS[event] and re.fullmatch('[a-f0-9]{32}', signal['event_id'])
    assert signal['turn_id'] == (None if event in SESSION_EVENTS else PROMPT_ID)   # turn_id = prompt_id
    serialized = json.dumps(signal)
    assert all(value not in serialized for value in ('PRIVATE', 'NEVER', str(tmp_path), 'command', 'password'))


def test_mapping_is_the_approved_one():
    assert CLAUDE_EVENTS == {
        'SessionStart': 'session_started', 'UserPromptSubmit': 'turn_started', 'PreToolUse': 'tool_started',
        'PostToolUse': 'tool_finished', 'PostToolUseFailure': 'tool_finished',
        'PermissionRequest': 'permission_requested', 'Stop': 'turn_stopped', 'StopFailure': 'turn_stopped',
        'SessionEnd': 'session_ended'}
    assert 'interrupted' not in CLAUDE_EVENTS.values() and 'interrupted' in UNMAPPED


@pytest.mark.parametrize('change', [
    {'prompt_id': None}, {'prompt_id': '../PRIVATE'}, {'prompt_id': 'sk-sensitive'}, {'prompt_id': 7},
    {'session_id': 'ghp_secret'}, {'session_id': 'x\nPRIVATE'}, {'session_id': True},
    {'hook_event_name': 'Notification'}, {'hook_event_name': 'SubagentStop'}, {'hook_event_name': 'Interrupt'},
    {'hook_event_name': []},
])
def test_turn_events_without_a_valid_prompt_id_and_unknown_events_are_dropped(tmp_path, change):
    assert normalize_claude_event(payload(tmp_path, **change)) is None


def test_missing_prompt_id_is_dropped_not_guessed(tmp_path):
    raw = payload(tmp_path, 'Stop')
    raw.pop('prompt_id')
    assert normalize_claude_event(raw) is None


@pytest.mark.parametrize('event', SESSION_EVENTS)
def test_session_events_need_no_prompt_id(tmp_path, event):
    raw = payload(tmp_path, event)
    raw.pop('prompt_id')   # documented as absent until the first user input
    assert normalize_claude_event(raw)['turn_id'] is None


def test_hook_main_reports_without_stdout(tmp_path, capsys):
    sent = []
    assert main(['--workspace', str(tmp_path), '--project-root', str(tmp_path)],
                stream=io.BytesIO(json.dumps(payload(tmp_path)).encode()),
                report=lambda workspace, signal: sent.append((workspace, signal))) == 0
    assert len(sent) == 1 and sent[0][1]['event'] == 'tool_started' and sent[0][1]['employee'] == 'claude-code'
    assert capsys.readouterr() == ('', '')


def test_hook_main_ignores_other_projects(tmp_path, capsys):
    project = tmp_path / 'project'
    project.mkdir()
    sent = []
    main(['--workspace', str(tmp_path), '--project-root', str(project)],
         stream=io.BytesIO(json.dumps(payload(tmp_path)).encode()), report=lambda *a: sent.append(a))
    assert sent == [] and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('raw', [b'', b'not json', b'[]', b'\xff\xfe', b'{' * 5000, b'x' * 300000],
                         ids=['empty', 'text', 'list', 'not-utf8', 'deep', 'oversized'])
def test_bad_input_never_blocks_or_echoes(tmp_path, raw, capsys):
    assert main(['--workspace', str(tmp_path), '--project-root', str(tmp_path)], stream=io.BytesIO(raw),
                report=lambda *a: pytest.fail('nothing to report')) == 0
    assert capsys.readouterr() == ('', '')


def test_storage_errors_and_invalid_args_are_fail_open_and_silent(tmp_path, capsys):
    def broken(workspace, signal):
        raise RuntimeError('PRIVATE store failure')
    assert main(['--workspace', str(tmp_path), '--project-root', str(tmp_path)],
                stream=io.BytesIO(json.dumps(payload(tmp_path)).encode()), report=broken) == 0
    assert main(['--unknown'], stream=io.BytesIO(b'{}')) == 0
    assert capsys.readouterr() == ('', '')


def test_renderer_uses_exec_form_async_and_short_timeouts(tmp_path):
    script = tmp_path / 'hook script.py'
    script.write_text('# observer')
    config = render_claude_hooks(python=Path(sys.executable), script=script, workspace=tmp_path, project_root=tmp_path)
    assert set(config['hooks']) == set(CLAUDE_EVENTS)
    for name, groups in config['hooks'].items():
        handler = groups[0]['hooks'][0]
        assert 'matcher' not in groups[0]                          # omitted matcher: every tool
        assert handler['type'] == 'command' and handler['async'] is True and handler['timeout'] <= 5
        assert handler['command'] == str(Path(sys.executable))     # exec form: no shell string to quote
        assert handler['args'] == [str(script), '--workspace', str(tmp_path), '--project-root', str(tmp_path)]
    assert 'bypass' not in json.dumps(config) and not (tmp_path / '.claude').exists()


@pytest.mark.parametrize('name', ['hook&extra.py', 'hook;x.py', 'hook%PATH%.py', 'hook$x.py'])
def test_renderer_rejects_command_metacharacters(tmp_path, name):
    script = tmp_path / name
    script.write_text('# never execute')
    with pytest.raises(ValueError):
        render_claude_hooks(python=Path(sys.executable), script=script, workspace=tmp_path, project_root=tmp_path)


def test_renderer_rejects_missing_paths(tmp_path):
    with pytest.raises(ValueError):
        render_claude_hooks(python=Path(sys.executable), script=tmp_path / 'absent.py',
                            workspace=tmp_path, project_root=tmp_path)


def test_example_file_matches_the_renderer_shape():
    example = json.loads(Path('config/session-hooks.claude.example.json').read_text(encoding='utf-8'))
    assert set(example['hooks']) == set(CLAUDE_EVENTS)
    for groups in example['hooks'].values():
        handler = groups[0]['hooks'][0]
        assert handler['async'] is True and handler['command'] == '__ABSOLUTE_PYTHON__'
        assert handler['args'][0].endswith('jarvis/cli/claude_session_signal.py')
    assert 'interrupted' in example['description']   # the unmapped event is stated, not hidden


def test_absolute_cli_smoke_does_not_depend_on_working_directory(tmp_path):
    from jarvis.cli import claude_session_signal
    result = subprocess.run([sys.executable, str(Path(claude_session_signal.__file__).resolve()),
                             '--workspace', str(tmp_path), '--project-root', str(tmp_path)],
                            cwd=tmp_path, input=b'{}', capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stdout == b'' and result.stderr == b''


def _store_accepts_claude():
    from jarvis.agents.session_status import SessionSignal
    try:
        SessionSignal.model_validate({'employee': 'claude-code', 'session_id': 's', 'event_id': 'a' * 32,
                                      'turn_id': None, 'event': 'session_started', 'source': 'self_reported'})
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _store_accepts_claude(),
                    reason="SessionStatusStore accepts only employee='codex' until board 2ebac8cf lands")
def test_real_cli_persists_claude_metadata_only(tmp_path):
    from jarvis.agents.session_status import SessionStatusStore
    from jarvis.cli import claude_session_signal
    from jarvis.core.guardrails import Guardrails
    project, workspace = tmp_path / 'project', tmp_path / 'data'
    project.mkdir(), workspace.mkdir()
    command = [sys.executable, str(Path(claude_session_signal.__file__).resolve()),
               '--workspace', str(workspace), '--project-root', str(project)]
    for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop'):
        result = subprocess.run(command, input=json.dumps(payload(project, event)).encode(), capture_output=True,
                                timeout=10)
        assert result.returncode == 0 and result.stdout == b''
    stored = json.dumps(SessionStatusStore(Guardrails(workspace)).inspect(), default=str)
    assert 'claude-code' in stored and 'PRIVATE' not in stored and 'NEVER' not in stored
