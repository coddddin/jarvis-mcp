import json

import pytest
from pydantic import ValidationError

from jarvis.agents.models import IntegerCase, StepResult, TaskRequest, QAEvidence
from jarvis.agents.runner import LocalConfig, LocalRunner
from jarvis.agents.store import TaskStore, TaskError
from jarvis.core.guardrails import Guardrails
from jarvis.workers.agent_step import check_integer_cases


CASES = [{'function': 'add', 'args': [2, 3], 'expected': 5},
         {'function': 'add', 'args': [-2, 2], 'expected': 0}]


@pytest.fixture
def store(tmp_path):
    return TaskStore(Guardrails(tmp_path))


def test_integer_qa_and_hash_are_bound_to_cases():
    result = check_integer_cases('def add(a:int, b:int)->int:\n return a+b', CASES)
    assert result['passed'] and result['cases_passed'] == result['cases_total'] == 2
    assert QAEvidence.model_validate(result).checker == 'python_integer_cases_v1'
    changed = check_integer_cases('def add(a,b):\n return a+b', [{**CASES[0], 'expected': 4}])
    assert not changed['passed'] and changed['cases_sha256'] != result['cases_sha256']


@pytest.mark.parametrize('source', [
    "import os\ndef add(a,b): return a+b",
    "def add(a,b): return __import__('os').system('echo forbidden')",
    "def add(a,b): return a if a>0 else __import__('os')",
    "def add(a,b): return a.__class__",
    "def add(a,b): return a**100000000",
    "def add(a,b):\n while True: pass",
    "@print\ndef add(a,b): return a+b",
    "def add(a,b=print('forbidden')): return a+b",
    "def add(a,b): return 1//0",
    "def add(a,b): return 'a'*100000000",
    "def add(a,b): return [x for x in range(a)]",
])
def test_outside_integer_language_never_runs(source):
    result = check_integer_cases(source, CASES)
    assert not result['passed']
    QAEvidence.model_validate(result)


def test_integer_conditionals_and_bounds():
    cases = [{'function': 'bigger', 'args': [-2, 5], 'expected': 5}]
    assert check_integer_cases('def bigger(a,b): return a if a>b else b', cases)['passed']
    huge = 'def add(a,b): return ' + '*'.join(['a'] * 24)
    assert not check_integer_cases(huge, [{'function': 'add', 'args': [10**9, 0], 'expected': 0}])['passed']


@pytest.mark.parametrize('data', [
    {'function': 'add', 'args': [True, 1], 'expected': 2},
    {'function': 'add', 'args': [10**10], 'expected': 0},
    {'function': '../bad', 'args': [1], 'expected': 1},
])
def test_integer_case_contract(data):
    with pytest.raises(ValidationError):
        IntegerCase.model_validate_json(json.dumps(data))
    with pytest.raises(ValueError):
        check_integer_cases('def add(a,b): return a+b', [data])


def test_generation_reservations_survive_restart_and_block_before_admission(store):
    task = store.create(TaskRequest(summary='Token-bound task', max_generation_tokens=1024))
    step = store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    store.finish_step(task.task_id, StepResult(step_id=step.active_step, role='planner', summary='Plan', reserved_tokens=1024))
    reopened = TaskStore(Guardrails(store.guard.workspace))
    with pytest.raises(TaskError, match='reservation budget'):
        reopened.begin_step(task.task_id, 'coder', reserve_output_tokens=1024)
    state = reopened.get(task.task_id)
    assert state.status == 'needs_user_input'
    assert state.steps_started == 1 and state.generation_tokens_reserved == 1024


def test_failed_model_call_does_not_refund_output_reservation(store, monkeypatch):
    task = store.create(TaskRequest(summary='Failed call remains charged'))
    runner = LocalRunner(store, LocalConfig())
    def fail(*_):
        raise OSError('Transport failed')
    monkeypatch.setattr(runner, '_worker', fail)
    with pytest.raises(TaskError):
        runner.step(task.task_id)
    state = TaskStore(Guardrails(store.guard.workspace)).get(task.task_id)
    assert state.generation_tokens_reserved == state.pending_generation_tokens == 1024
    assert state.status == 'needs_user_input'


def test_result_cannot_omit_precharged_reservation(store):
    task = store.create(TaskRequest(summary='Reservation accounting'))
    step = store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    with pytest.raises(TaskError):
        store.finish_step(task.task_id, StepResult(step_id=step.active_step, role='planner', summary='No charge'))
    assert store.get(task.task_id).status == 'running'


def test_runner_uses_real_restricted_qa_and_persisted_budget(store, monkeypatch):
    task = store.create(TaskRequest.model_validate_json(json.dumps({'summary': 'Implement add', 'integer_cases': CASES})))
    runner = LocalRunner(store, LocalConfig())
    worker = runner._worker
    def generate_or_check(mode, payload, deadline):
        if mode == 'generate':
            return {'summary': 'Plan'} if payload['role'] == 'planner' else {'summary': 'Code', 'proposal': 'def add(a,b): return a+b'}
        return worker(mode, payload, deadline)
    monkeypatch.setattr(runner, '_worker', generate_or_check)
    result = runner.run(task.task_id)
    assert result.status == 'completed'
    assert result.generation_tokens_reserved == 2048 and result.pending_generation_tokens == 0
    assert result.history[-1].qa_evidence.cases_passed == 2
