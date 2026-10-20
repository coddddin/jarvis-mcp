import pytest

from jarvis.agents.models import StepResult, TaskRequest
from jarvis.agents.runner import LocalRunner, LocalConfig
from jarvis.agents.store import TaskStore, TaskError
from jarvis.core.guardrails import Guardrails


@pytest.fixture
def store(tmp_path):
    return TaskStore(Guardrails(tmp_path))


def failed_planner(store, **changes):
    task = store.create(TaskRequest(summary='Recover a failed call', **changes))
    store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    return store.interrupt(task.task_id, 'Model transport failed')


def test_resume_preserves_charges_deadline_and_requires_explicit_feedback(store):
    task = failed_planner(store)
    recovered = TaskStore(store.guard).resume(task.task_id, 'Local model is running again')
    assert recovered.recovery_ready and recovered.deadline == task.deadline
    assert recovered.generation_tokens_reserved == 1024 and recovered.transport_retries == 1
    retry = store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    assert retry.steps_started == 1 and retry.pending_generation_tokens == 2048
    result = store.finish_step(task.task_id, StepResult(step_id=retry.active_step, role='planner',
        summary='Recovered plan', reserved_tokens=2048))
    assert result.expected_role == 'coder' and result.generation_tokens_reserved == 2048


def test_resume_limit_is_persisted_across_instances(store):
    task = failed_planner(store)
    for attempt in range(2):
        store = TaskStore(store.guard)
        store.resume(task.task_id, 'Explicit operator feedback')
        store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
        current = store.interrupt(task.task_id, 'Transport still unavailable')
        assert current.transport_retries == attempt + 1
    with pytest.raises(TaskError, match='remaining retry'):
        store.resume(task.task_id, 'Third extra attempt forbidden')
    assert store.get(task.task_id).generation_tokens_reserved == 3072


def test_resume_does_not_bypass_output_budget(store):
    task = failed_planner(store, max_generation_tokens=1024)
    store.resume(task.task_id, 'Retry within the original immutable budget')
    with pytest.raises(TaskError, match='budget'):
        store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    assert store.get(task.task_id).status == 'needs_user_input'


def test_resume_does_not_extend_deadline_or_reset_correction_limit(store, monkeypatch):
    task = failed_planner(store)
    monkeypatch.setattr('jarvis.agents.store.time.time', lambda: task.deadline + 1)
    with pytest.raises(TaskError, match='deadline'):
        store.resume(task.task_id, 'Cannot extend the deadline')


def test_resumed_runner_succeeds_without_silent_retry_or_counter_reset(store, monkeypatch):
    task = failed_planner(store)
    store.resume(task.task_id, 'Proceed with the same requirements')
    runner = LocalRunner(store, LocalConfig())
    real = runner._worker
    def work(mode, payload, deadline):
        if mode == 'generate':
            return {'summary':'Plan'} if payload['role']=='planner' else {'summary':'Code', 'proposal':'def add(a,b): return a+b'}
        return real(mode, payload, deadline)
    monkeypatch.setattr(runner, '_worker', work)
    final = runner.run(task.task_id)
    assert final.status == 'completed' and final.steps_started == 3
    assert final.transport_retries == 1 and final.generation_tokens_reserved == 3072
