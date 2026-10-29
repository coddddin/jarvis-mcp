"""Board 204bc125 parts 1+2: file-path arguments of pinned MCP tools stay inside approved folders, and results made
under a local pin never reach an external model."""
from pathlib import Path
import sys

import pytest

import jarvis.agents.brain as brain
from jarvis.app.chat import ChatService
from jarvis.core.guardrails import Guardrails
from jarvis.features.mcp_client.service import (McpError, McpSession, McpSettings, ServerConfig, ToolPin, discover,
                                                looks_like_path, path_suspects, valid_root)

PATHS = str(Path(__file__).parent / 'fixtures' / 'mcp_path_server.py')
HASH = 'a' * 64


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'projects' / 'demo').mkdir(parents=True)
    (tmp_path / 'projects' / 'demo' / 'app.exe').write_bytes(b'MZ')
    (tmp_path / 'documents').mkdir()
    (tmp_path / 'documents' / 'private.txt').write_text('secret', encoding='utf-8')
    return Guardrails(tmp_path)


@pytest.fixture
def settings(guard):
    return McpSettings(guard)


def approve(guard, settings, choices, roots=('projects',)):
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATHS])
    found = discover(guard, settings.load(), 'paths')
    assert found['status'] == {'state': 'ready'}
    settings.approve('paths', found['tools'], choices)
    if roots:
        settings.set_roots('paths', roots)
    return settings.load(), found


DEFAULT = {'open_binary': {'effect': 'read'}, 'look': {'effect': 'read', 'family': 'public_web'},
           'compare': {'effect': 'read'}, 'save_report': {'effect': 'confirm', 'path_args': [{'name': 'output_path',
                                                                                            'mode': 'write'}]}}


# --- detection --------------------------------------------------------------------------------------------------

def test_path_like_names_are_found_at_the_top_and_nested():
    schema = {'type': 'object', 'properties': {
        'path': {'type': 'string'}, 'input_file': {'type': 'string'}, 'outputDir': {'type': 'string'},
        'binary_path': {'type': 'string'}, 'query': {'type': 'string'}, 'url': {'type': 'string'},
        'element': {'type': 'string'}, 'profile': {'type': 'string'},
        'options': {'type': 'object', 'properties': {'file': {'type': 'string'}}},
        'items': {'type': 'array', 'items': {'$ref': '#/$defs/Item'}}},
        '$defs': {'Item': {'type': 'object', 'properties': {'folder': {'type': 'string'}}}}}
    top, nested = path_suspects(schema)
    assert top == ['binary_path', 'input_file', 'outputDir', 'path']
    assert nested == ['file', 'folder']


@pytest.mark.parametrize('value', ['C:\\Windows\\win.ini', 'c:/Users/x', '  D:\\x', '\\\\server\\share\\f',
                                   '\\\\?\\C:\\x', 'file:///C:/x', 'FILE:x', 'projects\\..\\..\\x', '..\\secret', '../x',
                                   ' ../../documents/private.txt'])
def test_path_looking_strings(value):
    assert looks_like_path(value)


@pytest.mark.parametrize('value', ['hello world', '오늘 서울 날씨', 'https://example.com/a/../b?q=C:\\x',
                                   'http://localhost:8080/', 'ratio 3:4', 'a/b/c', 'C: drive is full', 'see ../notes',
                                   'Click "Save as..." then C:\\ later', ''])
def test_ordinary_text_and_web_urls_are_not_paths(value):
    assert not looks_like_path(value)


@pytest.mark.parametrize('root, ok', [('projects', True), ('projects/demo', True), ('projects\\demo', True),
                                      ('', False), ('.', False), ('.jarvis-settings', False), ('projects/.git', False),
                                      ('..', False), ('projects/../x', False), ('C:\\x', False), ('\\\\srv\\s', False),
                                      (' projects', False), ('pro*', False), ('%TEMP%', False)])
def test_roots_are_plain_workspace_folders(root, ok):
    assert valid_root(root) is ok


# --- approval ---------------------------------------------------------------------------------------------------

def test_approval_auto_declares_path_arguments_and_keeps_them_local(guard, settings):
    config, found = approve(guard, settings, DEFAULT)
    suspects = {tool['name']: tool['path_suspects'] for tool in found['tools']}
    assert suspects['open_binary'] == {'top': ['path'], 'nested': []}
    assert suspects['configure'] == {'top': [], 'nested': ['file']}
    pins = {pin.name: pin for pin in config.servers[0].tools}
    assert [(a.name, a.mode) for a in pins['open_binary'].path_args] == [('path', 'read')]
    assert [(a.name, a.mode) for a in pins['compare'].path_args] == [('files', 'read')]
    assert [(a.name, a.mode) for a in pins['save_report'].path_args] == [('output_path', 'write')]
    assert pins['open_binary'].family == 'local' and pins['compare'].family == 'local'
    assert pins['look'].path_args == ()


