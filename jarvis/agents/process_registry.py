"""Owned-process observations only. No termination, command lines or process discovery."""
from contextlib import contextmanager
import math
import os
import sqlite3
import time
from typing import Literal
from uuid import uuid4

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.core.guardrails import GuardrailError
from jarvis.core.redaction import redact


class ProcessRegistryError(ValueError):
    pass


class ProcessRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    id: str = Field(pattern='^[a-f0-9]{32}$')
    pid: int = Field(gt=0)
    created: float = Field(gt=0)
    parent_pid: int = Field(ge=0)
    parent_created: float | None = Field(default=None, gt=0)
    kind: Literal['hud', 'verification', 'worker', 'service']
    label: str = Field(min_length=1, max_length=120)
    registered_at: float = Field(gt=0)
    max_runtime_seconds: int = Field(ge=1, le=604800)


class ProcessRegistry:
    DIRECTORY = '.jarvis-processes'
    MAX_RECORDS = 1000
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, guard, *, clock=time.time, lookup=psutil.Process):
        self.guard, self.clock, self.lookup = guard, clock, lookup
        guard.check_running()
        guard.validate_path(self.DIRECTORY, must_exist=False).mkdir(exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS processes ('
                'id TEXT PRIMARY KEY,pid INTEGER NOT NULL,created REAL NOT NULL,'
                'parent_pid INTEGER NOT NULL,parent_created REAL,kind TEXT NOT NULL,'
                'label TEXT NOT NULL,registered_at REAL NOT NULL,max_runtime_seconds INTEGER NOT NULL,'
                'UNIQUE(pid,created))')

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY)
        path = self.guard.validate_path(str(directory / 'processes.sqlite3'), must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = self.guard.validate_path(str(directory / (path.name + suffix)), must_exist=False)
            if sibling.exists() and (not sibling.is_file() or sibling.stat().st_nlink != 1
                                      or sibling.stat().st_size > self.MAX_BYTES):
                raise GuardrailError('Unsafe process registry storage.')
        db = None
        try:
            db = sqlite3.connect(path, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            yield db
            self.guard.check_running()
            db.commit()
        except sqlite3.Error as exc:
            raise ProcessRegistryError('Process registry storage unavailable.') from exc
        finally:
            if db is not None:
                db.close()

    def _time(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ProcessRegistryError('Invalid observation clock.')
        return float(value)

    def register_current(self, *, kind, label, max_runtime_seconds):
        return self._register(os.getpid(), kind, label, max_runtime_seconds, child=False)

    def register_child(self, pid, *, kind, label, max_runtime_seconds):
        return self._register(pid, kind, label, max_runtime_seconds, child=True)

    def _register(self, pid, kind, label, duration, *, child):
        self.guard.check_running()
        if type(pid) is not int or pid <= 0:
            raise ProcessRegistryError('Invalid process identifier.')
        if not isinstance(label, str) or not label.strip() or redact(label) != label or any(ord(c) < 32 for c in label):
            raise ProcessRegistryError('Invalid process label.')
        try:
            process = self.lookup(pid)
            created, parent_pid = float(process.create_time()), process.ppid()
            if not process.is_running():
                raise ProcessRegistryError('Process is no longer running.')
            if child and (pid == os.getpid() or parent_pid != os.getpid()):
                raise ProcessRegistryError('Only a direct child of this caller may be registered.')
            try:
                parent_created = float(self.lookup(parent_pid).create_time()) if parent_pid else None
            except (psutil.Error, OSError):
                if child:
                    raise ProcessRegistryError('Cannot verify the owning parent.')
                parent_created = None
            if process.create_time() != created or process.ppid() != parent_pid or not process.is_running():
                raise ProcessRegistryError('Process identity changed during registration.')
            record = ProcessRecord(id=uuid4().hex, pid=pid, created=created, parent_pid=parent_pid,
                parent_created=parent_created, kind=kind, label=label, registered_at=self._time(),
                max_runtime_seconds=duration)
        except (psutil.Error, OSError, ValidationError) as exc:
            raise ProcessRegistryError('Process registration could not be verified.') from exc
        with self.connection() as db:
            previous = db.execute('SELECT * FROM processes WHERE pid=? AND created=?', (pid, created)).fetchone()
            if previous:
                saved = self._record(previous)
                if any(saved[key] != record.model_dump()[key] for key in ('kind', 'label', 'max_runtime_seconds')):
                    raise ProcessRegistryError('Existing registration differs; its time limit cannot be reset.')
                return saved
            if db.execute('SELECT count(*) FROM processes').fetchone()[0] >= self.MAX_RECORDS:
                raise ProcessRegistryError('Process registry capacity reached; nothing was removed.')
            db.execute('INSERT INTO processes VALUES(?,?,?,?,?,?,?,?,?)', tuple(record.model_dump().values()))
        return record.model_dump()

    @staticmethod
    def _record(row):
        try:
            return ProcessRecord.model_validate(dict(row)).model_dump()
        except ValidationError as exc:
            raise ProcessRegistryError('Invalid process registry record.') from exc

    def _probe(self, pid, created):
        if not pid or created is None:
            return 'unverifiable'
        try:
            process = self.lookup(pid)
            if process.create_time() != created:
                return 'pid_reused'
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return 'exited'
            return 'running' if process.create_time() == created else 'pid_reused'
        except psutil.NoSuchProcess:
            return 'exited'
        except (psutil.Error, OSError):
            return 'unverifiable'

    def inspect(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= self.MAX_RECORDS:
            raise ProcessRegistryError('Limit must be between 1 and 1000.')
        now = self._time()
        with self.connection() as db:
            rows = db.execute('SELECT * FROM processes ORDER BY registered_at DESC,id LIMIT ?',
                              (self.MAX_RECORDS+1,)).fetchall()
        if len(rows) > self.MAX_RECORDS:
            raise ProcessRegistryError('Process registry capacity exceeded.')
        observations = []
        for row in rows:
            self.guard.check_running()
            record = self._record(row)
            state = self._probe(record['pid'], record['created'])
            parent = self._probe(record['parent_pid'], record['parent_created'])
            warnings = []
            if state == 'running':
                if parent in ('exited', 'pid_reused'):
                    warnings.append('parent_exited')
                elif record['parent_pid'] and parent == 'unverifiable':
                    warnings.append('parent_unverifiable')
                elapsed = now - record['created']
                if elapsed < 0:
                    warnings.append('clock_regressed')
                elif elapsed >= record['max_runtime_seconds']:
                    warnings.append('runtime_exceeded')
            elif state in ('unverifiable', 'pid_reused'):
                warnings.append('process_' + state)
            observations.append({**record, 'process_state': state, 'parent_state': parent,
                                 'warnings': warnings, 'observed_at': now})
        observations.sort(key=lambda item: (not bool(item['warnings']), item['process_state'] != 'running',
                                            -item['registered_at'], item['id']))
        return {'processes': observations[:limit], 'has_more': len(rows) > limit, 'scanned_count': len(rows),
                'warning_count': sum(bool(item['warnings']) for item in observations),
                'termination_supported': False, 'scope': 'registered_process_observations_only'}
