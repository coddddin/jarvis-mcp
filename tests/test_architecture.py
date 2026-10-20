import asyncio
import json
from pathlib import Path
import subprocess
import sys
import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jarvis.app.runtime import load_guardrails
from jarvis.core.paths import PROJECT_ROOT, DEFAULT_WORKSPACE, AUTH_ROOT, CONFIG_ROOT, WORKERS_ROOT
from jarvis.features.google.service import TOKEN_FILE
from scripts.check_architecture import check_architecture
import server


def test_all_pre_migration_tool_contracts_preserved():
    original = json.loads((PROJECT_ROOT / 'tests/fixtures/tool_contract_pre_layout.json').read_text(encoding='utf-8'))
    current = [t.model_dump(mode='json') for t in asyncio.run(server.create_server(load_guardrails()).list_tools())]
    names = {item['name'] for item in original}
    assert sorted((x for x in current if x['name'] in names), key=lambda x:x['name']) == original


def test_architecture_and_stable_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert not check_architecture(PROJECT_ROOT)
    assert DEFAULT_WORKSPACE == PROJECT_ROOT / 'JarvisWorkspace'
    assert AUTH_ROOT == PROJECT_ROOT / '.jarvis-auth'
    assert TOKEN_FILE == AUTH_ROOT / 'google-token.json'
    assert CONFIG_ROOT == PROJECT_ROOT / 'config'
    assert (WORKERS_ROOT / 'web_https.py').is_file()
    assert (WORKERS_ROOT / 'workflow_status.py').is_file()


def test_checker_detects_regressions(tmp_path):
    (tmp_path / 'stray.py').write_text('print(1)')
    core = tmp_path / 'jarvis/core'
    core.mkdir(parents=True)
    (core / 'bad.py').write_text('from jarvis.features.web.service import WebCollector\n')
    messages = '\n'.join(check_architecture(tmp_path))
    assert 'stray.py' in messages and 'reverse dependency' in messages


@pytest.mark.parametrize('source', ['from ..features.web import service\n', 'from jarvis import app\n',
                                  'from ..agents import store\n'])
def test_checker_catches_relative_and_package_reverse_imports(tmp_path, source):
    core = tmp_path / 'jarvis/core'
    core.mkdir(parents=True)
    (core / 'bad.py').write_text(source)
    assert any('reverse dependency' in error for error in check_architecture(tmp_path))


def test_agent_control_cannot_import_execution_adapters(tmp_path):
    agents = tmp_path / 'jarvis/agents'
    agents.mkdir(parents=True)
    (agents / 'bad.py').write_text('from jarvis.app import runtime\n')
    assert any('Agent control must depend only' in error for error in check_architecture(tmp_path))


def test_stable_entry_from_unrelated_cwd_and_package_entry(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    async def exercise(args, cwd):
        params = StdioServerParameters(command=sys.executable, args=args, cwd=str(cwd),
            env={'JARVIS_WORKSPACE': str(workspace)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 38
                assert not (await session.call_tool('get_system_status', {})).isError
    asyncio.run(exercise([str(PROJECT_ROOT / 'server.py')], tmp_path))
    asyncio.run(exercise(['-m', 'jarvis'], PROJECT_ROOT))


def test_oauth_entry_help_does_not_authenticate(tmp_path):
    result = subprocess.run([sys.executable, str(PROJECT_ROOT / 'google_auth_setup.py'), '--help'],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and '--client-secrets' in result.stdout