def test_a_user_may_mark_a_path_tool_public_web_or_a_name_as_text(guard, settings):
    config, _ = approve(guard, settings, {'open_binary': {'effect': 'read', 'family': 'public_web',
                                                          'not_paths': ['path']}})
    pin = config.servers[0].tools[0]
    assert pin.family == 'public_web' and pin.path_args == () and pin.not_paths == ('path',)


@pytest.mark.parametrize('choice, message', [
    ({'configure': {'effect': 'read'}}, '거부로만'),                                    # nested path: deny only
    ({'save_report': {'effect': 'read', 'path_args': [{'name': 'output_path', 'mode': 'write'}]}}, 'confirm or deny'),
    ({'save_report': {'effect': 'auto', 'path_args': [{'name': 'output_path', 'mode': 'write'}]}}, 'confirm or deny'),
    ({'open_binary': {'effect': 'read', 'path_args': ['nope']}}, '없는 인자'),
    ({'open_binary': {'effect': 'read', 'not_paths': ['nope']}}, '없는 인자'),
    ({'open_binary': {'effect': 'read', 'path_args': ['path'], 'not_paths': ['path']}}, 'twice'),
    ({'open_binary': {'effect': 'read', 'blocked_args': ['path']}}, '막을 수 없습니다'),          # required argument
    ({'look': {'effect': 'read', 'blocked_args': ['nope']}}, '없는 인자'),
])
def test_approval_refusals(guard, settings, choice, message):
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATHS])
    found = discover(guard, settings.load(), 'paths')
    with pytest.raises(ValueError, match=message):
        settings.approve('paths', found['tools'], choice)
    assert settings.load().servers[0].state == 'pending'


def test_a_nested_path_tool_can_be_pinned_as_deny(guard, settings):
    config, _ = approve(guard, settings, {'configure': {'effect': 'deny'}})
    assert config.servers[0].tools[0].effect == 'deny'


def test_write_paths_need_confirm_in_the_pin_model_too():
    with pytest.raises(ValueError, match='confirm or deny'):
        ToolPin(name='t', schema_sha256=HASH, effect='read', path_args=({'name': 'out', 'mode': 'write'},))
    assert ToolPin(name='t', schema_sha256=HASH, effect='confirm', path_args=({'name': 'out', 'mode': 'write'},))


def test_roots_are_resolved_for_the_approval_view_and_bad_ones_refused(guard, settings):
    settings.add_server('paths', 'Paths', sys.executable, ['-I', PATHS])
    view = settings.set_roots('paths', ['projects/demo'])
    assert view == [{'root': 'projects/demo', 'path': str(guard.workspace / 'projects' / 'demo'), 'ok': True,
                     'reason': None}]
    assert discover(guard, settings.load(), 'paths')['roots'] == view
    for bad in (['.'], ['.jarvis-settings'], ['projects/../documents'], ['missing'], ['projects/demo/app.exe'],
                [str(guard.workspace)], ['projects', 'projects']):
        with pytest.raises(McpError):
            settings.set_roots('paths', bad)
    assert settings.load().servers[0].roots == ('projects/demo',)
    with pytest.raises(ValueError):
        ServerConfig(id='x', label='x', command=sys.executable, roots=('.jarvis-board',))


# --- calls --------------------------------------------------------------------------------------------------------

def test_a_path_inside_an_approved_folder_reaches_the_server_as_a_checked_absolute_path(guard, settings):
    config, _ = approve(guard, settings, DEFAULT)
    target = guard.workspace / 'projects' / 'demo' / 'app.exe'
    with McpSession(guard, config) as session:
        assert session.call('mcp:paths/open_binary', {'path': 'projects/demo/app.exe'})['text'] == f'open:{target}'
        assert session.call('mcp:paths/open_binary', {'path': str(target)})['text'] == f'open:{target}'
        answer = session.call('mcp:paths/compare', {'files': ['projects/demo/app.exe', str(target)]})
        assert answer['text'] == f'compare:{target};{target}'
        out = guard.workspace / 'projects' / 'demo' / 'report.md'
        answer = session.call('mcp:paths/save_report', {'output_path': 'projects/demo/report.md', 'text': '요약'},
                              confirmed=True)
        assert answer['text'] == f'saved:{out}|요약'
        sandbox = guard.workspace / '.jarvis-mcp' / 'paths' / 'note.md'
        answer = session.call('mcp:paths/save_report', {'output_path': '.jarvis-mcp/paths/note.md', 'text': 'x'},
                              confirmed=True)
        assert answer['text'] == f'saved:{sandbox}|x'


