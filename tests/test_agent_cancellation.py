import subprocess

import pytest

from jarvis.agents.models import TaskRequest
from jarvis.agents.runner import LocalRunner, LocalConfig
from jarvis.agents.store import TaskStore, TaskError
from jarvis.core.guardrails import Guardrails


def test_other_store_cancellation_terminates_active_worker(tmp_path, monkeypatch):
    tasks = TaskStore(Guardrails(tmp_path))
    task = tasks.create(TaskRequest(summary='Cancel an active model step'))
    step = tasks.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    process = type('FakeProcess', (), {'returncode':None, 'killed':False})()
    def communicate(*args, **kwargs):
        TaskStore(Guardrails(tmp_path)).interrupt(task.task_id, 'Cancel from another process', cancelled=True)
        raise subprocess.TimeoutExpired('fixed-worker', 0.2)
    def kill():
        process.killed = True
        process.returncode = -1
    process.communicate, process.kill = communicate, kill
    process.poll = lambda: process.returncode
    process.wait = lambda **kwargs: process.returncode
    monkeypatch.setattr('jarvis.agents.runner.subprocess.Popen', lambda *args, **kwargs: process)
    runner = LocalRunner(tasks, LocalConfig())
    with pytest.raises(TaskError, match='cancelled'):
        runner._worker('generate', {'_task_id': task.task_id, '_step_id': step.active_step}, task.deadline)
    assert process.killed
    final = tasks.get(task.task_id)
    assert final.status == 'cancelled' and final.generation_tokens_reserved == 1024


def test_case_task_cannot_complete_without_matching_qa_evidence(tmp_path):
    tasks = TaskStore(Guardrails(tmp_path))
    task = tasks.create(TaskRequest.model_validate_json(
        '{"summary":"Explicit cases","integer_cases":[{"function":"add","args":[1,2],"expected":3}]}'))
    from jarvis.agents.models import StepResult
    for role in ['planner', 'coder']:
        step = tasks.begin_step(task.task_id, role)
        tasks.finish_step(task.task_id, StepResult(step_id=step.active_step, role=role,
            summary='Result', proposal='def add(a,b): return a+b' if role=='coder' else ''))
    step = tasks.begin_step(task.task_id, 'qa')
    with pytest.raises(ValueError, match='evidence'):
        tasks.finish_step(task.task_id, StepResult(step_id=step.active_step, role='qa', summary='Unverified', qa_passed=True))
    assert tasks.get(task.task_id).status == 'running'
