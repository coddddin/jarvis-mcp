import json
import time

import pytest
from pydantic import ValidationError

from jarvis.agents.models import TaskRequest
from jarvis.agents.sandbox import DockerRuntimeConfig, SandboxRunner, SandboxError
from jarvis.core.guardrails import Guardrails, GuardrailError


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    executable = tmp_path / 'docker.exe'
    executable.touch()
    runner = SandboxRunner(Guardrails(workspace), DockerRuntimeConfig(executable=str(executable), image_id='sha256:' + 'a'*64))
    monkeypatch.setattr(runner, '_trusted', lambda args, **kwargs:
        'linux' if args[0] == 'info' else runner.config.image_id if args[0] == 'image' else '')
    return runner


def cases(expected=5):
    return [{'function': 'add', 'args': [2,3], 'expected': expected}]


def test_fixed_command_capabilities_no_mounts_and_host_computes_verdict(sandbox, monkeypatch):
    captured = []
    def exchange(arguments, payload, deadline):
        captured.append((arguments, json.loads(payload)))
        return b'{"values":[5]}'
    monkeypatch.setattr(sandbox, '_exchange', exchange)
    evidence = sandbox.check('def add(a,b): return a+b', cases(), time.time()+60)
    assert evidence.passed and evidence.checker == 'python_container_cases_v1'
    arguments, payload = captured[0]
    for expected in ['--network=none', '--read-only', '--cap-drop=ALL', '--memory=128m',
                     '--cpus=0.5', '--pids-limit=32', '--user=65534:65534', '--pull=never', '--log-driver=none']:
        assert expected in arguments
    assert not any(arg in {'-v', '--volume', '--mount', '--privileged', '--gpus'} for arg in arguments)
    assert payload['calls'] == [{'function':'add', 'args':[2,3]}]
    assert 'expected' not in json.dumps(payload)
    failed = sandbox.check('def add(a,b): return a+b', cases(6), time.time()+60)
    assert not failed.passed


@pytest.mark.parametrize('raw', [b'{"passed":true}', b'{"values":[true]}', b'{"values":["5"]}', b'{"candidate_failed":true}'])
def test_candidate_cannot_supply_qa_verdict(sandbox, monkeypatch, raw):
    monkeypatch.setattr(sandbox, '_exchange', lambda *args: raw)
    assert not sandbox.check('def add(a,b): return a+b', cases(), time.time()+60).passed


def test_bad_output_fails_closed(sandbox, monkeypatch):
    monkeypatch.setattr(sandbox, '_exchange', lambda *args: b'noise\n{"values":[5]}')
    with pytest.raises(SandboxError, match='unexpected'):
        sandbox.check('def add(a,b): return a+b', cases(), time.time()+60)


def test_remote_docker_environment_is_removed(sandbox, monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'tcp://remote.invalid:2375')
    monkeypatch.setenv('DOCKER_CONTEXT', 'remote')
    env = sandbox._environment()
    assert 'DOCKER_HOST' not in env and 'DOCKER_CONTEXT' not in env
    assert sandbox._command(['info'])[1:3] == ['--host', sandbox.endpoint]


def test_windows_or_missing_pinned_image_is_refused(sandbox, monkeypatch):
    monkeypatch.setattr(sandbox, '_trusted', lambda args, **kwargs: 'windows')
    with pytest.raises(SandboxError, match='Linux'):
        sandbox.check('def add(a,b): return a+b', cases(), time.time()+60)
    monkeypatch.setattr(sandbox, '_trusted', lambda args, **kwargs: 'linux' if args[0]=='info' else 'sha256:'+'b'*64)
    with pytest.raises(SandboxError, match='Pinned'):
        sandbox.check('def add(a,b): return a+b', cases(), time.time()+60)


def test_cleanup_cannot_be_reported_success_when_unverified(sandbox, monkeypatch):
    def command(args, **kwargs):
        if args[0] == 'info':
            return 'linux'
        if args[0] == 'image':
            return sandbox.config.image_id
        raise SandboxError('Daemon unavailable')
    monkeypatch.setattr(sandbox, '_trusted', command)
    monkeypatch.setattr(sandbox, '_exchange', lambda *args: b'{"values":[5]}')
    with pytest.raises(SandboxError, match='cleanup unverified'):
        sandbox.check('def add(a,b): return a+b', cases(), time.time()+60)


def test_stop_blocks_any_container_command(sandbox):
    (sandbox.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        sandbox.check('def add(a,b): return a+b', cases(), time.time()+60)


def test_container_task_requires_explicit_cases_and_pinned_image():
    with pytest.raises(ValidationError):
        TaskRequest(summary='No cases', qa_mode='container')
    with pytest.raises(ValidationError):
        DockerRuntimeConfig(executable='docker', image_id='python:latest')
