"""Transactional task admission and retry accounting, independent of model vendors."""
from contextlib import contextmanager
import re
import sqlite3
import time
from uuid import uuid4

from pydantic import ValidationError

from jarvis.agents.models import Role, StepResult, Task, TaskRequest, TERMINAL
from jarvis.agents.events import EventBatch, EventError, EventState, TaskEvent
from jarvis.core.guardrails import Guardrails, GuardrailError
from jarvis.core.redaction import redact


class TaskError(ValueError):
    """A task state transition or stored record was rejected."""


class TaskNotFound(TaskError):
    """An explicitly forgotten, expired or unknown task has no stored record."""


class TaskStore:
    """Trusted Python API. Does not execute tools, models, code or QA commands.

    Every role must be admitted before execution. The caller still uses the
    existing guarded MCP tools; these step counters never reset their budget.
    """
    DIRECTORY = '.jarvis-agents'
    DATABASE = '.jarvis-agents/tasks.sqlite3'
    MAX_TASKS = 1000

    def __init__(self, guard: Guardrails):
        self.guard = guard
        self.guard.check_running()
        directory = guard.validate_path(self.DIRECTORY, must_exist=False)
        directory.mkdir(exist_ok=True)
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks '
                       '(id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS task_events (task_id TEXT NOT NULL, sequence INTEGER NOT NULL, '
                       'record TEXT NOT NULL, PRIMARY KEY(task_id, sequence))')

    @contextmanager
    def _connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY)
        if not directory.is_dir():
            raise GuardrailError('Task storage must be a directory.')
        path = self.guard.validate_path(self.DATABASE, must_exist=False)
        # SQLite may also read/write journal and shared-memory siblings.
        for name in (path.name, path.name + '-journal', path.name + '-wal', path.name + '-shm'):
            sibling = self.guard.validate_path(str(directory / name), must_exist=False)
            if sibling.exists() and (not sibling.is_file() or sibling.stat().st_nlink != 1):
                raise GuardrailError('Linked or non-file task storage is forbidden.')
        db = None
        try:
            db = sqlite3.connect(path, timeout=1, isolation_level=None)
            # Serialize admission across threads, instances and server restarts.
            db.execute('BEGIN IMMEDIATE')
            yield db
            self.guard.check_running()
            db.commit()
        except sqlite3.Error as exc:
            raise TaskError('Task storage failed; no successful transition is reported.') from exc
        finally:
            if db is not None:
                db.close()  # Uncommitted transactions are rolled back on failure.

    @staticmethod
    def _id(task_id: str):
        if not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise TaskError('Invalid task ID.')

    def _read(self, db, task_id: str) -> Task:
        self._id(task_id)
        row = db.execute('SELECT record FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound('Task does not exist.')
        try:
            task = Task.model_validate_json(row[0])
        except ValidationError as exc:
            raise TaskError('Invalid persisted task state; operator review required.') from exc
        if task.task_id != task_id:
            raise TaskError('Persisted task identity mismatch.')
        return task

    @staticmethod
    def _change(task: Task, **changes) -> Task:
        return Task.model_validate({**task.model_dump(), **changes})

    @staticmethod
    def _save(db, task: Task):
        db.execute('UPDATE tasks SET record = ? WHERE id = ?',
                   (task.model_dump_json(), task.task_id))
        TaskStore._emit(db, task, 'task.state')

    @staticmethod
    def _emit(db, task, kind):
        sequence = db.execute('SELECT COALESCE(MAX(sequence), 0)+1 FROM task_events WHERE task_id=?',
                              (task.task_id,)).fetchone()[0]
        event = TaskEvent(event_id=uuid4().hex, task_id=task.task_id, sequence=sequence,
            kind=kind, agent_id=task.expected_role, timestamp=time.time(),
            payload=EventState(status=task.status, role=task.expected_role, steps_started=task.steps_started,
                correction_retries=task.correction_retries, generation_tokens_reserved=task.generation_tokens_reserved))
        db.execute('INSERT INTO task_events VALUES (?,?,?)', (task.task_id, sequence, event.model_dump_json()))

    def events(self, task_id, *, after=0, limit=32):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise EventError('Invalid event cursor or page size.')
        with self._connection() as db:
            task = self._expire(db, self._read(db, task_id))
            latest = db.execute('SELECT COALESCE(MAX(sequence),0) FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
            if latest:
                last_row = db.execute('SELECT record FROM task_events WHERE task_id=? AND sequence=?', (task_id, latest)).fetchone()
                last_event = TaskEvent.model_validate_json(last_row[0])
                expected = EventState(status=task.status, role=task.expected_role, steps_started=task.steps_started,
                    correction_retries=task.correction_retries, generation_tokens_reserved=task.generation_tokens_reserved)
                if last_event.payload != expected:
                    raise EventError('Latest event differs from task snapshot; explicit recovery required.')
            if after > latest:
                raise EventError('Cursor is ahead of this task; obtain a fresh task snapshot.')
            rows = db.execute('SELECT record FROM task_events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                              (task_id, after, limit)).fetchall()
            events = tuple(TaskEvent.model_validate_json(row[0]) for row in rows)
            if any(e.task_id != task_id or e.sequence != after + index + 1 for index, e in enumerate(events)):
                raise EventError('Event gap or identity mismatch; obtain a fresh task snapshot.')
            if not events and after < latest:
                raise EventError('Event gap; obtain a fresh task snapshot.')
            return EventBatch(events=events, next_sequence=events[-1].sequence if events else after,
                              latest_sequence=latest, terminal=task.status in TERMINAL)

    def create(self, request: TaskRequest) -> Task:
        request = TaskRequest.model_validate(request.model_dump())
        now = time.time()
        task = Task(task_id=uuid4().hex, request=request, created_at=now,
                    deadline=now + request.timeout_seconds, updated_at=now)
        with self._connection() as db:
            self._prune(db)
            if db.execute('SELECT count(*) FROM tasks').fetchone()[0] >= self.MAX_TASKS:
                raise TaskError('Task retention capacity reached; explicitly forget old terminal tasks.')
            db.execute('INSERT INTO tasks VALUES (?, ?)', (task.task_id, task.model_dump_json()))
            self._emit(db, task, 'task.created')
        return task

    def _prune(self, db):
        removed = 0
        ids = [row[0] for row in db.execute('SELECT id FROM tasks')]
        for task_id in ids:
            task = self._expire(db, self._read(db, task_id))
            if task.status in TERMINAL and task.updated_at + task.request.retention_days * 86400 <= time.time():
                db.execute('DELETE FROM task_events WHERE task_id=?', (task_id,))
                db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
                removed += 1
        return removed

    def prune(self):
        with self._connection() as db:
            return {'expired_tasks_removed': self._prune(db)}

    def forget(self, task_id):
        with self._connection() as db:
            task = self._expire(db, self._read(db, task_id))
            if task.status not in TERMINAL:
                raise TaskError('Cancel or finish a running task before forgetting its evidence.')
            db.execute('DELETE FROM task_events WHERE task_id=?', (task_id,))
            db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
        return {'forgotten': task_id, 'backups_and_applied_files_preserved': True}

    def _expire(self, db, task: Task) -> Task:
        if task.status not in TERMINAL and time.time() >= task.deadline:
            task = self._change(task, status='timed_out', active_step=None,
                                recovery_ready=False,
                                reason='Task deadline reached.', updated_at=time.time())
            self._save(db, task)
        return task

    def recent(self, *, limit=8):
        """Bounded operator listing; validates records before returning any IDs."""
        if type(limit) is not int or not 1 <= limit <= 32:
            raise TaskError('Choose 1 to 32 recent tasks.')
        with self._connection() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM tasks LIMIT ?', (self.MAX_TASKS + 1,))]
            if len(ids) > self.MAX_TASKS:
                raise TaskError('Task storage capacity exceeded.')
            tasks = [self._read(db, task_id) for task_id in ids]
            return tuple(sorted(tasks, key=lambda task: task.updated_at, reverse=True)[:limit])

    def get(self, task_id: str) -> Task:
        with self._connection() as db:
            task = self._expire(db, self._read(db, task_id))
        return task

    def begin_step(self, task_id: str, role: Role, *, reserve_output_tokens: int = 0) -> Task:
        if type(reserve_output_tokens) is not int or reserve_output_tokens not in {0, 1024} or role == 'qa' and reserve_output_tokens:
            raise TaskError('Invalid fixed model output reservation.')
        error = None
        with self._connection() as db:
            task = self._expire(db, self._read(db, task_id))
            if task.status != 'ready':
                error = 'Task is not ready; inspect its persisted status.'
            elif role != task.expected_role:
                error = 'Unexpected role; planner, coder and QA order is enforced.'
            elif task.generation_tokens_reserved + reserve_output_tokens > task.request.max_generation_tokens:
                task = self._change(task, status='needs_user_input',
                                    recovery_ready=False,
                                    reason='Shared output-generation reservation budget exhausted.', updated_at=time.time())
                self._save(db, task)
                error = task.reason
            elif (task.generation_tokens_reserved + reserve_output_tokens) * 9 > task.request.max_model_tokens:
                task = self._change(task, status='needs_user_input',
                                    recovery_ready=False,
                                    reason='Shared context/output reservation budget exhausted.', updated_at=time.time())
                self._save(db, task)
                error = task.reason
            elif task.steps_started >= task.request.max_steps and not task.recovery_ready:
                task = self._change(task, status='needs_user_input',
                                    recovery_ready=False,
                                    reason='Shared role-step budget exhausted.', updated_at=time.time())
                self._save(db, task)
                error = task.reason
            else:
                recovering = task.recovery_ready
                correction = not recovering and role == 'coder' and bool(task.history) and task.history[-1].role == 'qa'
                task = self._change(task, status='running', active_step=uuid4().hex,
                                    steps_started=task.steps_started + int(not recovering),
                                    correction_retries=task.correction_retries + int(correction),
                                    generation_tokens_reserved=task.generation_tokens_reserved + reserve_output_tokens,
                                    pending_generation_tokens=(task.pending_generation_tokens if recovering else 0) + reserve_output_tokens,
                                    recovery_ready=False,
                                    updated_at=time.time())
                self._save(db, task)
        if error:
            raise TaskError(error)
        return task

    def finish_step(self, task_id: str, result: StepResult) -> Task:
        result = StepResult.model_validate(result.model_dump())
        error = None
        with self._connection() as db:
            task = self._expire(db, self._read(db, task_id))
            if (task.status != 'running' or result.step_id != task.active_step or result.role != task.expected_role
                    or result.reserved_tokens != task.pending_generation_tokens):
                error = 'Result is stale, duplicated, expired or belongs to another role.'
            else:
                status, next_role, reason = 'ready', task.expected_role, ''
                if result.role == 'planner':
                    next_role = 'coder'
                elif result.role == 'coder':
                    next_role = 'qa'
                elif result.qa_passed:
                    status = 'completed'
                elif task.correction_retries >= 3:
                    status, reason = 'needs_user_input', 'QA failed after three correction retries.'
                else:
                    next_role = 'coder'
                task = self._change(task, status=status, expected_role=next_role,
                                    active_step=None, history=(*task.history, result),
                                    pending_generation_tokens=0,
                                    reason=reason, updated_at=time.time())
                self._save(db, task)
        if error:
            raise TaskError(error)
        return task

    def interrupt(self, task_id: str, reason: str, *, cancelled: bool = False) -> Task:
        """Explicitly record a role failure or user cancellation, without retrying."""
        if not isinstance(cancelled, bool):
            raise TaskError('Cancellation flag must be boolean.')
        reason = TaskRequest(summary=reason).summary
        error = None
        with self._connection() as db:
            task = self._expire(db, self._read(db, task_id))
            if task.status in TERMINAL:
                error = 'Terminal tasks cannot be reset or overwritten.'
            else:
                task = self._change(task, status='cancelled' if cancelled else 'needs_user_input',
                                    recovery_ready=False,
                                    active_step=None, reason=redact(reason), updated_at=time.time())
                self._save(db, task)
        if error:
            raise TaskError(error)
        return task

    def resume(self, task_id, feedback):
        feedback = TaskRequest(summary=feedback).summary
        with self._connection() as db:
            task = self._read(db, task_id)
            if (task.status != 'needs_user_input' or task.steps_started != len(task.history) + 1
                    or task.transport_retries >= 2):
                raise TaskError('Only an interrupted admitted step with remaining retry budget can resume.')
            if time.time() >= task.deadline:
                raise TaskError('Original task deadline passed; resume cannot extend it.')
            task = self._change(task, status='ready', recovery_ready=True, operator_feedback=feedback,
                transport_retries=task.transport_retries + 1,
                failure_notes=(*task.failure_notes, task.reason), reason='', updated_at=time.time())
            self._save(db, task)
        return task
