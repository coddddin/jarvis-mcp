"""MCP client core (M1): HUD-only config with approval, pinned tools, an isolated per-task session, families."""
from pathlib import Path
import sys

import psutil
import pytest
from pydantic import ValidationError

import jarvis.agents.brain as brain
from jarvis.core.guardrails import Guardrails
from jarvis.features.mcp_client.service import (ConfirmRequired, McpConfig, McpError, McpSession, McpSettings,
                                                ServerConfig, ToolPin, discover, families, schema_hash)

FAKE = str(Path(__file__).parent / 'fixtures' / 'mcp_fake_server.py')


@pytest.fixture
def guard(tmp_path):
    return Guardrails(tmp_path)


@pytest.fixture
def settings(guard):
    return McpSettings(guard)


def add_fake(settings, env_names=('FAKE_VARIANT',)):
    return settings.add_server('fake', 'Fake', sys.executable, ['-I', FAKE], env_names)


def approved(guard, settings, choices=None):
    add_fake(settings)
    found = discover(guard, settings.load(), 'fake')
    assert found['status'] == {'state': 'ready'}
    choices = choices or {'echo': {'family': 'public_web', 'effect': 'read'},
                          'slow': {'family': 'local', 'effect': 'read', 'timeout_s': 1},
                          'big': {'family': 'local', 'effect': 'read', 'max_chars': 100},
                          'boom': {'family': 'local', 'effect': 'confirm'}}
    settings.approve('fake', found['tools'], choices)
    return settings.load()


# --- config ---

def test_a_new_server_is_pending_with_no_tools_and_offers_nothing(guard, settings):
    add_fake(settings)
    config = settings.load()
    assert config.servers[0].state == 'pending' and config.servers[0].tools == ()
    with McpSession(guard, config) as session:                  # only approved servers start
        assert session.status == {} and session.offered() == []


def test_discover_lists_tools_with_hashes_and_saves_nothing(guard, settings):
    add_fake(settings)
    before = (guard.workspace / McpSettings.PATH).read_bytes()
    found = discover(guard, settings.load(), 'fake')
    names = {tool['name'] for tool in found['tools']}
    assert {'echo', 'slow', 'big', 'boom', 'browser_evaluate'} <= names
    assert all(tool['schema_sha256'] == schema_hash(tool) for tool in found['tools'])
    assert (guard.workspace / McpSettings.PATH).read_bytes() == before


@pytest.mark.parametrize('kwargs, message', [
    ({'command': 'python.exe'}, 'absolute'),
    ({'args': ('--allow-private-ips',)}, 'forbidden'),
    ({'args': ('C:/x/node_modules/@playwright/mcp/cli.js', '--headless', '--isolated')}, '--no-webmcp'),
    ({'env_names': ('API_KEY=secret',)}, 'names only'),
])
def test_server_config_refuses_unsafe_settings(kwargs, message):
    base = {'id': 'x', 'label': 'X', 'command': sys.executable}
    with pytest.raises(ValidationError, match=message):
        ServerConfig(**{**base, **kwargs})


def test_dangerous_tools_are_always_denied():
    with pytest.raises(ValidationError, match='always denied'):
        ToolPin(name='browser_evaluate', effect='read', schema_sha256='0' * 64)


def test_approval_pins_chosen_tools_and_forces_deny(guard, settings):
    add_fake(settings)
    found = discover(guard, settings.load(), 'fake')
    server = settings.approve('fake', found['tools'], {'echo': {'family': 'public_web', 'effect': 'read'},
                                                      'browser_evaluate': {'family': 'local', 'effect': 'read'}})
    pins = {pin.name: pin for pin in server.tools}
    assert server.state == 'approved' and set(pins) == {'echo', 'browser_evaluate'}
    assert pins['browser_evaluate'].effect == 'deny'
    assert families(settings.load()) == {'mcp:fake/echo': 'public_web'}
    with pytest.raises(McpError, match='did not report'):
        settings.approve('fake', found['tools'], {'missing': {'effect': 'read'}})


def test_linked_or_invalid_settings_read_as_no_servers(guard, settings):
    path = guard.workspace / McpSettings.PATH
    path.parent.mkdir()
    path.write_text('{"version": 1, "servers": [{"id": "BAD"}]}', encoding='utf-8')
    assert settings.load() == McpConfig()


# --- session ---

