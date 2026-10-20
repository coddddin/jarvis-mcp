"""Real static-QA workers; model responses are isolated test fixtures, not live AI."""
import hashlib
import json
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError

from jarvis.agents.models import StepResult, TaskRequest
from jarvis.agents.runner import LocalConfig, LocalRunner
from jarvis.agents.store import TaskError, TaskStore
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.core.paths import WORKERS_ROOT
from jarvis.workers import agent_step


@pytest.fixture
def store(tmp_path):
    return TaskStore(Guardrails(tmp_path))


class FixtureRunner(LocalRunner):
    def __init__(self, store, sources):
        super().__init__(store, LocalConfig())
        self.sources = iter(sources)
        self.model_calls = 0

    def _worker(self, mode, payload, deadline):
        if mode == 'generate':
            self.model_calls += 1
            if payload['role'] == 'planner':
                return {'summary': 'Prepare complete Python source and check syntax.'}
            return {'summary': 'Prepared Python proposal.', 'proposal': next(self.sources)}
        return super()._worker(mode, payload, deadline)


def test_actual_static_qa_evidence_finishes_without_executing_source(store):
    marker = store.guard.workspace / 'must-not-exist'
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('not executed')\n"
    task = store.create(TaskRequest(summary='Syntax-only proposal'))
    result = FixtureRunner(store, [source]).run(task.task_id)
    assert result.status == 'completed'
    evidence = result.history[-1].qa_evidence
    assert evidence.passed
    assert evidence.sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert not marker.exists()
    assert TaskStore(Guardrails(store.guard.workspace)).get(task.task_id).history[-1].qa_evidence == evidence


def test_failed_actual_qa_is_feedback_then_corrected(store):
    task = store.create(TaskRequest(summary='Prepare valid Python'))
    runner = FixtureRunner(store, ['if True\n    print(1)', 'if True:\n    print(1)'])
    result = runner.run(task.task_id)
    assert result.status == 'completed'
    assert result.correction_retries == 1
    assert [r.qa_evidence.passed for r in result.history if r.role == 'qa'] == [False, True]


def test_static_failures_stop_after_three_corrections(store):
    task = store.create(TaskRequest(summary='Repeated bad proposals'))
    runner = FixtureRunner(store, ['if True'] * 4)
    result = runner.run(task.task_id)
    assert result.status == 'needs_user_input'
    assert result.correction_retries == 3
    assert runner.model_calls == 5  # one planner and four coder calls
    assert runner.run(task.task_id) == result
    assert runner.model_calls == 5


@pytest.mark.parametrize('response', [{'summary': 'Fake QA', 'qa_passed': True},
                                    {'summary': 'Blank', 'proposal': ' '},
                                    {'summary': 'Secret', 'proposal': 'password="private"'}])
def test_invalid_coder_reply_cannot_be_marked_passed(store, response, monkeypatch):
    task = store.create(TaskRequest(summary='Contract rejection'))
    runner = FixtureRunner(store, ['unused'])
    runner.step(task.task_id)
    monkeypatch.setattr(runner, '_worker', lambda *args: response)
    with pytest.raises(TaskError):
        runner.step(task.task_id)
    failed = store.get(task.task_id)
    assert failed.status == 'needs_user_input'
    assert failed.steps_started == 2
    assert len(failed.history) == 1


def test_worker_failure_is_charged_once_and_persisted(store, monkeypatch):
    task = store.create(TaskRequest(summary='Transport failure'))
    runner = LocalRunner(store, LocalConfig())
    def fail(*_):
        raise OSError('must not store raw response or credentials')
    monkeypatch.setattr(runner, '_worker', fail)
    with pytest.raises(TaskError):
        runner.step(task.task_id)
    state = store.get(task.task_id)
    assert state.status == 'needs_user_input' and state.steps_started == 1
    assert 'credentials' not in state.reason
    assert runner.run(task.task_id).steps_started == 1


def test_forged_qa_evidence_hash_rejected(store, monkeypatch):
    task = store.create(TaskRequest(summary='Evidence matching'))
    runner = FixtureRunner(store, ['print(1)'])
    runner.step(task.task_id)
    runner.step(task.task_id)
    monkeypatch.setattr(runner, '_worker', lambda *args: {
        'checker': 'python_syntax_v1', 'sha256': '0' * 64, 'passed': True, 'detail': 'Forged'})
    with pytest.raises(TaskError):
        runner.step(task.task_id)
    assert store.get(task.task_id).status == 'needs_user_input'


def test_worker_request_deadline_terminates_process(store):
    runner = LocalRunner(store, LocalConfig())
    with pytest.raises(TaskError, match='timed out'):
        runner._worker('qa', {'proposal': 'print(1)'}, time.time() - 1)


def test_stopped_runner_does_not_admit_or_execute(store):
    task = store.create(TaskRequest(summary='Stop before admission'))
    runner = FixtureRunner(store, ['print(1)'])
    (store.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        runner.run(task.task_id)
    assert runner.model_calls == 0


@pytest.mark.parametrize('model', ['qwen:cloud', 'https://remote/model:tag', '../bad:tag'])
def test_nonlocal_configuration_rejected(model):
    with pytest.raises(ValidationError):
        LocalConfig(model=model)


def test_uninstalled_or_cloud_model_is_rejected_before_chat(monkeypatch):
    calls = []
    def request(path, body=None):
        calls.append(path)
        return {'models': [{'name': 'qwen2.5-coder:3b'}]} if path == '/api/tags' else {
            'remote_host': 'remote.invalid', 'details': {'format': 'gguf'}}
    monkeypatch.setattr(agent_step, 'request', request)
    with pytest.raises(ValueError):
        agent_step.local_model('qwen2.5-coder:3b')
    assert calls == ['/api/tags', '/api/show']
    calls.clear()
    with pytest.raises(ValueError):
        agent_step.local_model('missing:latest')
    assert calls == ['/api/tags']


def test_structured_request_has_fixed_bounds_and_no_tool_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_step, 'local_model', lambda model: None)
    def request(path, body):
        calls.append((path, body))
        return {'done': True, 'done_reason': 'stop',
                'message': {'role': 'assistant', 'content': '{"summary":"Plan"}'}}
    monkeypatch.setattr(agent_step, 'request', request)
    assert agent_step.generate({'model': 'qwen2.5-coder:3b', 'role': 'planner',
                               'schema': {}, 'context': 'Explicit task'}) == {'summary': 'Plan'}
    path, body = calls[0]
    assert path == '/api/chat' and body['stream'] is False
    assert body['options']['num_predict'] == 1024
    assert body['options']['num_ctx'] == 8192
    assert 'tools' not in body


def test_http_redirect_is_refused_and_connection_is_fixed_loopback(monkeypatch):
    seen = []
    class Connection:
        def __init__(self, host, port, timeout):
            seen.append((host, port))
        def request(self, *args):
            pass
        def getresponse(self):
            return type('Response', (), {'status': 302})()
        def close(self):
            seen.append('closed')
    monkeypatch.setattr(agent_step.http.client, 'HTTPConnection', Connection)
    with pytest.raises(ValueError):
        agent_step.request('/api/tags')
    assert seen == [('127.0.0.1', 11434), 'closed']


def test_cli_help_does_not_load_models_or_create_storage():
    result = subprocess.run([sys.executable, '-m', 'jarvis.cli.agents', '--help'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and 'static QA only' in result.stdout
