from jarvis.core.paths import WORKERS_ROOT
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import time
import subprocess
import urllib.request

import pytest
from pydantic import ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from jarvis.core.guardrails import Guardrails
from jarvis.features.workflows.service import WorkflowLauncher, WorkflowError, Registry
import jarvis.features.workflows.service as workflow_launcher
import server


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_status_server_responds_with_idle_browser_connection():
    port = free_port()
    script = WORKERS_ROOT / 'workflow_status.py'
    child = subprocess.Popen([sys.executable, str(script), '--port', str(port)],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    idle = None
    try:
        deadline = time.monotonic() + 5
        while idle is None and time.monotonic() < deadline:
            try:
                idle = socket.create_connection(('127.0.0.1', port), timeout=.1)
            except OSError:
                time.sleep(.02)
        assert idle is not None, 'Status service did not start'
        # A browser can connect without sending a request yet.
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2) as response:
            assert response.status == 200
            assert json.load(response)['status'] == 'running'
    finally:
        if idle is not None:
            idle.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    config = tmp_path / 'config.json'
    port = free_port()
    config.write_text(json.dumps({'version': 1, 'programs': {'demo': '@status-server'},
        'allowed_hosts': ['127.0.0.1'], 'profiles': {'demo': {
            'description': 'Local test', 'max_runtime_seconds': 30,
            'actions': [{'kind': 'service', 'program': 'demo', 'args': ['--port', str(port)],
                         'cwd': '.', 'port': port}, {'kind': 'web', 'url': f'http://127.0.0.1:{port}/'}]}}}))
    instance = WorkflowLauncher(Guardrails(workspace))
    monkeypatch.setattr(instance, 'CONFIG', config)
    monkeypatch.setattr(workflow_launcher.webbrowser, 'open_new_tab', lambda url: True)
    yield instance
    instance.shutdown()


def edit_config(launcher, function):
    value = json.loads(launcher.CONFIG.read_text())
    function(value)
    launcher.CONFIG.write_text(json.dumps(value))


def test_preview_real_service_start_status_stop(launcher, monkeypatch):
    monkeypatch.setenv('JARVIS_TEST_SECRET', 'do-not-inherit')
    real_popen = workflow_launcher.subprocess.Popen
    recorded = []
    def spawn(*args, **kwargs):
        recorded.append(kwargs)
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(workflow_launcher.subprocess, 'Popen', spawn)
    assert launcher.profiles()['workflows'][0]['name'] == 'demo'
    preview = launcher.preview('demo')
    assert not launcher.history()['runs']
    result = launcher.start(preview['preview_id'])
    assert result['status'] == 'running'
    assert result['steps'][0]['status'] == 'tcp_ready'
    assert result['steps'][1]['status'] == 'dispatch_accepted_unverified'
    assert recorded[0]['shell'] is False
    assert 'JARVIS_TEST_SECRET' not in recorded[0]['env']
    stopped = launcher.stop(result['id'])
    assert stopped['status'] == 'stopped'
    assert stopped['processes'][0]['exit_code'] is not None
    assert len(launcher.history()['runs']) == 1


def test_duplicate_and_preview_single_use(launcher):
    first = launcher.preview('demo')
    run = launcher.start(first['preview_id'])
    with pytest.raises(WorkflowError, match='already used'):
        launcher.start(first['preview_id'])
    with pytest.raises(WorkflowError, match='already active'):
        launcher.start(launcher.preview('demo')['preview_id'])
    launcher.stop(run['id'])


def test_config_change_expired_and_disabled(launcher):
    preview = launcher.preview('demo')
    edit_config(launcher, lambda data: data['profiles']['demo'].update(description='changed'))
    with pytest.raises(WorkflowError, match='changed'):
        launcher.start(preview['preview_id'])
    preview = launcher.preview('demo')
    launcher.previews[preview['preview_id']]['expires'] = time.monotonic() - 1
    with pytest.raises(WorkflowError, match='expired'):
        launcher.start(preview['preview_id'])
    edit_config(launcher, lambda data: data['profiles']['demo'].update(enabled=False))
    with pytest.raises(WorkflowError, match='disabled'):
        launcher.preview('demo')
    with pytest.raises(WorkflowError, match='not registered'):
        launcher.preview('cmd /c anything')


def test_preoccupied_service_is_never_adopted(launcher):
    config = json.loads(launcher.CONFIG.read_text())
    port = config['profiles']['demo']['actions'][0]['port']
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', port))
        occupied.listen()
        with pytest.raises(WorkflowError, match='already occupied'):
            launcher.start(launcher.preview('demo')['preview_id'])
    assert not launcher.runs


def test_browser_failure_cleans_owned_service(launcher, monkeypatch):
    monkeypatch.setattr(workflow_launcher.webbrowser, 'open_new_tab', lambda url: False)
    result = launcher.start(launcher.preview('demo')['preview_id'])
    assert result['status'] == 'failed'
    assert result['errors']
    assert result['processes'][0]['exit_code'] is not None


def test_service_timeout_cleans_process(launcher):
    # Server deliberately binds a different free port; configured readiness must fail.
    edit_config(launcher, lambda data: data['profiles']['demo']['actions'][0].update(
        args=['--port', str(free_port())], startup_seconds=1))
    result = launcher.start(launcher.preview('demo')['preview_id'])
    assert result['status'] == 'failed'
    assert 'timeout' in result['errors'][0]
    assert result['processes'][0]['exit_code'] is not None


