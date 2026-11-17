"""Board 204bc125 part 4: fixed non-secret server settings, pins-only task listings and the REA preset."""
import json
from pathlib import Path
import re
import sys

import pytest
from pydantic import ValidationError

from jarvis.core.guardrails import Guardrails
from jarvis.features.mcp_client.service import McpError, McpSession, McpSettings, ServerConfig, discover
from jarvis.features.mcp_presets import service as presets
from jarvis.features.mcp_presets.service import REA_PINS, REA_VERSION, PresetError, setup_rea

FIXTURES = Path(__file__).parent / 'fixtures'
ENV_SERVER = str(FIXTURES / 'mcp_env_server.py')
PATH_SERVER = str(FIXTURES / 'mcp_path_server.py')

# The REA tools denied to Claude Code in the user's settings (2026-10-29): none may ever be pinned here.
CLAUDE_DENIED = '''annotate_native_function capture_adb_screen capture_browser_scenario capture_electron_scenario
capture_native_ui_scenario capture_process_scenario close_gdb_session close_rizin_debug_session collect_adb_bugreport
cutter_command execute_rizin_command gdb_console inspect_adb_client inspect_adb_device inspect_adb_display
inspect_adb_package inspect_adb_window install_adb_package instrument_with_frida list_adb_devices list_adb_directory
list_adb_features list_adb_packages list_adb_processes list_adb_services load_frida_script observe_javascript_runtime
observe_native_calls observe_native_ui observe_web_execution observe_web_session pull_adb_file pull_adb_package
push_adb_file read_adb_logcat read_adb_setting resolve_adb_packages resume_frida_session rizin_debug_command
start_adb_activity start_adb_app start_frida_session start_gdb_session start_rizin_debug_session stop_adb_app
uninstall_adb_package'''.split()
# Anything that writes, debugs, runs a program or command, captures, or drives a browser or device.
RISKY = re.compile(r'^(?:set_|unset_|annotate|record_|update_|export_|import_|extract_|recover_|execute_|start_|stop_|'
                   r'install|uninstall|push_|pull_|capture_|observe_|instrument|load_|resume_|unload_|cutter|gdb|rizin|'
                   r'close_gdb|close_rizin|close_frida)|adb|frida|electron|browser|web_|webmcp|javascript_runtime|'
                   r'objdump|_process_|cdp')


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'projects').mkdir()
    return Guardrails(tmp_path)


@pytest.fixture
def outside(tmp_path_factory):
    folder = tmp_path_factory.mktemp('program')
    return folder


# --- fixed settings ---------------------------------------------------------------------------------------------

def config(**extra):
    return {'id': 'env', 'label': 'Env', 'command': sys.executable, 'args': ['-I', ENV_SERVER], **extra}


@pytest.mark.parametrize('fixed', [
    [{'name': 'NODE_OPTIONS', 'value': '--require x'}], [{'name': 'PATH', 'value': 'C:\\x'}],
    [{'name': 'JAVA_HOME', 'value': 'relative\\jdk'}], [{'name': 'JAVA_HOME', 'value': '\\\\server\\jdk'}],
    [{'name': 'JAVA_HOME', 'value': 'C:\\a\\..\\b'}], [{'name': 'JAVA_HOME', 'value': 'C:\\%JDK%'}],
    [{'name': 'JAVA_HOME', 'value': 'C:\\jdk:stream'}], [{'name': 'GHIDRA_HEADLESS_MAXMEM', 'value': '100G'}],
    [{'name': 'GHIDRA_HEADLESS_MAXMEM', 'value': '1G; x'}],
    [{'name': 'JAVA_HOME', 'value': 'C:\\a'}, {'name': 'JAVA_HOME', 'value': 'C:\\b'}]])
def test_only_allowlisted_settings_with_plain_values_are_accepted(fixed):
    with pytest.raises(ValidationError):
        ServerConfig.model_validate(config(fixed_env=fixed))


def test_a_fixed_setting_may_not_also_be_a_passed_name():
    with pytest.raises(ValidationError):
        ServerConfig.model_validate(config(env_names=['JAVA_HOME'], fixed_env=[{'name': 'JAVA_HOME', 'value': 'C:\\j'}]))
    assert ServerConfig.model_validate(config(fixed_env=[{'name': 'GHIDRA_HEADLESS_MAXMEM', 'value': '2G'}]))


def add_env_server(guard, fixed):
    settings = McpSettings(guard)
    settings.add_server('env', 'Env', sys.executable, ['-I', ENV_SERVER], fixed_env=fixed)
    found = discover(guard, settings.load(), 'env')
    assert found['status'] == {'state': 'ready'}
    settings.approve('env', found['tools'], {'setting': {'effect': 'read', 'not_paths': []}})
    return settings.load()


