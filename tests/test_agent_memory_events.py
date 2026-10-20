import asyncio
import json
import os
import sqlite3

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.agents.events import AsyncEventBus, EventError
from jarvis.agents.memory import MemoryInput, MemoryScope, MemoryStore, MemoryError
from jarvis.agents.models import StepResult, TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.agents.runner import LocalRunner, LocalConfig
from jarvis.app.events import create_event_app, load_event_token
from jarvis.core.guardrails import Guardrails, GuardrailError


@pytest.fixture
def tasks(tmp_path):
    return TaskStore(Guardrails(tmp_path))


def memory_input(**changes):
    return MemoryInput(project='projects/demo', kind='preference', key='style', value='Use short functions', **changes)


def terminal(tasks):
    task = tasks.create(TaskRequest(summary='Private request should not appear in events'))
    tasks.interrupt(task.task_id, 'Cancelled', cancelled=True)
    return task.task_id


def test_memory_confirm_edit_revision_and_namespace(tasks):
    store = MemoryStore(tasks)
    scope = MemoryScope(project='projects/demo')
    record = store.put(memory_input())
    assert store.list(scope, confirmed_only=True) == ()
    confirmed = store.confirm(scope, record.memory_id, record.revision)
    assert confirmed.confirmed and confirmed.untrusted_data
    assert MemoryStore(TaskStore(tasks.guard)).list(scope, confirmed_only=True) == (confirmed,)
    assert store.list(MemoryScope(owner='other', project='projects/demo')) == ()
    assert store.list(MemoryScope(project='projects/other')) == ()
    edited = store.put(memory_input())
    assert edited.memory_id == record.memory_id and not edited.confirmed
    with pytest.raises(MemoryError, match='changed'):
        store.confirm(scope, edited.memory_id, confirmed.revision)
    store.delete(scope, edited.memory_id, edited.revision)
    assert store.list(scope) == ()


def test_memory_expiry_and_capacity(tasks, monkeypatch):
    store = MemoryStore(tasks)
    record = store.put(memory_input(retention_days=1))
    monkeypatch.setattr(store, 'MAX_RECORDS', 1)
    with pytest.raises(MemoryError):
        store.put(MemoryInput(project='projects/demo', kind='path', key='other', value='projects/demo'))
    monkeypatch.setattr('jarvis.agents.memory.time.time', lambda: record.expires_at + 1)
    assert store.list(MemoryScope(project='projects/demo')) == ()


@pytest.mark.parametrize('value', ['password=never-store', 'Bearer never-store', 'sk-never-store'])
def test_memory_secret_rejected_before_storage(tasks, value):
    MemoryStore(tasks)
    with pytest.raises(ValidationError):
        MemoryInput(project='projects/demo', kind='preference', key='test', value=value)
    assert b'never-store' not in (tasks.guard.workspace / tasks.DATABASE).read_bytes()


def test_runner_uses_only_opted_in_confirmed_scope(tasks, monkeypatch):
    memory = MemoryStore(tasks)
    record = memory.put(memory_input())
    task = tasks.create(TaskRequest(summary='Use explicit preferences', memory_project='projects/demo'))
    runner = LocalRunner(tasks, LocalConfig())
    contexts = []
    def generate(mode, payload, deadline):
        contexts.append(json.loads(payload['context']))
        return {'summary': 'Plan'}
    monkeypatch.setattr(runner, '_worker', generate)
    runner.step(task.task_id)
    assert contexts[0]['untrusted_confirmed_preferences'] == []
    memory.confirm(MemoryScope(project='projects/demo'), record.memory_id, record.revision)
    another = tasks.create(TaskRequest(summary='Confirmed preference', memory_project='projects/demo'))
    runner.step(another.task_id)
    assert contexts[1]['untrusted_confirmed_preferences'][0]['not_permissions'] is True
    assert contexts[1]['untrusted_confirmed_preferences'][0]['value'] == 'Use short functions'