def test_a_session_offers_only_matching_pins_and_calls_them(guard, settings):
    config = approved(guard, settings)
    with McpSession(guard, config) as session:
        assert session.status == {'fake': {'state': 'ready'}}
        offered = {tool['name']: tool for tool in session.offered()}
        assert set(offered) == {'mcp:fake/echo', 'mcp:fake/slow', 'mcp:fake/big', 'mcp:fake/boom'}
        assert offered['mcp:fake/echo']['description'] == 'Echo the text back.'
        answer = session.call('mcp:fake/echo', {'text': '안녕 » é'})             # UTF-8 both ways
        assert answer['text'] == 'echo:안녕 » é' and not answer['is_error']
        trace = session.trace('mcp:fake/echo', answer, 'e1')
        assert trace['family'] == 'public_web' and trace['server'] == 'fake' and trace['untrusted_content']
        big = session.call('mcp:fake/big', {'n': 500})
        assert len(big['text']) == 100 and big['truncated']
        with pytest.raises(McpError, match='not attached'):
            session.call('mcp:fake/browser_evaluate', {'code': '1'})
        with pytest.raises(ConfirmRequired):
            session.call('mcp:fake/boom', {})
        failed = session.call('mcp:fake/boom', {}, confirmed=True)
        assert failed['is_error'] and session.trace('mcp:fake/boom', failed, 'e2')['status'] == 'error'


def test_a_changed_tool_is_not_offered_until_reapproved(guard, settings, monkeypatch):
    config = approved(guard, settings)
    monkeypatch.setenv('FAKE_VARIANT', 'changed')               # the server now describes echo differently
    with McpSession(guard, config) as session:
        assert session.status == {'fake': {'state': 'changed', 'tools': ['echo']}}
        assert 'mcp:fake/echo' not in {tool['name'] for tool in session.offered()}
        assert 'mcp:fake/slow' in {tool['name'] for tool in session.offered()}
    assert settings.load() == config                            # the session never writes the settings


def test_a_slow_tool_kills_the_session_and_its_server(guard, settings):
    config = approved(guard, settings)
    session = McpSession(guard, config)
    session.start()
    worker = psutil.Process(session.process.pid)
    servers = worker.children(recursive=True)
    assert servers
    with pytest.raises(McpError, match='in time'):
        session.call('mcp:fake/slow', {'seconds': 30})          # pin timeout 1 s + grace
    assert session.process is None
    _, alive = psutil.wait_procs([worker, *servers], timeout=5)
    assert not alive


def test_the_session_time_limit_stops_further_calls(guard, settings):
    config = approved(guard, settings)
    now = [0.0]
    session = McpSession(guard, config, clock=lambda: now[0], session_seconds=10)
    session.start()
    now[0] = 11
    with pytest.raises(McpError, match='time limit'):
        session.call('mcp:fake/echo', {'text': 'x'})
    session.close()


def test_a_missing_program_is_reported_without_starting_anything(guard, settings, tmp_path):
    settings.add_server('gone', 'Gone', str(tmp_path / 'nope.exe'))
    config = settings.load()
    config = McpConfig(servers=(config.servers[0].model_copy(update={'state': 'approved'}),))
    with McpSession(guard, config) as session:
        assert session.status['gone']['state'] == 'failed' and session.process is None


def test_only_listed_environment_names_reach_the_server(guard, settings, monkeypatch):
    monkeypatch.setenv('FAKE_VARIANT', 'changed')
    monkeypatch.setenv('JARVIS_TEST_SECRET', 'do-not-pass')
    settings.add_server('fake', 'Fake', sys.executable, ['-I', FAKE], ())          # FAKE_VARIANT not listed
    found = discover(guard, settings.load(), 'fake')
    echo = next(tool for tool in found['tools'] if tool['name'] == 'echo')
    assert 'evil' not in echo['description']


# --- brain families ---

def test_attached_families_decide_what_may_reach_an_external_model():
    try:
        brain.set_attached_families({'mcp:fake/echo': 'public_web', 'mcp:fake/files': 'local',
                                     'search_public_web': 'local', 'mcp:x/y': 'approved_notes'})
        assert brain.external_allowed('mcp:fake/echo')
        assert not brain.external_allowed('mcp:fake/files')
        assert not brain.external_allowed('mcp:x/y')                # only public_web or local can be attached
        assert brain.family_of('search_public_web') == 'public_web'   # built-in tools cannot be overridden
        assert not brain.external_allowed('mcp:unknown/tool')
        assert 'mcp:fake/echo' not in brain.EXTERNAL_TOOLS          # the jarvis MCP allowlist is unchanged
    finally:
        brain.set_attached_families({})