def test_fixed_settings_reach_the_server_but_never_a_model(guard, outside):
    loaded = add_env_server(guard, {'JAVA_HOME': str(outside), 'GHIDRA_HEADLESS_MAXMEM': '1G'})
    with McpSession(guard, loaded) as session:
        assert session.call('mcp:env/setting', {'name': 'GHIDRA_HEADLESS_MAXMEM'})['text'] == 'GHIDRA_HEADLESS_MAXMEM=1G'
        answer = session.call('mcp:env/setting', {'name': 'JAVA_HOME'})
        assert answer['text'] == 'JAVA_HOME=' + str(outside)
        assert session.call('mcp:env/setting', {'name': 'NODE_OPTIONS'})['text'] == 'NODE_OPTIONS=<unset>'
        assert str(outside) not in json.dumps(session.offered()) and str(outside) not in json.dumps(session.status)


@pytest.mark.parametrize('where', ['missing', 'inside', 'parent', 'link'])
def test_a_folder_that_is_gone_inside_the_workspace_or_linked_fails_the_server_without_naming_it(guard, outside, where):
    folder = outside
    loaded = add_env_server(guard, {'JAVA_HOME': str(outside)})
    if where == 'missing':
        folder = outside / 'gone'
    elif where == 'inside':
        folder = guard.workspace / 'projects'
    elif where == 'parent':
        folder = guard.workspace.parent
    else:
        folder = outside.parent / (outside.name + '-link')
        try:
            folder.symlink_to(outside, target_is_directory=True)
        except OSError:
            if sys.platform != 'win32':
                raise
            import _winapi                       # a junction needs no admin rights on Windows
            _winapi.CreateJunction(str(outside), str(folder))
    server = loaded.servers[0].model_copy(update={'fixed_env': (
        loaded.servers[0].fixed_env[0].model_copy(update={'value': str(folder)}),)})
    with McpSession(guard, loaded.model_copy(update={'servers': (server,)})) as session:
        status = session.status['env']
        assert status['state'] == 'failed' and str(folder) not in status['reason'] and session.offered() == []


# --- pins-only listing ------------------------------------------------------------------------------------------

def test_a_task_session_lists_only_pinned_tools_while_discover_lists_all(guard):
    settings = McpSettings(guard)
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATH_SERVER])
    found = discover(guard, settings.load(), 'paths')
    assert {tool['name'] for tool in found['tools']} == {'open_binary', 'save_report', 'look', 'compare', 'configure'}
    settings.approve('paths', found['tools'], {'look': {'effect': 'read'}})
    with McpSession(guard, settings.load()) as session:
        assert [tool['name'] for tool in session.discovered['paths']] == ['look']
        assert session.status['paths'] == {'state': 'ready'}


def test_a_pin_the_server_stopped_reporting_is_still_changed_under_the_pins_only_listing(guard):
    settings = McpSettings(guard)
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATH_SERVER])
    found = discover(guard, settings.load(), 'paths')
    renamed = [{**tool, 'name': 'gone'} if tool['name'] == 'look' else tool for tool in found['tools']]
    settings.approve('paths', renamed, {'gone': {'effect': 'read'}})
    with McpSession(guard, settings.load()) as session:
        assert session.status['paths'] == {'state': 'changed', 'tools': ['gone']} and session.offered() == []


def test_a_blocked_argument_is_not_shown_to_a_model(guard):
    settings = McpSettings(guard)
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATH_SERVER])
    found = discover(guard, settings.load(), 'paths')
    settings.approve('paths', found['tools'], {'look': {'effect': 'read', 'blocked_args': ['url']}})
    with McpSession(guard, settings.load()) as session:
        assert set(session.offered()[0]['input_schema']['properties']) == {'query'}
        with pytest.raises(McpError, match='url'):
            session.call('mcp:paths/look', {'query': 'x', 'url': 'https://example.com'})


def test_the_worker_caps_fit_a_full_rea_listing():
    from jarvis.workers import mcp_session
    assert mcp_session.MAX_TOOLS >= 256 and mcp_session.MAX_REPLY <= 4_000_000


# --- the REA preset ---------------------------------------------------------------------------------------------

def test_rea_pins_are_static_reads_and_never_a_denied_or_risky_tool():
    assert not set(REA_PINS) & set(CLAUDE_DENIED)
    assert [name for name in REA_PINS if RISKY.search(name)] == []
    assert all(pin['family'] == 'local' and pin['effect'] == 'read' for pin in REA_PINS.values())
    assert {'snapshot_path', 'format', 'entry_point'} <= set(REA_PINS['open_binary']['blocked_args'])
    assert {'snapshot_path', 'overwrite'} <= set(REA_PINS['close_binary']['blocked_args'])


class Hub:
    def __init__(self, paths, fail=False):
        self.paths, self.fail = paths, fail

    def program_paths(self, names):
        if self.fail:
            raise presets.IntegrationError('Integration directory unavailable.')
        return {name: self.paths[name] for name in names}