def test_events_order_replay_no_private_data_and_atomic_rollback(tasks):
    task_id = terminal(tasks)
    page = tasks.events(task_id)
    assert [e.sequence for e in page.events] == [1, 2]
    assert page.terminal and page.events[-1].payload.status == 'cancelled'
    assert 'Private request' not in page.model_dump_json()
    assert tasks.events(task_id, after=1).events == page.events[1:]
    with tasks._connection() as db:
        task = tasks._read(db, task_id)
    before = tasks.events(task_id)
    with pytest.raises(RuntimeError):
        with tasks._connection() as db:
            tasks._emit(db, task, 'task.state')
            raise RuntimeError('transaction rollback')
    assert tasks.events(task_id) == before


@pytest.mark.parametrize('after', [-1, True, '0', 999])
def test_invalid_event_cursors(tasks, after):
    with pytest.raises(EventError):
        tasks.events(terminal(tasks), after=after)


def test_deleted_event_gap_detected(tasks):
    task_id = terminal(tasks)
    with sqlite3.connect(tasks.guard.workspace / tasks.DATABASE) as db:
        db.execute('DELETE FROM task_events WHERE task_id=? AND sequence=1', (task_id,))
    with pytest.raises(EventError, match='gap'):
        tasks.events(task_id)


def test_deleted_terminal_event_detected(tasks):
    task_id = terminal(tasks)
    with sqlite3.connect(tasks.guard.workspace / tasks.DATABASE) as db:
        db.execute('DELETE FROM task_events WHERE task_id=? AND sequence=2', (task_id,))
    with pytest.raises(EventError, match='snapshot'):
        tasks.events(task_id, after=1)


def test_async_slow_consumer_and_reconnect_keeps_terminal(tasks):
    task_id = terminal(tasks)
    async def consume():
        first = []
        async for event in AsyncEventBus(tasks).stream(task_id):
            first.append(event)
            break
        rest = []
        await asyncio.sleep(0.02)
        async for event in AsyncEventBus(TaskStore(tasks.guard)).stream(task_id, after=first[-1].sequence):
            rest.append(event)
        return first + rest
    result = asyncio.run(consume())
    assert [e.sequence for e in result] == [1, 2]
    assert result[-1].payload.status == 'cancelled'


def app(tasks):
    task_id = terminal(tasks)
    other_id = terminal(tasks)
    application = create_event_app(tasks, token='x' * 43, task_ids=[task_id], origins=['http://127.0.0.1:3000'])
    return application, task_id, other_id


@pytest.mark.parametrize('bad', ['token', 'origin', 'task'])
def test_websocket_auth_origin_and_task_denied(tasks, bad):
    application, task_id, other_id = app(tasks)
    headers = {'origin': 'http://127.0.0.1:3000', 'authorization': 'Bearer ' + 'x' * 43}
    if bad == 'token':
        headers['authorization'] = 'Bearer ' + 'y' * 43
    if bad == 'origin':
        headers['origin'] = 'https://attacker.invalid'
    with TestClient(application) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f'/tasks/{other_id if bad == "task" else task_id}/events', headers=headers):
                pass


def test_websocket_header_and_browser_first_frame_replay(tasks):
    application, task_id, _ = app(tasks)
    with TestClient(application) as client:
        with client.websocket_connect(f'/tasks/{task_id}/events', headers={
            'origin': 'http://127.0.0.1:3000', 'authorization': 'Bearer ' + 'x' * 43}) as socket:
            assert socket.receive_json()['sequence'] == 1
            assert socket.receive_json()['sequence'] == 2
            assert socket.receive_json()['kind'] == 'stream.end'
        with client.websocket_connect(f'/tasks/{task_id}/events?after=1', headers={'origin': 'http://127.0.0.1:3000'}) as socket:
            socket.send_json({'token': 'x' * 43})
            assert socket.receive_json()['sequence'] == 2


def test_token_creation_reuse_and_link_denial(tmp_path):
    root = tmp_path / 'auth'
    token = load_event_token(root)
    assert len(token) == 43 and load_event_token(root) == token
    os.link(root / 'agent-events-token.txt', root / 'copy.txt')
    with pytest.raises(ValueError, match='Linked'):
        load_event_token(root)


def test_memory_and_events_obey_stop_marker(tasks):
    memory = MemoryStore(tasks)
    task_id = terminal(tasks)
    (tasks.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        memory.list(MemoryScope(project='projects/demo'))
    with pytest.raises(GuardrailError):
        tasks.events(task_id)
