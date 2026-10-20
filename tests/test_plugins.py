import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from jarvis.core.guardrails import Guardrails
from jarvis.app.runtime import ToolContext
import server
import jarvis.app.plugins as tools
@pytest.fixture
def plugins(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    directory = tmp_path / 'plugins'
    directory.mkdir()
    monkeypatch.setattr(tools, 'PLUGIN_DIR', directory)
    before = set(sys.modules)
    yield directory, ToolContext.create(Guardrails(workspace))
    for name in set(sys.modules) - before:
        if name.startswith('jarvis.features.'):
            sys.modules.pop(name, None)


def write_plugin(directory, source, feature='custom'):
    folder = directory / feature / 'tools'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'plugin_{uuid4().hex}_tool.py'
    path.write_text(source, encoding='utf-8')
    return path


VALID = '''
def build(context):
    def plugin_probe(value: str = 'ok') -> dict:
        """Probe registration and shared call admission."""
        return {'value': value, 'calls': context.guard._calls}
    return plugin_probe
'''


def test_existing_tool_contract_unchanged(tmp_path):
    guard = Guardrails(tmp_path.resolve())
    actual = [tool.model_dump(mode='json') for tool in asyncio.run(server.create_server(guard).list_tools())]
    original = json.loads((Path(__file__).parent / 'fixtures/tool_contract.json').read_text(encoding='utf-8'))
    original_names = {tool['name'] for tool in original}
    existing = [tool for tool in actual if tool['name'] in original_names]
    assert sorted(existing, key=lambda x: x['name']) == sorted(original, key=lambda x: x['name'])
    assert len(actual) == 31
    assert 'read_public_web_page' in {tool['name'] for tool in actual}
    assert 'search_public_web' in {tool['name'] for tool in actual}
    assert {'analyze_program_static', 'read_registered_feed_with_agent_reach'} <= {tool['name'] for tool in actual}


def test_new_file_autoload_and_shared_budget(plugins):
    directory, context = plugins
    write_plugin(directory, VALID)
    write_plugin(directory, VALID.replace('plugin_probe', 'another_probe'))
    mcp = FastMCP('test')
    assert set(tools.load_tools(mcp, context)) == {'plugin_probe', 'another_probe'}
    async def exercise():
        for index in range(20):
            result = await mcp.call_tool('plugin_probe' if index % 2 else 'another_probe', {})
            assert json.loads(result[0].text)['calls'] == index + 1
        with pytest.raises(ToolError, match='budget'):
            await mcp.call_tool('plugin_probe', {})
    asyncio.run(exercise())


def test_stop_prevents_plugin_body(plugins):
    directory, context = plugins
    write_plugin(directory, VALID.replace("return {'value': value, 'calls': context.guard._calls}",
                                          "raise AssertionError('must not enter body')"))
    mcp = FastMCP('test')
    tools.load_tools(mcp, context)
    (context.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(ToolError, match='stopped'):
        asyncio.run(mcp.call_tool('plugin_probe', {}))


@pytest.mark.parametrize('source', [
    'not valid python !!!', 'VALUE = 1', 'def build(context, required): return None',
    'def build(context): return []',
    'def build(context):\n    async def async_tool(): return {}\n    return async_tool',
    "raise ImportError('dependency missing')",
])
def test_invalid_plugins_fail_before_registration(plugins, source):
    directory, context = plugins
    write_plugin(directory, VALID)
    write_plugin(directory, source)
    mcp = FastMCP('test')
    with pytest.raises(tools.PluginLoadError):
        tools.load_tools(mcp, context)
    assert asyncio.run(mcp.list_tools()) == []


def test_duplicate_names_rejected(plugins):
    directory, context = plugins
    write_plugin(directory, VALID)
    write_plugin(directory, VALID)
    with pytest.raises(tools.PluginLoadError, match='Duplicate'):
        tools.load_tools(FastMCP('test'), context)


def test_data_workspace_never_scanned(plugins, monkeypatch):
    directory, context = plugins
    nested = context.guard.workspace / 'tools'
    nested.mkdir()
    marker = context.guard.workspace / 'executed.txt'
    (nested / 'untrusted_tool.py').write_text("raise AssertionError('must not import')")
    write_plugin(directory, VALID)
    tools.load_tools(FastMCP('test'), context)
    monkeypatch.setattr(tools, 'PLUGIN_DIR', nested)
    with pytest.raises(tools.PluginLoadError, match='outside the data workspace'):
        tools.collect_tools(context)
    assert not marker.exists()


def test_hardlinked_plugin_rejected(plugins, tmp_path):
    import os
    directory, context = plugins
    source = tmp_path / 'outside.py'
    source.write_text(VALID)
    folder = directory / 'custom' / 'tools'
    folder.mkdir(parents=True)
    os.link(source, folder / 'linked_tool.py')
    with pytest.raises(tools.PluginLoadError, match='Invalid plugin'):
        tools.collect_tools(context)


def test_unexpected_plugin_failure_is_error(plugins):
    directory, context = plugins
    write_plugin(directory, VALID.replace("return {'value': value, 'calls': context.guard._calls}",
                                          "raise RuntimeError('private detail')"))
    mcp = FastMCP('test')
    tools.load_tools(mcp, context)
    with pytest.raises(ToolError) as captured:
        asyncio.run(mcp.call_tool('plugin_probe', {}))
    assert 'private detail' not in str(captured.value)


def test_categories_allow_same_filename_with_distinct_tools(plugins):
    directory, context = plugins
    for category, function in [('alpha', 'first_probe'), ('beta', 'second_probe')]:
        group = directory / category / 'tools'
        group.mkdir(parents=True)
        (group.parent / '__init__.py').write_text("raise AssertionError('feature initializers must not run')")
        (group / '__init__.py').write_text("raise AssertionError('category initializers must not run')")
        (group / 'shared_tool.py').write_text(VALID.replace('plugin_probe', function))
    assert set(tools.load_tools(FastMCP('test'), context)) == {'first_probe', 'second_probe'}


def test_duplicate_tool_names_across_categories_rejected(plugins):
    directory, context = plugins
    for category in ('alpha', 'beta'):
        write_plugin(directory, VALID, category)
    with pytest.raises(tools.PluginLoadError, match='Duplicate'):
        tools.collect_tools(context)


def test_linked_category_rejected_before_import(plugins, tmp_path):
    import os
    import subprocess
    directory, context = plugins
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'malicious_tool.py').write_text("raise AssertionError('must not import')")
    linked = directory / 'external'
    if os.name == 'nt':
        assert subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(outside)],
                              capture_output=True).returncode == 0
    else:
        linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(tools.PluginLoadError, match='Linked plugin category'):
        tools.collect_tools(context)


def test_linked_tools_subdirectory_rejected(plugins, tmp_path):
    import os
    import subprocess
    directory, context = plugins
    feature = directory / 'custom'
    feature.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'bad_tool.py').write_text("raise AssertionError('must not import')")
    linked = feature / 'tools'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(tools.PluginLoadError, match='Linked plugin directory'):
        tools.collect_tools(context)


def test_flat_legacy_tool_placement_is_not_silently_ignored(plugins):
    directory, context = plugins
    write_plugin(directory, VALID)
    (directory / 'stray_tool.py').write_text(VALID)
    with pytest.raises(tools.PluginLoadError, match='placement'):
        tools.collect_tools(context)