def installed(outside, version=REA_VERSION):
    package = outside / 'node_modules' / 'rea-agents'
    (package / 'scripts').mkdir(parents=True)
    (package / 'scripts' / 'rea.mjs').write_text('', encoding='utf-8')
    (package / 'package.json').write_text(json.dumps({'name': 'rea-agents', 'version': version}), encoding='utf-8')
    (outside / 'ghidra').mkdir()
    (outside / 'jdk').mkdir()
    return Hub({'node': Path(sys.executable), 'rea_entry': package / 'scripts' / 'rea.mjs',
                'ghidra': outside / 'ghidra', 'java_home': outside / 'jdk'})


def fake_tools(extra=()):
    tools = []
    for name, pin in REA_PINS.items():
        names = [item['name'] for item in pin.get('path_args', ())] + list(pin.get('blocked_args', ())) \
            + list(pin.get('not_paths', ()))
        required = [item['name'] for item in pin.get('path_args', ())][:1]
        tools.append({'name': name, 'description': name, 'input_schema': {
            'type': 'object', 'properties': {arg: {'type': 'string'} for arg in names}, 'required': required}})
    return tools + [{'name': item, 'description': item, 'input_schema': {'type': 'object'}} for item in extra]


def fake_discover(tools, state='ready'):
    return lambda guard, config, server_id: {'status': {'state': state}, 'tools': tools, 'roots': []}


def test_setup_approves_exactly_the_preset_with_its_launch_and_roots(guard, outside):
    seen = []
    server = setup_rea(guard, hub=installed(outside), confirm=lambda found: seen.append(len(found['tools'])) or True,
                       discover_tools=fake_discover(fake_tools(extra=CLAUDE_DENIED)))
    assert seen == [len(REA_PINS) + len(CLAUDE_DENIED)]
    assert {pin.name for pin in server.tools} == set(REA_PINS) and server.roots == ('projects',)
    assert server.args[-1] == '--mcp' and server.args[0].endswith('rea.mjs') and server.label == 'REA'
    assert {item.name: item.value for item in server.fixed_env} == {
        'GHIDRA_INSTALL_DIR': str(outside / 'ghidra'), 'JAVA_HOME': str(outside / 'jdk'), 'GHIDRA_HEADLESS_MAXMEM': '1G'}
    pins = {pin.name: pin for pin in server.tools}
    assert [item.name for item in pins['open_binary'].path_args] == ['path'] and pins['open_binary'].timeout_s == 120
    assert all(pin.family == 'local' and pin.effect == 'read' for pin in pins.values())


@pytest.mark.parametrize('problem, match', [
    ('missing', '알리지 않았습니다: open_binary'), ('refused', '승인하지 않아'), ('failed', '시작하지 못했습니다')])
def test_a_failed_setup_leaves_no_server_entry(guard, outside, problem, match):
    tools = [tool for tool in fake_tools() if tool['name'] != 'open_binary'] if problem == 'missing' else fake_tools()
    with pytest.raises(PresetError, match=match):
        setup_rea(guard, hub=installed(outside), confirm=lambda found: problem != 'refused',
                  discover_tools=fake_discover(tools, 'failed' if problem == 'failed' else 'ready'))
    assert McpSettings(guard).load().servers == ()


def test_setup_needs_the_exact_rea_version_and_checked_paths(guard, outside):
    with pytest.raises(PresetError, match='6.7.0'):
        setup_rea(guard, hub=installed(outside, version='6.8.0'), discover_tools=fake_discover(fake_tools()))
    with pytest.raises(PresetError, match='integrations.json'):
        setup_rea(guard, hub=Hub({}, fail=True), discover_tools=fake_discover(fake_tools()))
    assert McpSettings(guard).load().servers == ()


# --- result shaping and the eval scorer -------------------------------------------------------------------------

def test_the_worker_keeps_only_the_named_keys_of_a_json_object_before_cutting():
    from jarvis.workers.mcp_session import kept
    wrapped = json.dumps({'evidence_id': 'e', 'provenance': 'x' * 5000, 'normalized_result': [1, 2]})
    assert json.loads(kept(wrapped, ['result', 'normalized_result'])) == {'normalized_result': [1, 2]}
    for text in (wrapped, '[1]', 'plain', '{broken'):
        assert kept(text, ()) == text
    assert kept('{"other": 1}', ['result']) == '{"other": 1}' and kept('[1]', ['result']) == '[1]'


def test_eval_scoring_ignores_the_tool_line_and_needs_an_ok_open():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'tool_eval'))
    from score import score
    task = {'answer_all': ['advapi32'], 'tools_ok': ['open_binary']}
    ok = [{'name': 'mcp:rea/open_binary', 'status': 'ok'}]
    assert score(task, 'ADVAPI32.DLL입니다.\n\n사용한 도구: mcp:rea/open_binary', ok, REA_PINS)['pass']
    assert not score(task, '모릅니다.\n\n사용한 도구: advapi32', ok, REA_PINS)['pass']
    assert not score(task, 'ADVAPI32', [{'name': 'mcp:rea/open_binary', 'status': 'error'}], REA_PINS)['pass']
    assert not score(task, 'ADVAPI32', ok + [{'name': 'mcp:rea/set_comment', 'status': 'ok'}], REA_PINS)['pass']
