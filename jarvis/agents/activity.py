"""Bounded run observations. No worker execution, scheduling or termination authority."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import threading
from typing import Literal
from uuid import uuid4

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jarvis.core.guardrails import GuardrailError
from jarvis.core.roster import load_roster


class ActivityError(ValueError):
    pass


class ActivityEvent(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    run_id: str = Field(pattern='^[a-f0-9]{32}$')
    epoch: int = Field(ge=1)
    seq: int = Field(ge=1, le=1_000_000)
    kind: Literal['start', 'heartbeat', 'progress', 'result_received', 'completed', 'failed']
    phase: Literal['queued', 'working', 'review', 'summarizing', 'finished']
    reason_code: Literal['none', 'timeout', 'cancelled', 'provider_unavailable', 'unknown'] = 'none'
    result_ref: str | None = Field(default=None, pattern='^[a-f0-9]{32}$')

    @model_validator(mode='after')
    def terminal_phase(self):
        if self.kind in ('completed', 'failed') and self.phase != 'finished':
            raise ValueError('Terminal reports require finished phase.')
        return self


class ActivityStore:
    DIRECTORY = '.jarvis-activity'
    MAX_RUNS = 200
    MAX_EVENTS = 512
    MAX_BYTES = 16 * 1024 * 1024
    MIN_HEARTBEAT = 5
    HEARTBEAT_DELAY = 30

    def __init__(self, guard, *, clock=time.time, monotonic=time.monotonic, lookup=psutil.Process):
        self.guard, self.clock, self.monotonic, self.lookup = guard, clock, monotonic, lookup
        self.generation = uuid4().hex
        self.prefixes = load_roster(workspace=guard.workspace).board_prefixes()
        guard.check_running()
        guard.validate_path(self.DIRECTORY, must_exist=False).mkdir(exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS slots(owner TEXT,task_id TEXT,epoch INTEGER NOT NULL,'
                       'PRIMARY KEY(owner,task_id))')
            db.execute('CREATE TABLE IF NOT EXISTS runs('
                       'id TEXT PRIMARY KEY,owner TEXT NOT NULL,task_id TEXT NOT NULL,epoch INTEGER NOT NULL,'
                       'token_hash TEXT NOT NULL,pid INTEGER NOT NULL,process_created REAL NOT NULL,'
                       'registered_at REAL NOT NULL,last_seq INTEGER NOT NULL DEFAULT 0,'
                       'received_at REAL,received_mono REAL,generation TEXT,heartbeat_mono REAL,terminal_seq INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS events('
                       'event_id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,seq INTEGER,'
                       'payload TEXT NOT NULL,digest TEXT NOT NULL,received_at REAL NOT NULL,UNIQUE(run_id,seq))')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
            if 'terminal_seq' not in columns:
                db.execute('ALTER TABLE runs ADD COLUMN terminal_seq INTEGER')
                # Preserve old facts and repair existing out-of-order terminal reports.
                for row in db.execute('SELECT run_id,seq,payload FROM events ORDER BY seq'):
                    if json.loads(row['payload'])['kind'] in ('completed', 'failed'):
                        db.execute('UPDATE runs SET terminal_seq=? WHERE id=? '
                                   'AND (terminal_seq IS NULL OR terminal_seq>?)',
                                   (row['seq'], row['run_id'], row['seq']))
            # Nullable lineage. Runs registered before it keep NULL (unknown); nothing is inferred.
            for column in ('parent_run_id', 'step_id', 'retry_of', 'lineage'):
                if column not in columns:
                    db.execute('ALTER TABLE runs ADD COLUMN ' + column + ' TEXT')

    @contextmanager
    def connection(self, *, write=True):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY)
        path = self.guard.validate_path(str(directory / 'activity.sqlite3'), must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = self.guard.validate_path(str(directory / (path.name + suffix)), must_exist=False)
            if sibling.exists() and (not sibling.is_file() or sibling.stat().st_nlink != 1
                                      or sibling.stat().st_size > self.MAX_BYTES):
                raise GuardrailError('Unsafe activity storage.')
        db = None
        try:
            target = str(path) if write else path.as_uri() + '?mode=ro'
            db = sqlite3.connect(target, uri=not write, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            if write:
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                db.execute('PRAGMA max_page_count=' + str(self.MAX_BYTES // page_size))
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
            yield db
            self.guard.check_running()
            db.commit()
        except sqlite3.Error:
            raise ActivityError('Activity storage unavailable or capacity reached; nothing was removed.') from None
        finally:
            if db is not None:
                db.close()

    def _times(self):
        wall, mono = self.clock(), self.monotonic()
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
               for v in (wall, mono)) or wall <= 0 or mono < 0:
            raise ActivityError('Invalid collector clock.')
        return float(wall), float(mono)

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
            raise ActivityError('Invalid activity identifier.')
        return value

    def register(self, owner, task_id, pid=None, *, parent_run_id=None, step_id=None, retry_of=None):
        """Trusted local launcher only. Never expose registration as a public HTTP route."""
        self.guard.check_running()
        self._id(task_id)
        for link in (parent_run_id, retry_of):
            if link is not None and (type(link) is not str or not re.fullmatch('[a-f0-9]{32}', link)):
                raise ActivityError('Invalid activity lineage.')
        if step_id is not None and (type(step_id) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,48}', step_id)):
            raise ActivityError('Invalid activity step.')
        if (not isinstance(owner, str) or not re.fullmatch('[a-z][a-z0-9-]{1,31}:[A-Za-z0-9._-]{1,48}', owner)
                or owner.split(':', 1)[0] not in self.prefixes or owner.startswith('user:')):
            raise ActivityError('Invalid activity owner.')
        pid = os.getpid() if pid is None else pid
        if type(pid) is not int or pid <= 0:
            raise ActivityError('Invalid process identifier.')
        try:
            process = self.lookup(pid)
            created = float(process.create_time())
            parent = process.ppid()
            if (not math.isfinite(created) or created <= 0 or not process.is_running()
                    or pid != os.getpid() and parent != os.getpid()
                    or process.create_time() != created or process.ppid() != parent):
                raise ActivityError('Only this caller or its live direct child may be registered.')
        except (psutil.Error, OSError):
            raise ActivityError('Owned process identity could not be verified.') from None
        now, _ = self._times()
        identity, token = uuid4().hex, secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self.connection() as db:
            if db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] >= self.MAX_RUNS:
                raise ActivityError('Run capacity reached; nothing was removed.')
            # Links only point back to existing runs of this task, so cycles cannot form.
            if parent_run_id is not None and db.execute('SELECT 1 FROM runs WHERE id=? AND task_id=?',
                                                        (parent_run_id, task_id)).fetchone() is None:
                raise ActivityError('Parent run not found in this task.')
            if retry_of is not None:
                prior = db.execute('SELECT * FROM runs WHERE id=? AND task_id=?', (retry_of, task_id)).fetchone()
                if (prior is None or prior['lineage'] != 'registered' or prior['owner'] != owner
                        or prior['step_id'] != step_id or prior['parent_run_id'] != parent_run_id):
                    raise ActivityError('Retry must match an existing run of the same task, owner, step and parent.')
            previous = db.execute('SELECT epoch FROM slots WHERE owner=? AND task_id=?', (owner, task_id)).fetchone()
            epoch = previous['epoch'] + 1 if previous else 1
            db.execute('INSERT INTO slots VALUES(?,?,?) ON CONFLICT(owner,task_id) DO UPDATE SET epoch=excluded.epoch',
                       (owner, task_id, epoch))
            db.execute('INSERT INTO runs(id,owner,task_id,epoch,token_hash,pid,process_created,registered_at,'
                       'parent_run_id,step_id,retry_of,lineage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, owner, task_id, epoch, digest, pid, created, now,
                        parent_run_id, step_id, retry_of, 'registered'))
        return {'run_id': identity, 'epoch': epoch, 'token': token}

    def ingest(self, token, event):
        self.guard.check_running()
        try:
            if not isinstance(event, dict) or len(json.dumps(event, ensure_ascii=False).encode('utf-8')) > 1024:
                raise ActivityError('Invalid activity event.')
            parsed = ActivityEvent.model_validate(event)
        except (ValidationError, TypeError, ValueError, OverflowError, RecursionError):
            raise ActivityError('Invalid activity event.') from None
        if not isinstance(token, str) or not re.fullmatch('[A-Za-z0-9_-]{43}', token):
            raise ActivityError('Activity authorization failed.')
        payload = json.dumps(parsed.model_dump(), sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        now, mono = self._times()
        with self.connection() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (parsed.run_id,)).fetchone()
            if run is None or not secrets.compare_digest(run['token_hash'], hashlib.sha256(token.encode('ascii')).hexdigest()):
                raise ActivityError('Activity authorization failed.')
            epoch = db.execute('SELECT epoch FROM slots WHERE owner=? AND task_id=?', (run['owner'], run['task_id'])).fetchone()[0]
            if parsed.epoch != run['epoch'] or parsed.epoch != epoch:
                raise ActivityError('Activity run has been fenced.')
            if run['generation'] == self.generation and run['received_mono'] is not None and mono < run['received_mono']:
                raise ActivityError('Collector monotonic clock regressed.')
            previous = db.execute('SELECT digest FROM events WHERE run_id=? AND seq=?', (parsed.run_id, parsed.seq)).fetchone()
            if previous:
                if not secrets.compare_digest(previous['digest'], digest):
                    raise ActivityError('Sequence already has different content.')
                status = 'duplicate'
            elif run['terminal_seq'] is not None and parsed.seq > run['terminal_seq']:
                raise ActivityError('Run already has a terminal report; register a new run.')
            elif (parsed.kind == 'heartbeat' and run['generation'] == self.generation
                    and run['heartbeat_mono'] is not None and 0 <= mono - run['heartbeat_mono'] < self.MIN_HEARTBEAT):
                status = 'rate_limited'
            else:
                if db.execute('SELECT COUNT(*) FROM events WHERE run_id=?', (parsed.run_id,)).fetchone()[0] >= self.MAX_EVENTS:
                    raise ActivityError('Run event capacity reached; nothing was removed.')
                db.execute('INSERT INTO events(run_id,seq,payload,digest,received_at) VALUES(?,?,?,?,?)',
                           (parsed.run_id, parsed.seq, payload, digest, now))
                # Out-of-order transitions remain durable, but cannot overwrite the latest state.
                status = 'accepted' if parsed.seq > run['last_seq'] else 'stale_sequence'
                heartbeat = mono if parsed.kind == 'heartbeat' else (
                    run['heartbeat_mono'] if run['generation'] == self.generation else None)
                db.execute('UPDATE runs SET last_seq=?,received_at=?,received_mono=?,generation=?,heartbeat_mono=? WHERE id=?',
                           (max(parsed.seq, run['last_seq']), now, mono, self.generation, heartbeat, parsed.run_id))
                if parsed.kind in ('completed', 'failed'):
                    db.execute('UPDATE runs SET terminal_seq=? WHERE id=? '
                               'AND (terminal_seq IS NULL OR terminal_seq>?)',
                               (parsed.seq, parsed.run_id, parsed.seq))
        return {'status': status, 'run_id': parsed.run_id, 'seq': parsed.seq}

    def _probe(self, row):
        try:
            process = self.lookup(row['pid'])
            if process.create_time() != row['process_created']:
                return 'identity_mismatch'
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return 'exited'
            return 'running' if process.create_time() == row['process_created'] else 'identity_mismatch'
        except psutil.NoSuchProcess:
            return 'exited'
        except (psutil.Error, OSError):
            return 'unknown'

    def _project(self, db, row, mono):
        self.guard.check_running()
        report_seq = row['terminal_seq'] if row['terminal_seq'] is not None else row['last_seq']
        latest = db.execute('SELECT payload FROM events WHERE run_id=? AND seq=?', (row['id'], report_seq)).fetchone()
        count = db.execute('SELECT COUNT(*) FROM events WHERE run_id=?', (row['id'],)).fetchone()[0]
        heartbeat, reason = 'unknown', 'no_report'
        if row['generation'] is not None and row['generation'] != self.generation:
            reason = 'collector_restarted'
        elif row['received_mono'] is not None:
            elapsed = mono - row['received_mono']
            if elapsed < 0:
                reason = 'clock_regressed'
            else:
                heartbeat, reason = ('recent' if elapsed <= self.HEARTBEAT_DELAY else 'delayed'), 'received_event'
        return {'run_id': row['id'], 'owner': row['owner'], 'task_id': row['task_id'],
            'epoch': row['epoch'], 'fenced': row['epoch'] != row['current_epoch'],
            'last_seq': row['last_seq'], 'sequence_gap': count != row['last_seq'],
            'terminal_seq': row['terminal_seq'],
            'report': {'source': 'self_reported', 'event': json.loads(latest['payload']) if latest else None},
            'heartbeat': {'source': 'observed', 'status': heartbeat, 'reason': reason, 'received_at': row['received_at']},
            'process': {'source': 'observed', 'status': self._probe(row), 'pid': row['pid']},
            'lineage': {'source': 'registered' if row['lineage'] == 'registered' else 'unknown',
                        'parent_run_id': row['parent_run_id'], 'step_id': row['step_id'], 'retry_of': row['retry_of']},
            'verified': []}

    def inspect(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= self.MAX_RUNS:
            raise ActivityError('Invalid activity limit.')
        now, mono = self._times()
        with self.connection(write=False) as db:
            rows = db.execute('SELECT r.*,s.epoch AS current_epoch FROM runs r JOIN slots s '
                              'ON r.owner=s.owner AND r.task_id=s.task_id ORDER BY registered_at DESC,id LIMIT ?', (limit + 1,)).fetchall()
            runs = [self._project(db, row, mono) for row in rows[:limit]]
        return {'observed_at': now, 'runs': runs, 'has_more': len(rows) > limit,
                'control_supported': False, 'generation': self.generation}

    def trace(self, task_id):
        """Read-only lineage of one task from stored self-reports. Not verification or Board completion."""
        self._id(task_id)
        now, mono = self._times()
        runs, links, budget, truncated = [], [], self.MAX_EVENTS, False
        with self.connection(write=False) as db:
            rows = db.execute('SELECT r.*,s.epoch AS current_epoch FROM runs r JOIN slots s '
                              'ON r.owner=s.owner AND r.task_id=s.task_id WHERE r.task_id=? '
                              'ORDER BY r.registered_at,r.id LIMIT ?', (task_id, self.MAX_RUNS + 1)).fetchall()
            for row in rows[:self.MAX_RUNS]:
                run = self._project(db, row, mono)
                stored = db.execute('SELECT event_id,payload,received_at FROM events WHERE run_id=? ORDER BY seq,event_id LIMIT ?',
                                    (row['id'], self.MAX_EVENTS + 1)).fetchall()
                events = []
                for item in stored[:self.MAX_EVENTS]:
                    payload = json.loads(item['payload'])
                    events.append({'cursor': item['event_id'], 'source': 'self_reported', 'received_at': item['received_at'],
                                   **{key: payload[key] for key in ('seq', 'kind', 'phase', 'reason_code', 'result_ref')}})
                events = events[:budget]
                start = next((e['seq'] for e in events if e['kind'] == 'start'), None)
                end = run['report']['event'] if row['terminal_seq'] is not None else None
                refs = {}
                for item in events:
                    if item['result_ref'] is not None:
                        refs.setdefault(item['result_ref'], item['seq'])
                run.update({
                    'start': {'source': 'self_reported', 'seq': start} if start else {'source': 'unknown', 'seq': None},
                    'end': ({'source': 'self_reported', 'kind': end['kind'], 'seq': row['terminal_seq']} if end
                            else {'source': 'unknown', 'kind': None, 'seq': None}),
                    'result_refs': [{'source': 'self_reported', 'ref': ref, 'seq': seq} for ref, seq in refs.items()],
                    'events': events[:budget], 'events_has_more': len(stored) > budget,
                    # Record completeness only; a completed self-report with gaps stays incomplete, never proof.
                    'trace_complete': bool(start and end and not run['sequence_gap'] and len(stored) <= budget)})
                budget -= len(run['events'])
                truncated = truncated or run['events_has_more']
                for kind, target in (('parent', row['parent_run_id']), ('retry_of', row['retry_of'])):
                    if target is not None:
                        links.append({'run_id': row['id'], 'type': kind, 'target': target, 'step_id': row['step_id']})
                runs.append(run)
        listed = {run['run_id'] for run in runs}
        for link in links:
            link['in_trace'] = link['target'] in listed
        has_more = len(rows) > self.MAX_RUNS or truncated
        return {'task_id': task_id, 'observed_at': now, 'status': 'observed' if runs else 'unknown',
                'runs': runs, 'links': links, 'has_more': has_more,
                'trace_complete': bool(runs) and not has_more and all(run['trace_complete'] for run in runs),
                'verified': [], 'control_supported': False, 'generation': self.generation}

    def history(self, run_id, *, after=0, limit=100):
        self._id(run_id)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ActivityError('Invalid activity history range.')
        with self.connection(write=False) as db:
            if db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone() is None:
                raise ActivityError('Activity run not found.')
            rows = db.execute('SELECT event_id,payload,received_at FROM events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?',
                              (run_id, after, limit + 1)).fetchall()
        return {'run_id': run_id, 'events': [{'cursor': r['event_id'], 'source': 'self_reported', 'event': json.loads(r['payload']),
                                            'received_at': r['received_at']} for r in rows[:limit]],
                'has_more': len(rows) > limit}


class ActivityReporter:
    """Optional local-launcher heartbeat; never invokes models or controls workers."""
    def __init__(self, store, registration, *, interval=10):
        if type(interval) is not int or not 5 <= interval <= 30:
            raise ActivityError('Heartbeat interval must be 5-30 seconds.')
        self.store, self.registration, self.interval = store, dict(registration), interval
        self.seq, self.phase, self.rejected = 0, 'working', 0
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread = None

    def send(self, kind, *, phase=None, reason_code='none', result_ref=None):
        with self._lock:
            if kind == 'heartbeat' and self._stop.is_set():
                return False
            self.seq += 1
            try:
                event = {'run_id': self.registration['run_id'], 'epoch': self.registration['epoch'],
                         'seq': self.seq, 'kind': kind, 'phase': phase or self.phase,
                         'reason_code': reason_code, 'result_ref': result_ref}
                result = self.store.ingest(self.registration['token'], event)
                if result['status'] not in ('accepted', 'stale_sequence', 'duplicate'):
                    self.rejected += 1
                    return False
                if phase:
                    self.phase = phase
                if kind in ('completed', 'failed'):
                    self._stop.set()
                return True
            except (ActivityError, GuardrailError, OSError):
                # Failure is visible in a bounded counter, never a new model call.
                self.rejected = min(self.rejected + 1, 1_000_000)
                return False

    def start(self):
        if self._thread is not None:
            raise ActivityError('Reporter already started.')
        self.send('start')
        def heartbeat():
            while not self._stop.wait(self.interval):
                self.send('heartbeat')
        self._thread = threading.Thread(target=heartbeat, name='jarvis-activity-heartbeat', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        # Observer closure does not prove the worker exited or completed its task.