@pytest.mark.parametrize('path', ['documents/private.txt', 'C:\\Windows\\win.ini', '\\\\server\\share\\x',
                                  '\\\\?\\C:\\Windows\\win.ini', 'projects/../documents/private.txt',
                                  'file:///C:/Windows/win.ini', 'projects/demo/missing.exe', '.jarvis-settings/x',
                                  '.jarvis-mcp/other/x', 'projects/demo/app.exe:stream', '%USERPROFILE%\\x', 5, None])
def test_paths_outside_approved_folders_are_refused_before_anything_is_sent(guard, settings, path):
    config, _ = approve(guard, settings, DEFAULT)
    with McpSession(guard, config) as session:
        if path is None:
            assert session.call('mcp:paths/open_binary', {})['is_error']        # missing argument: server error
            return
        with pytest.raises(McpError):
            session.call('mcp:paths/open_binary', {'path': path})
        with pytest.raises(McpError):
            session.call('mcp:paths/compare', {'files': ['projects/demo/app.exe', path]})


def test_without_roots_only_the_sandbox_is_reachable(guard, settings):
    config, _ = approve(guard, settings, DEFAULT, roots=())
    with McpSession(guard, config) as session:
        with pytest.raises(McpError, match='승인된 폴더'):
            session.call('mcp:paths/open_binary', {'path': 'projects/demo/app.exe'})


