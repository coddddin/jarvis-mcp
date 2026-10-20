"""Scoped, expiring operator-confirmed preferences, never executable authority."""
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from jarvis.agents.models import Contract
from jarvis.core.redaction import redact


class MemoryError(ValueError):
    """Memory storage, scope, content or revision was rejected."""


class MemoryScope(Contract):
    owner: str = Field(default='local', pattern=r'^[A-Za-z0-9_-]{1,64}$')
    project: str = Field(pattern=r'^projects/[A-Za-z0-9_-]{1,80}$')


class MemoryInput(MemoryScope):
    kind: Literal['preference', 'path', 'error_solution']
    key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    value: str = Field(min_length=1, max_length=1000)
    source: str = Field(default='local_operator', min_length=1, max_length=100)
    retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator('value', 'source')
    @classmethod
    def safe_content(cls, value):
        if not value.strip() or redact(value) != value:
            raise ValueError('Empty or recognized sensitive memory is refused.')
        return value


class MemoryRecord(MemoryInput):
    memory_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')
    confirmed: bool = False
    created_at: float
    updated_at: float
    expires_at: float
    untrusted_data: Literal[True] = True


class MemoryStore:
    MAX_RECORDS = 1000

    def __init__(self, tasks):
        self.tasks = tasks
        with tasks._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, owner TEXT NOT NULL, '
                       'project TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, expires REAL NOT NULL, '
                       'record TEXT NOT NULL, UNIQUE(owner, project, kind, key))')

    @staticmethod
    def _purge(db):
        db.execute('DELETE FROM memories WHERE expires <= ?', (time.time(),))

    def put(self, request: MemoryInput):
        request = MemoryInput.model_validate(request.model_dump())
        now = time.time()
        with self.tasks._connection() as db:
            self._purge(db)
            row = db.execute('SELECT record FROM memories WHERE owner=? AND project=? AND kind=? AND key=?',
                             (request.owner, request.project, request.kind, request.key)).fetchone()
            old = MemoryRecord.model_validate_json(row[0]) if row else None
            if old is None and db.execute('SELECT count(*) FROM memories').fetchone()[0] >= self.MAX_RECORDS:
                raise MemoryError('Memory retention capacity reached; delete or expire old entries.')
            record = MemoryRecord(**request.model_dump(), memory_id=old.memory_id if old else uuid4().hex,
                revision=uuid4().hex, created_at=old.created_at if old else now, updated_at=now,
                expires_at=now + request.retention_days * 86400)
            # Editing always revokes confirmation, even when a key is reused.
            db.execute('INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?)',
                (record.memory_id, record.owner, record.project, record.kind, record.key, record.expires_at, record.model_dump_json()))
        return record

    def list(self, scope: MemoryScope, *, confirmed_only=False, limit=100):
        scope = MemoryScope.model_validate(scope.model_dump())
        if type(limit) is not int or not 1 <= limit <= 100 or type(confirmed_only) is not bool:
            raise MemoryError('Invalid bounded memory query.')
        with self.tasks._connection() as db:
            self._purge(db)
            rows = db.execute('SELECT record FROM memories WHERE owner=? AND project=? ORDER BY id',
                              (scope.owner, scope.project)).fetchall()
            records = [MemoryRecord.model_validate_json(row[0]) for row in rows]
            records = [r for r in records if not confirmed_only or r.confirmed]
        return tuple(records[:limit])

    def _revise(self, scope, memory_id, revision, *, delete=False):
        scope = MemoryScope.model_validate(scope.model_dump())
        with self.tasks._connection() as db:
            self._purge(db)
            row = db.execute('SELECT record FROM memories WHERE owner=? AND project=? AND id=?',
                             (scope.owner, scope.project, memory_id)).fetchone()
            if row is None:
                raise MemoryError('Memory does not exist in this owner/project scope.')
            record = MemoryRecord.model_validate_json(row[0])
            if record.revision != revision:
                raise MemoryError('Memory changed; inspect its current revision before confirming or deleting.')
            if delete:
                db.execute('DELETE FROM memories WHERE id=?', (memory_id,))
                return None
            updated = MemoryRecord.model_validate({**record.model_dump(), 'confirmed': True,
                'revision': uuid4().hex, 'updated_at': time.time()})
            db.execute('UPDATE memories SET record=? WHERE id=?', (updated.model_dump_json(), memory_id))
        return updated

    def confirm(self, scope, memory_id, revision):
        return self._revise(scope, memory_id, revision)

    def delete(self, scope, memory_id, revision):
        self._revise(scope, memory_id, revision, delete=True)
