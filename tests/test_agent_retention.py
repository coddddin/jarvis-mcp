import pytest

from jarvis.agents.models import StepResult, TaskRequest
from jarvis.agents.store import TaskStore, TaskError
from jarvis.core.guardrails import Guardrails


@pytest.fixture
def store(tmp_path):
    return TaskStore(Guardrails(tmp_path))


def test_combined_context_output_budget_blocks_before_call(store):
    task = store.create(TaskRequest(summary='One model call', max_model_tokens=9216))
    step = store.begin_step(task.task_id, 'planner', reserve_output_tokens=1024)
    state = store.finish_step(task.task_id, StepResult(step_id=step.active_step, role='planner', summary='Plan', reserved_tokens=1024))
    assert state.model_tokens_reserved == 9216
    with pytest.raises(TaskError, match='context/output'):
        store.begin_step(task.task_id, 'coder', reserve_output_tokens=1024)
    assert store.get(task.task_id).generation_tokens_reserved == 1024


def test_explicit_forget_removes_evidence_and_events_only(store):
    task = store.create(TaskRequest(summary='Transient request'))
    marker = store.guard.workspace / 'user.txt'
    marker.write_text('preserve')
    with pytest.raises(TaskError, match='Cancel'):
        store.forget(task.task_id)
    store.interrupt(task.task_id, 'User cancelled', cancelled=True)
    assert store.events(task.task_id).terminal
    assert store.forget(task.task_id)['backups_and_applied_files_preserved']
    with pytest.raises(TaskError, match='exist'):
        store.get(task.task_id)
    with store._connection() as db:
        assert db.execute('SELECT count(*) FROM task_events').fetchone()[0] == 0
    assert marker.read_text() == 'preserve'


def test_terminal_retention_prunes_evidence_after_days(store, monkeypatch):
    task = store.create(TaskRequest(summary='Expires', retention_days=1))
    task = store.interrupt(task.task_id, 'Finished', cancelled=True)
    monkeypatch.setattr('jarvis.agents.store.time.time', lambda: task.updated_at + 86401)
    assert store.prune()['expired_tasks_removed'] == 1
    assert store.prune()['expired_tasks_removed'] == 0


def test_capacity_cannot_be_bypassed_by_new_task_ids(store, monkeypatch):
    monkeypatch.setattr(store, 'MAX_TASKS', 1)
    task = store.create(TaskRequest(summary='First'))
    with pytest.raises(TaskError, match='capacity'):
        store.create(TaskRequest(summary='Second'))
    store.interrupt(task.task_id, 'Cancel', cancelled=True)
    store.forget(task.task_id)
    assert store.create(TaskRequest(summary='Now room')).status == 'ready'