def test_a_link_inside_a_root_is_refused(guard, settings, tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside')
    (outside / 'x.bin').write_bytes(b'x')
    link = guard.workspace / 'projects' / 'link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != 'win32':
            raise
        import _winapi                       # a junction needs no admin rights on Windows
        _winapi.CreateJunction(str(outside), str(link))
    assert link.is_symlink() or link.is_junction()
    config, _ = approve(guard, settings, DEFAULT)
    with McpSession(guard, config) as session:
        with pytest.raises(McpError):
            session.call('mcp:paths/open_binary', {'path': 'projects/link/x.bin'})


def test_undeclared_paths_are_refused_but_text_and_web_urls_pass(guard, settings):
    config, _ = approve(guard, settings, DEFAULT)
    with McpSession(guard, config) as session:
        answer = session.call('mcp:paths/look', {'query': '서울 날씨 3:4 a/b', 'url': 'https://example.com/x/../y'})
        assert answer['text'] == 'look:서울 날씨 3:4 a/b|https://example.com/x/../y'
        for bad in ({'query': 'C:\\Users\\me\\secret.txt'}, {'query': 'x', 'url': 'file:///C:/x'},
                    {'query': '\\\\host\\share'}, {'query': 'docs\\..\\..\\x'}):
            with pytest.raises(McpError, match='경로로 보이는'):
                session.call('mcp:paths/look', bad)
        with pytest.raises(McpError, match='경로로 보이는'):           # a path in the text of a write tool
            session.call('mcp:paths/save_report', {'output_path': 'projects/demo/r.md', 'text': 'C:\\x'},
                         confirmed=True)


def test_not_paths_fields_are_plain_text(guard, settings):
    config, _ = approve(guard, settings, {'look': {'effect': 'read', 'not_paths': ['query']}})
    with McpSession(guard, config) as session:
        assert session.call('mcp:paths/look', {'query': 'C:\\label'})['text'] == 'look:C:\\label|'
        with pytest.raises(McpError):
            session.call('mcp:paths/look', {'query': 'x', 'url': 'C:\\x'})


def test_an_external_brain_is_offered_only_public_web_tools(guard, settings):
    config, _ = approve(guard, settings, DEFAULT)
    with McpSession(guard, config) as session:
        assert {tool['name'] for tool in session.offered_for(external=True)} == {'mcp:paths/look'}
        assert len(session.offered_for(external=False)) == 4


# --- part 2: local results never reach an external model ------------------------------------------------------------

def test_a_result_made_under_a_local_pin_stays_local_after_re_pinning():
    brain.set_attached_families({'mcp:rea/open_binary': 'local', 'mcp:web/look': 'public_web'})
    try:
        local = {'name': 'mcp:rea/open_binary', 'family': 'local', 'detail': 'MZ header'}
        web = {'name': 'mcp:web/look', 'family': 'public_web', 'detail': 'page'}
        assert not brain.trace_external_allowed(local) and brain.trace_external_allowed(web)
        brain.set_attached_families({'mcp:rea/open_binary': 'public_web', 'mcp:web/look': 'public_web'})
        assert not brain.trace_external_allowed(local)                       # recorded as local: stays local
        assert brain.trace_external_allowed({'name': 'mcp:rea/open_binary', 'family': 'public_web'})
        brain.set_attached_families({'mcp:web/look': 'local'})
        assert not brain.trace_external_allowed(web)                         # now local by the user's pin
        assert not brain.trace_external_allowed({'name': 'mcp:gone/x', 'family': 'public_web'})   # unknown tool
        assert brain.trace_external_allowed({'name': 'search_public_web'})   # built-in traces carry no family
        assert not brain.trace_external_allowed({'name': 'search_public_web', 'family': 'local'})
        assert not brain.trace_external_allowed('search_public_web')
    finally:
        brain.set_attached_families({})


def test_chat_history_replaces_turns_with_local_results_for_an_external_brain():
    brain.set_attached_families({'mcp:rea/open_binary': 'public_web'})
    try:
        def turn(index, tools):
            return {'id': f'r{index}', 'mode': 'chat', 'status': 'completed', 'user': f'질문 {index}',
                    'assistant': f'답 {index}', 'tools': tools, 'model': {'label': 'm', 'external': False}}
        turns = [turn(1, [{'name': 'mcp:rea/open_binary', 'family': 'local', 'detail': 'MZ header'}]),
                 turn(2, [{'name': 'search_public_web', 'detail': 'page'}])]
        assert not ChatService._external_safe(turns[0]) and ChatService._external_safe(turns[1])
        sent = repr(ChatService._history(turns, 'now', external=True))
        assert '질문 1' not in sent and '답 1' not in sent and '질문 2' in sent and '답 2' in sent
        local = repr(ChatService._history(turns, 'now', external=False))
        assert '질문 1' in local                                              # the local model still sees it
    finally:
        brain.set_attached_families({})


def test_a_pin_from_before_path_args_is_changed_until_approved_again(guard, settings):
    config, _ = approve(guard, settings, {'open_binary': {'effect': 'read'}, 'look': {'effect': 'read'}})
    server = config.servers[0]
    old = tuple(pin.model_copy(update={'path_args': ()}) for pin in server.tools)     # what an M1-era file holds
    stale = config.model_copy(update={'servers': (server.model_copy(update={'tools': old}),)})
    with McpSession(guard, stale) as session:
        assert session.status == {'paths': {'state': 'changed', 'tools': ['open_binary']}}
        assert {tool['name'] for tool in session.offered()} == {'mcp:paths/look'}
        with pytest.raises(McpError, match='not attached'):
            session.call('mcp:paths/open_binary', {'path': '../../documents/private.txt'})


def test_blocked_arguments_may_not_be_set_by_a_model(guard, settings):
    config, _ = approve(guard, settings, {'look': {'effect': 'read', 'family': 'public_web', 'blocked_args': ['url']}})
    pin = config.servers[0].tools[0]
    assert pin.blocked_args == ('url',) and pin.path_args == ()
    with McpSession(guard, config) as session:
        assert session.call('mcp:paths/look', {'query': 'x'})['text'] == 'look:x|'
        with pytest.raises(McpError, match='url'):
            session.call('mcp:paths/look', {'query': 'x', 'url': 'https://example.com'})


def test_playwright_snapshot_filename_can_be_blocked_so_the_pin_stays_read():
    # @playwright/mcp 0.0.83 README: browser_snapshot(target?, filename?, depth?, boxes?); filename saves a file.
    schema = {'type': 'object', 'properties': {'target': {'type': 'string'}, 'filename': {'type': 'string'},
                                               'depth': {'type': 'number'}, 'boxes': {'type': 'boolean'}}}
    from jarvis.features.mcp_client.service import unchecked_paths
    plain = ToolPin(name='browser_snapshot', schema_sha256=HASH, effect='read')
    assert unchecked_paths(plain, schema) == ['filename']          # an M1-era pin: changed until approved again
    blocked = ToolPin(name='browser_snapshot', schema_sha256=HASH, effect='read', blocked_args=('filename',))
    assert unchecked_paths(blocked, schema) == []
