"""Content-free platform session declarations, not process or model observations."""
from contextlib import contextmanager
import hashlib
import json
import math
import sqlite3
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.core.guardrails import GuardrailError
from jarvis.core.redaction import redact


class SessionStatusError(ValueError):
    pass


class SessionSignal(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    employee: Literal['codex', 'claude-code']
    session_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$')
    event_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    turn_id: str | None = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$')
    event: Literal['session_started', 'turn_started', 'tool_started', 'tool_finished',
                   'permission_requested', 'turn_stopped', 'interrupted', 'session_ended']
    source: Literal['self_reported']


class SessionStatusStore:
    DIRECTORY = '.jarvis-sessions'
    MAX_BYTES = 16 * 1024 * 1024
    MAX_SESSIONS = 200
    MAX_EVENTS = 20000

    def __init__(self, guard, *, clock=time.time, busy_timeout=2):
        if isinstance(busy_timeout, bool) or not isinstance(busy_timeout, (int, float)) or not math.isfinite(busy_timeout) or not 0 <= busy_timeout <= 2:
            raise ValueError("Invalid session storage timeout.")
        self.busy_timeout = busy_timeout
        self.guard, self.clock = guard, clock
        guard.check_running()
        guard.validate_path(self.DIRECTORY, must_exist=False).mkdir(exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions(employee TEXT,session_id TEXT,state TEXT,'
                'turn_id TEXT,closed INTEGER NOT NULL DEFAULT 0,received_at REAL NOT NULL,'
                'last_event TEXT NOT NULL,PRIMARY KEY(employee,session_id))')
            db.execute('CREATE TABLE IF NOT EXISTS turns(employee TEXT,session_id TEXT,turn_id TEXT,'
                'terminal TEXT,PRIMARY KEY(employee,session_id,turn_id))')
            db.execute('CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,employee TEXT,'
                'session_id TEXT,turn_id TEXT,event TEXT NOT NULL,digest TEXT NOT NULL,'
                'received_at REAL NOT NULL,applied INTEGER NOT NULL)')

    @contextmanager
    def connection(self, *, write=True):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY)
        path = self.guard.validate_path(str(directory / 'sessions.sqlite3'), must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = self.guard.validate_path(str(directory / (path.name + suffix)), must_exist=False)
            if sibling.exists() and (not sibling.is_file() or sibling.stat().st_nlink != 1
                    or sibling.stat().st_size > self.MAX_BYTES):
                raise GuardrailError('Unsafe session status storage.')
        db = None
        try:
            db = sqlite3.connect(str(path) if write else path.as_uri()+'?mode=ro',
                uri=not write, timeout=self.busy_timeout, isolation_level=None)
            db.row_factory = sqlite3.Row
            if write:
                size = db.execute('PRAGMA page_size').fetchone()[0]
                db.execute('PRAGMA max_page_count='+str(self.MAX_BYTES//size))
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
            yield db
            self.guard.check_running()
            db.commit()
        except sqlite3.Error:
            raise SessionStatusError('Session status storage unavailable or full; nothing was removed.') from None
        finally:
            if db is not None:
                db.close()

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now <= 0:
            raise SessionStatusError('Invalid receipt clock.')
        return float(now)

    def report(self, signal):
        self.guard.check_running()
        try:
            parsed = SessionSignal.model_validate(signal)
            data = parsed.model_dump()
            if any(value is not None and redact(value) != value for value in (parsed.session_id, parsed.turn_id)):
                raise ValueError('Sensitive identifier.')
        except (ValidationError, TypeError, ValueError):
            raise SessionStatusError('Invalid content-free session signal.') from None
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        now = self._now()
        key = (parsed.employee, parsed.session_id)
        with self.connection() as db:
            duplicate = db.execute('SELECT digest FROM events WHERE event_id=?', (parsed.event_id,)).fetchone()
            if duplicate:
                if duplicate['digest'] != digest:
                    raise SessionStatusError('Event ID already has different content.')
                return {'status': 'duplicate', 'applied': False}
            if db.execute('SELECT count(*) FROM events').fetchone()[0] >= self.MAX_EVENTS:
                raise SessionStatusError('Session event capacity reached; nothing was removed.')
            row = db.execute('SELECT * FROM sessions WHERE employee=? AND session_id=?', key).fetchone()
            if row is None:
                if db.execute('SELECT count(*) FROM sessions').fetchone()[0] >= self.MAX_SESSIONS:
                    raise SessionStatusError('Session capacity reached; nothing was removed.')
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', (*key, 'unknown', None, 0, now, parsed.event))
                row = db.execute('SELECT * FROM sessions WHERE employee=? AND session_id=?', key).fetchone()
            state, turn, closed = row['state'], row['turn_id'], row['closed']
            applied = False
            if parsed.event == 'session_started':
                if closed or turn is None:
                    state, turn, closed, applied = 'started', None, 0, True
            elif parsed.event == 'session_ended':
                if turn:
                    db.execute('UPDATE turns SET terminal=COALESCE(terminal,?) WHERE employee=? AND session_id=? AND turn_id=?',
                        ('session_ended', *key, turn))
                state, closed, applied = 'ended', 1, True
            elif not closed and parsed.turn_id is not None:
                turn_row = db.execute('SELECT terminal FROM turns WHERE employee=? AND session_id=? AND turn_id=?',
                    (*key, parsed.turn_id)).fetchone()
                if parsed.event in ('turn_stopped', 'interrupted'):
                    db.execute('INSERT INTO turns VALUES(?,?,?,?) ON CONFLICT(employee,session_id,turn_id) '
                        'DO UPDATE SET terminal=COALESCE(turns.terminal,excluded.terminal)', (*key, parsed.turn_id, parsed.event))
                    if not (turn_row and turn_row['terminal']) and (turn == parsed.turn_id or (turn is None and turn_row is None)):
                        state, turn, applied = parsed.event, parsed.turn_id, True
                elif not (turn_row and turn_row['terminal']):
                    if parsed.event == 'turn_started' and turn_row is None:
                        if turn and turn != parsed.turn_id:
                            db.execute('UPDATE turns SET terminal=COALESCE(terminal,?) WHERE employee=? AND session_id=? AND turn_id=?',
                                ('superseded', *key, turn))
                        db.execute('INSERT OR IGNORE INTO turns VALUES(?,?,?,NULL)', (*key, parsed.turn_id))
                        state, turn, applied = 'turn_started', parsed.turn_id, True
                    elif parsed.event != 'turn_started' and turn == parsed.turn_id and turn_row:
                        state, applied = parsed.event, True
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)', (parsed.event_id, *key, parsed.turn_id,
                parsed.event, digest, now, int(applied)))
            # Receipt is an observed fact even when a late declaration cannot change state.
            if applied:
                db.execute('UPDATE sessions SET state=?,turn_id=?,closed=?,received_at=?,last_event=? WHERE employee=? AND session_id=?',
                    (state, turn, closed, now, parsed.event, *key))
            return {'status': 'accepted' if applied else 'ignored', 'applied': applied}

    def inspect(self):
        now = self._now()
        with self.connection(write=False) as db:
            rows = db.execute('SELECT * FROM sessions ORDER BY received_at DESC,session_id LIMIT ?', (self.MAX_SESSIONS,)).fetchall()
            event_count = db.execute('SELECT count(*) FROM events').fetchone()[0]
            session_count = db.execute('SELECT count(*) FROM sessions').fetchone()[0]
            capacity = {
                'events': {'used': event_count, 'limit': self.MAX_EVENTS, 'reached': event_count >= self.MAX_EVENTS},
                'sessions': {'used': session_count, 'limit': self.MAX_SESSIONS, 'reached': session_count >= self.MAX_SESSIONS},
                'automatic_deletion': False,
            }
            sessions = []
            for row in rows:
                sessions.append({'employee': row['employee'], 'session_id': row['session_id'], 'turn_id': row['turn_id'],
                    'state': row['state'], 'last_event': row['last_event'], 'source': 'self_reported',
                    'receipt': {'source': 'observed', 'received_at': row['received_at'],
                        'status': 'clock_unknown' if now < row['received_at'] else 'recent' if now-row['received_at'] <= 60 else 'old'},
                    'connection_status': 'unknown', 'verified': []})
        return {'schema_version': 1, 'sessions': sessions, 'observed_at': now, 'source': 'self_reported',
            'read_only': True, 'control_supported': False, 'identity_verified': False, 'capacity': capacity}
