import asyncio
import json
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from jarvis.core.guardrails import GuardrailError, Guardrails
import server
from jarvis.features.system.tools import screenshot_tool, shell_tool


@pytest.fixture
def guard(tmp_path):
    workspace = tmp_path / 'JarvisWorkspace'
    workspace.mkdir()
    return Guardrails(workspace)


@pytest.mark.parametrize('command', [
    'rm -rf /', 'del file', 'powershell -Command whoami', 'cmd /c dir',
    'python -c print(1)', 'echo hi;whoami', 'pwd && whoami', 'echo hi > file',
    'echo $(whoami)', 'pwd\nwhoami', 'ls ..', 'ls C:/Windows',
    'ls //server/share', 'ls file:stream', 'ls --help', 'unknown', '',
    'echo "unterminated', 'x' * 1025,
])
def test_reject_commands(guard, command):
    with pytest.raises(GuardrailError):
        guard.validate_command(command)


@pytest.mark.parametrize('command', ['pwd', 'whoami', 'hostname', 'echo hello', 'ls', 'ls "."'])
def test_allow_commands(guard, command):
    assert guard.validate_command(command).operation == command.split()[0]


def test_paths(guard):
    assert guard.validate_path('.') == guard.workspace
    assert guard.validate_path('new.png', must_exist=False).parent == guard.workspace
    for value in ('../secret', str(guard.workspace.parent), '', 'missing', 'a\x00b', 'file:ads'):
        with pytest.raises(GuardrailError):
            guard.validate_path(value)


def test_link_escape(guard, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = guard.workspace / 'escape'
    if os.name == 'nt':
        # Windows junctions require no Developer Mode or symlink privilege.
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                              capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(GuardrailError):
        guard.validate_path('escape')
    with pytest.raises(GuardrailError):
        guard.validate_path('escape/new.png', must_exist=False)


def test_budget_and_stop(guard):
    for _ in range(20):
        guard.enter_call()
    with pytest.raises(GuardrailError, match='budget'):
        guard.enter_call()
    fresh = Guardrails(guard.workspace)
    (guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError, match='stopped'):
        fresh.enter_call()


@pytest.mark.parametrize('value', ['', 'relative', 'missing-absolute'])
def test_invalid_configuration(monkeypatch, tmp_path, value):
    if value == 'missing-absolute':
        value = str(tmp_path / 'missing')
    monkeypatch.setenv('JARVIS_WORKSPACE', value)
    with pytest.raises(GuardrailError):
        server.load_guardrails()


def test_screenshot_and_failure(guard, monkeypatch):
    mcp = server.create_server(guard)
    buffer = io.BytesIO()
    Image.new('RGB', (32, 24)).save(buffer, format='PNG')
    monkeypatch.setattr(screenshot_tool, 'capture_desktop', lambda: buffer.getvalue())
    asyncio.run(mcp.call_tool('take_screenshot', {}))
    files = list(guard.workspace.glob('screenshot-*.png'))
    assert len(files) == 1
    with Image.open(files[0]) as image:
        assert image.size == (32, 24)

    def fail():
        raise OSError('Desktop unavailable')
    monkeypatch.setattr(screenshot_tool, 'capture_desktop', fail)
    with pytest.raises(ToolError, match='Screenshot failed'):
        asyncio.run(mcp.call_tool('take_screenshot', {}))
    assert len(list(guard.workspace.glob('screenshot-*.png'))) == 1


def test_worker_timeout_and_failure(guard, monkeypatch):
    mcp = server.create_server(guard)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('worker', 5)
    monkeypatch.setattr(shell_tool.subprocess, 'run', timeout)
    with pytest.raises(ToolError, match='timeout'):
        asyncio.run(mcp.call_tool('run_safe_shell_command', {'command': 'pwd'}))
    with pytest.raises(ToolError, match='10 second timeout'):
        screenshot_tool.capture_desktop()

    monkeypatch.setattr(shell_tool.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess([], 1, '', 'private detail'))
    with pytest.raises(ToolError, match='exit code 1'):
        asyncio.run(mcp.call_tool('run_safe_shell_command', {'command': 'pwd'}))


def test_real_stdio_protocol(guard):
    documents = guard.workspace / 'documents'
    documents.mkdir()
    (documents / 'note.md').write_text('자비스 벡터 검색 MCP knowledge search', encoding='utf-8')
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(server.__file__).resolve())],
            env={**os.environ, 'JARVIS_WORKSPACE': str(guard.workspace),
                 'JARVIS_GOOGLE_TOKEN_FILE': str(guard.workspace / 'missing-token.json')},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    'run_safe_shell_command', 'get_system_status', 'take_screenshot',
                    'index_local_documents', 'search_local_documents', 'clear_local_index',
                    'get_google_connection_status', 'get_daily_briefing',
                    'create_project_snapshot', 'list_project_snapshots',
                    'preview_project_restore', 'restore_project_snapshot',
                    'preview_file_organization', 'apply_file_organization',
                    'list_file_organization_operations', 'preview_file_organization_undo',
                    'undo_file_organization', 'preview_downloads_organization',
                    'list_workflows', 'preview_workflow', 'start_workflow',
                    'get_workflow_status', 'list_workflow_runs', 'stop_workflow', 'analyze_error_log',
                    'list_web_sources', 'collect_web_information',
                    'analyze_program_static', 'read_registered_feed_with_agent_reach',
                    'search_public_web', 'read_public_web_page'}
                sources = await session.call_tool('list_web_sources', {})
                assert not sources.isError
                assert any(x['source_id'] == 'python-news' for x in json.loads(sources.content[0].text)['sources'])
                diagnostic = await session.call_tool('analyze_error_log', {
                    'error_log': 'password=private-test-value\nNameError: missing variable'})
                assert not diagnostic.isError
                report = json.loads(diagnostic.content[0].text)
                assert report['findings'][0]['kind'] == 'NameError'
                assert 'private-test-value' not in json.dumps(report)
                for command in ('pwd', 'echo hello', 'ls', 'whoami', 'hostname'):
                    response = await session.call_tool('run_safe_shell_command', {'command': command})
                    assert not response.isError, response
                stats = await session.call_tool('get_system_status', {})
                assert not stats.isError
                stats_data = json.loads(stats.content[0].text)
                assert 0 <= stats_data['cpu_percent'] <= 100
                for arguments in ({'command': 'rm -rf /'}, {'command': 'ls ..'}, {}, {'command': 123}):
                    rejected = await session.call_tool('run_safe_shell_command', arguments)
                    assert rejected.isError
                indexed = await session.call_tool('index_local_documents', {})
                assert not indexed.isError
                found = await session.call_tool('search_local_documents', {'query': '벡터 검색'})
                assert json.loads(found.content[0].text)['results'][0]['path'] == 'documents/note.md'
                status = await session.call_tool('get_google_connection_status', {})
                assert json.loads(status.content[0].text)['credentials_present'] is False
                assert (await session.call_tool('get_daily_briefing', {})).isError
                assert not (await session.call_tool('clear_local_index', {})).isError
    asyncio.run(exercise())