def test_browser_dispatch_timeout_cleans_service(launcher, monkeypatch):
    def slow_browser(url):
        time.sleep(0.1)
        return True
    monkeypatch.setattr(workflow_launcher.webbrowser, 'open_new_tab', slow_browser)
    monkeypatch.setattr(launcher, 'BROWSER_DISPATCH_TIMEOUT', 0.01)
    run = launcher.start(launcher.preview('demo')['preview_id'])
    assert run['status'] == 'failed'
    assert 'timed out' in run['errors'][0]
    assert run['processes'][0]['exit_code'] is not None


def test_short_lease_rejects_slow_startup(launcher):
    edit_config(launcher, lambda data: data['profiles']['demo'].update(max_runtime_seconds=1))
    edit_config(launcher, lambda data: data['profiles']['demo']['actions'][0].update(
        args=['--port', str(free_port())], startup_seconds=3))
    run = launcher.start(launcher.preview('demo')['preview_id'])
    assert run['status'] == 'failed'
    assert 'lease expired' in run['errors'][0]


def test_leases_and_stop_marker_cleanup(launcher):
    run = launcher.start(launcher.preview('demo')['preview_id'])
    launcher.runs[run['id']]['deadline'] = time.monotonic() - 1
    deadline = time.monotonic() + 4
    while launcher.status(run['id'])['status'] == 'running' and time.monotonic() < deadline:
        time.sleep(0.05)
    assert launcher.status(run['id'])['status'] == 'expired'
    run = launcher.start(launcher.preview('demo')['preview_id'])
    (launcher.guard.workspace / '.jarvis-stop').touch()
    deadline = time.monotonic() + 4
    while launcher.status(run['id'])['status'] == 'running' and time.monotonic() < deadline:
        time.sleep(0.05)
    assert launcher.status(run['id'])['status'] == 'interrupted'


def test_historical_pid_does_not_grant_stop_ownership(launcher):
    run = launcher.start(launcher.preview('demo')['preview_id'])
    other = WorkflowLauncher(launcher.guard)
    assert other.status(run['id'])['owned_by_this_server'] is False
    with pytest.raises(WorkflowError, match='does not own'):
        other.stop(run['id'])


def test_pid_identity_mismatch_prevents_stop(launcher, monkeypatch):
    run = launcher.start(launcher.preview('demo')['preview_id'])
    real_process = workflow_launcher.psutil.Process
    class WrongIdentity:
        def create_time(self):
            return -1
    with monkeypatch.context() as patch:
        patch.setattr(workflow_launcher.psutil, 'Process', lambda pid: WrongIdentity())
        stopped = launcher.stop(run['id'])
        assert stopped['status'] == 'stop_failed'
    assert real_process(run['processes'][0]['pid']).is_running()


@pytest.mark.parametrize('url', ['file:///private.txt', 'javascript:alert(1)',
                                 'https://evil.example/', 'http://user:password@127.0.0.1:8080/'])
def test_disallowed_urls_fail_preflight(launcher, url):
    edit_config(launcher, lambda data: data['profiles']['demo']['actions'][1].update(url=url))
    with pytest.raises(WorkflowError):
        launcher.preview('demo')
    assert not launcher.runs


def test_shell_executable_and_data_registry_rejected(launcher, tmp_path):
    fake = tmp_path / ('cmd.exe' if os.name == 'nt' else 'sh')
    fake.write_text('not executed')
    edit_config(launcher, lambda data: data['programs'].update(demo=str(fake)))
    with pytest.raises(WorkflowError, match='Shell'):
        launcher.preview('demo')
    config = launcher.guard.workspace / 'config.json'
    config.write_bytes(launcher.CONFIG.read_bytes())
    launcher.CONFIG = config
    with pytest.raises(WorkflowError, match='outside the data workspace'):
        launcher.profiles()


def test_strict_registry_model():
    with pytest.raises(ValidationError):
        Registry.model_validate({'version': 1, 'programs': {}, 'allowed_hosts': [],
            'profiles': {'demo': {'description': 'bad', 'max_runtime_seconds': '30',
                                   'actions': [{'kind': 'web', 'url': 'https://example.com'}]}}})


def test_new_workflow_tools_use_common_stop(launcher):
    mcp = server.create_server(launcher.guard)
    (launcher.guard.workspace / '.jarvis-stop').touch()
    for name, arguments in [('list_workflows', {}), ('preview_workflow', {'name': 'local-status'}),
                            ('start_workflow', {'preview_id': 'a' * 32}),
                            ('get_workflow_status', {'run_id': 'a' * 32}),
                            ('list_workflow_runs', {}), ('stop_workflow', {'run_id': 'a' * 32})]:
        with pytest.raises(ToolError, match='stopped'):
            asyncio.run(mcp.call_tool(name, arguments))


def test_real_mcp_profile_preview_without_launch(launcher):
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__).resolve())],
                                       env={**os.environ, 'JARVIS_WORKSPACE': str(launcher.guard.workspace)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                profiles = await session.call_tool('list_workflows', {})
                assert not profiles.isError
                assert any(item['name'] == 'local-status' for item in json.loads(profiles.content[0].text)['workflows'])
                preview = await session.call_tool('preview_workflow', {'name': 'local-status'})
                assert not preview.isError
                assert json.loads(preview.content[0].text)['actions'][0]['kind'] == 'service'
    asyncio.run(exercise())
