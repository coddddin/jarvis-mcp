"""Shared task board: who owns which work and files, with leases and handoff notes.

Agent IDs are self-declared by MCP clients, not authenticated. Path claims are
advisory coordination, not OS locks. Board text is data, never authorization.
"""
from contextlib import contextmanager
import json
import re
import sqlite3
import time
import unicodedata
from uuid import uuid4

from jarvis.core.guardrails import GuardrailError
from jarvis.core.redaction import redact
from jarvis.core.roster import RosterError, load_roster


class BoardError(ValueError):
    """A board request was refused."""


AGENT = re.compile(r'([a-z][a-z0-9-]{1,31}):[A-Za-z0-9._-]{1,48}')
TASK_ID = re.compile(r'[a-f0-9]{32}')
COMMIT = re.compile(r'[0-9a-f]{7,40}')
SEGMENT = re.compile(r'[A-Za-z0-9._@+-]+')
PROTECTED = ('JarvisWorkspace', '.jarvis-auth', '.venv', '.git')
STATUSES = ('open', 'claimed', 'blocked', 'review', 'done', 'cancelled')
RELEASE_TO = ('open', 'blocked', 'review', 'done', 'cancelled')
NOTE_KINDS = ('handoff', 'finding', 'question')
MAX_TASKS, MAX_NOTES, MAX_PATHS, MAX_MESSAGES = 2000, 200, 20, 5000
MAX_EVENTS = 20000


def _text(value, name, limit, *, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise BoardError(f'{name} must be {"1-" if required else "0-"}{limit} characters.')
    if redact(value) != value:
        raise BoardError(f'Remove credentials or control characters from {name}.')
    return value.strip()


def _task_id(value):
    if not isinstance(value, str) or not TASK_ID.fullmatch(value):
        raise BoardError('Invalid task ID.')
    return value


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise BoardError('Each path must be 1-200 characters.')
    if redact(value) != value:
        raise BoardError('Paths cannot contain control characters or credentials.')
    path = value.replace('\\', '/')
    directory = path.endswith('/')
    parts = [part for part in path.split('/') if part]
    if (path.startswith('/') or ':' in path or not parts
            or any(part in ('.', '..') or not SEGMENT.fullmatch(part) for part in parts)):
        raise BoardError(f'Use a project-relative path without "..": {value!r}')
    if parts[0] in PROTECTED:
        raise BoardError(f'Data, credential and environment folders cannot be claimed: {parts[0]}')
    return '/'.join(parts) + ('/' if directory else '')


def _paths(values):
    if not isinstance(values, list) or len(values) > MAX_PATHS:
        raise BoardError(f'paths must be a list of at most {MAX_PATHS} entries.')
    return sorted(dict.fromkeys(_path(value) for value in values))


def overlaps(a, b):
    """True when one path equals or contains the other at a segment boundary."""
    left, right = a.rstrip('/').split('/'), b.rstrip('/').split('/')
    shorter = min(len(left), len(right))
    return left[:shorter] == right[:shorter]


class BoardStore:
    DIRECTORY = '.jarvis-board'
    DATABASE = '.jarvis-board/board.sqlite3'

    def __init__(self, guard, *, clock=time.time, roster=None):
        self.guard, self.clock = guard, clock
        self._roster = roster
        self._ready = False

    def _prefixes(self):
        if self._roster is None:
            try:
                self._roster = load_roster(workspace=self.guard.workspace)
            except RosterError as exc:
                raise BoardError(f'Agent roster unavailable: {exc}') from None
        return self._roster.board_prefixes()

    def _agent(self, value):
        match = AGENT.fullmatch(value) if isinstance(value, str) else None
        if match is None or match.group(1) not in self._prefixes():
            raise BoardError('Invalid agent ID; use <employee>:<session> with an employee from config/agents.json '
                             '(or user:/local:).')
        return value

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY, must_exist=False)
        if directory.is_symlink() or directory.is_junction():
            raise GuardrailError('Linked board storage is forbidden.')
        directory.mkdir(exist_ok=True)
        path = self.guard.validate_path(self.DATABASE, must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = directory / (path.name + suffix)
            if sibling.exists() and (not sibling.is_file() or sibling.is_symlink() or sibling.stat().st_nlink != 1):
                raise GuardrailError('Linked board storage is forbidden.')
        db = None
        try:
            db = sqlite3.connect(path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            if not self._ready:
                self._schema(db)
            yield db
            self.guard.check_running()
            db.commit()
            self._ready = True
        except sqlite3.Error as exc:
            raise BoardError('Board storage unavailable.') from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL, '
                   'status TEXT NOT NULL, creator TEXT NOT NULL, owner TEXT, lease_until REAL, paths TEXT NOT NULL, '
                   'depends_on TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, '
                   'author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, refs TEXT NOT NULL, created REAL NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS notes_task ON notes(task_id, id)')
        db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, '
                   'recipient TEXT NOT NULL, task_id TEXT, body TEXT NOT NULL, created REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS reads (message_id INTEGER NOT NULL, reader TEXT NOT NULL, '
                   'PRIMARY KEY(message_id, reader))')
        db.execute('CREATE INDEX IF NOT EXISTS messages_recipient ON messages(recipient, id)')
        # Status history: states and agent IDs only, never summaries or note text.
        db.execute('CREATE TABLE IF NOT EXISTS status_events (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                   'task_id TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL, agent TEXT NOT NULL, '
                   'at REAL NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS status_events_task ON status_events(task_id, id)')
        db.execute('CREATE TRIGGER IF NOT EXISTS status_events_follow_tasks AFTER DELETE ON tasks '
                   'BEGIN DELETE FROM status_events WHERE task_id = old.id; END')

    def _row(self, row, now):
        task = dict(row)
        task['paths'], task['depends_on'] = json.loads(task['paths']), json.loads(task['depends_on'])
        task['stale'] = task['status'] == 'claimed' and (task['lease_until'] or 0) <= now
        return task

    def _load(self, db, task_id):
        row = db.execute('SELECT * FROM tasks WHERE id=?', (_task_id(task_id),)).fetchone()
        if row is None:
            raise BoardError('Task not found.')
        return self._row(row, self.clock())

    def _add_note(self, db, task_id, author, kind, body, refs, now):
        if db.execute('SELECT COUNT(*) FROM notes WHERE task_id=?', (task_id,)).fetchone()[0] >= MAX_NOTES:
            raise BoardError('Note limit reached for this task; create a follow-up task.')
        db.execute('INSERT INTO notes(task_id,author,kind,body,refs,created) VALUES(?,?,?,?,?,?)',
                   (task_id, author, kind, body, json.dumps(refs), now))

    @staticmethod
    def _event(db, task_id, from_status, to_status, agent, now):
        """One status transition, in the caller's transaction; the oldest rows beyond MAX_EVENTS are pruned."""
        db.execute('INSERT INTO status_events(task_id,from_status,to_status,agent,at) VALUES(?,?,?,?,?)',
                   (task_id, from_status, to_status, agent, now))
        db.execute('DELETE FROM status_events WHERE id <= '
                   '(SELECT id FROM status_events ORDER BY id DESC LIMIT 1 OFFSET ?)', (MAX_EVENTS,))

    @staticmethod
    def _lease(minutes):
        if type(minutes) is not int or not 5 <= minutes <= 240:
            raise BoardError('lease_minutes must be an integer from 5 to 240.')
        return minutes * 60

    def create(self, agent, title, detail, paths, *, depends_on=None):
        agent = self._agent(agent)
        title, detail = _text(title, 'title', 200), _text(detail, 'detail', 2000, required=False)
        paths = _paths(paths)
        depends_on = sorted(dict.fromkeys(_task_id(item) for item in (depends_on or [])))
        if len(depends_on) > 10:
            raise BoardError('At most 10 dependencies.')
        now = self.clock()
        with self.connection() as db:
            if db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] >= MAX_TASKS:
                raise BoardError('Board task limit reached; archive old tasks first.')
            for item in depends_on:
                self._load(db, item)
            identity = uuid4().hex
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, title, detail, 'open', agent, None, None, json.dumps(paths),
                        json.dumps(depends_on), now, now))
            self._event(db, identity, None, 'open', agent, now)
            return self._load(db, identity)

    def claim(self, agent, task_id, *, lease_minutes=30):
        agent, lease = self._agent(agent), self._lease(lease_minutes)
        now = self.clock()
        with self.connection() as db:
            task = self._load(db, task_id)
            renewing = task['status'] == 'claimed' and task['owner'] == agent
            if not renewing:
                if task['status'] == 'claimed' and not task['stale']:
                    raise BoardError(f'Task is claimed by {task["owner"]} until lease expiry.')
                if task['status'] not in ('open', 'blocked', 'review', 'claimed'):
                    raise BoardError(f'Task is {task["status"]}; it cannot be claimed.')
            pending = [item for item in task['depends_on']
                       if self._load(db, item)['status'] != 'done']
            if pending:
                raise BoardError('Unfinished dependencies: ' + ', '.join(pending))
            conflicts = []
            for row in db.execute('SELECT * FROM tasks WHERE status="claimed" AND lease_until>? AND id!=? AND owner!=?',
                                  (now, task['id'], agent)):
                other = self._row(row, now)
                hits = sorted({a for a in task['paths'] for b in other['paths'] if overlaps(a, b)})
                if hits:
                    conflicts.append(f'{other["owner"]} ({other["id"][:8]}): {", ".join(hits)}')
            if conflicts:
                raise BoardError('Paths overlap active claims: ' + '; '.join(conflicts))
            if task['stale'] and task['owner'] != agent:
                self._add_note(db, task['id'], agent, 'handoff',
                               f'Took over stale claim from {task["owner"]} (lease expired).', [], now)
            db.execute('UPDATE tasks SET status="claimed", owner=?, lease_until=?, updated=? WHERE id=?',
                       (agent, now + lease, now, task['id']))
            if not renewing:
                # A stale takeover is claimed -> claimed with the new owner as agent.
                self._event(db, task['id'], task['status'], 'claimed', agent, now)
            return self._load(db, task['id'])

    def heartbeat(self, agent, task_id, *, lease_minutes=30):
        agent, lease = self._agent(agent), self._lease(lease_minutes)
        now = self.clock()
        with self.connection() as db:
            task = self._load(db, task_id)
            if task['status'] != 'claimed' or task['owner'] != agent:
                raise BoardError('Only the current owner of a claimed task can renew it.')
            db.execute('UPDATE tasks SET lease_until=?, updated=? WHERE id=?', (now + lease, now, task['id']))
            return self._load(db, task['id'])

    def note(self, agent, task_id, kind, body, refs=None):
        agent = self._agent(agent)
        if kind not in NOTE_KINDS:
            raise BoardError('kind must be handoff, finding or question.')
        body = _text(body, 'body', 4000)
        refs = [_text(ref, 'ref', 200) for ref in (refs or [])]
        if len(refs) > 10:
            raise BoardError('At most 10 refs.')
        now = self.clock()
        with self.connection() as db:
            task = self._load(db, task_id)
            self._add_note(db, task['id'], agent, kind, body, refs, now)
            db.execute('UPDATE tasks SET updated=? WHERE id=?', (now, task['id']))
            return self._load(db, task['id'])

    def release(self, agent, task_id, status, summary, *, verification='', commit=''):
        agent = self._agent(agent)
        if status not in RELEASE_TO:
            raise BoardError('status must be open, blocked, review, done or cancelled.')
        summary = _text(summary, 'summary', 2000)
        verification = _text(verification, 'verification', 1000, required=False)
        if status == 'done' and not verification:
            raise BoardError('done requires verification (commands run and results).')
        if commit and not COMMIT.fullmatch(commit):
            raise BoardError('commit must be a 7-40 character hex hash.')
        now = self.clock()
        with self.connection() as db:
            task = self._load(db, task_id)
            if task['status'] != 'claimed' or task['owner'] != agent:
                raise BoardError('Only the current owner of a claimed task can release it.')
            body = summary + (f'\nVerification: {verification}' if verification else '')
            self._add_note(db, task['id'], agent, 'handoff', body, [f'commit:{commit}'] if commit else [], now)
            db.execute('UPDATE tasks SET status=?, lease_until=NULL, updated=? WHERE id=?', (status, now, task['id']))
            self._event(db, task['id'], 'claimed', status, agent, now)
            return self._load(db, task['id'])

    def get(self, task_id, *, notes=10):
        if type(notes) is not int or not 1 <= notes <= 50:
            raise BoardError('notes must be 1-50.')
        with self.connection() as db:
            task = self._load(db, task_id)
            rows = db.execute('SELECT * FROM (SELECT * FROM notes WHERE task_id=? ORDER BY id DESC LIMIT ?) ORDER BY id',
                              (task['id'], notes)).fetchall()
            task['notes'] = [{'author': row['author'], 'kind': row['kind'], 'body': row['body'],
                              'refs': json.loads(row['refs']), 'created': int(row['created'])} for row in rows]
            task['total_notes'] = db.execute('SELECT COUNT(*) FROM notes WHERE task_id=?', (task['id'],)).fetchone()[0]
            return task

    def _recipient(self, value):
        if value == '*':
            return value
        if isinstance(value, str) and ':' not in value and value in self._prefixes():
            return value
        return self._agent(value)

    def send(self, agent, to, body, *, task_id=''):
        """Direct message: to is an agent ID, an employee ID (any of its sessions) or '*' (everyone)."""
        agent, to = self._agent(agent), self._recipient(to)
        body = _text(body, 'body', 1000)
        now = self.clock()
        with self.connection() as db:
            if task_id:
                self._load(db, task_id)
            cursor = db.execute('INSERT INTO messages(sender,recipient,task_id,body,created) VALUES(?,?,?,?,?)',
                                (agent, to, task_id or None, body, now))
            # Bounded retention: keep the newest MAX_MESSAGES.
            db.execute('DELETE FROM messages WHERE id <= (SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET ?)',
                       (MAX_MESSAGES,))
            db.execute('DELETE FROM reads WHERE message_id NOT IN (SELECT id FROM messages)')
            return {'id': cursor.lastrowid, 'to': to, 'task_id': task_id or None}

    def pending_for_employee(self, employee):
        """Messages to an employee or its auto session that no session of that employee has read yet.

        Used by the dispatcher to decide whether to wake the employee; broadcasts never wake anyone."""
        return len(self.pending_ids(employee))

    def pending_ids(self, employee):
        """Sorted ids of the messages pending_for_employee counts: the dispatcher's input fingerprint (J2 H-02)."""
        if employee not in self._prefixes() or employee in ('user', 'local'):
            raise BoardError('Unknown employee.')
        with self.connection() as db:
            return [row[0] for row in db.execute(
                'SELECT m.id FROM messages m WHERE m.recipient IN (?,?) AND m.sender NOT LIKE ? '
                'AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.reader LIKE ?) ORDER BY m.id',
                (employee, employee + ':auto', employee + ':%', employee + ':%'))]

    def activity_snapshot(self):
        """Read-only HUD projection. Board declarations never imply a live process."""
        self.guard.check_running()
        self._prefixes()  # Load trusted roster; never expose its executable/configuration fields.
        now = self.clock()
        agents = {name: {'id': name, 'label': agent.label, 'roster_status': agent.status,
                        'connection_status': 'unknown', 'last_activity': None,
                        'claimed_count': 0, 'stale_count': 0}
                  for name, agent in self._roster.agents.items()}
        result = {'schema_version': 1, 'observed_at': int(now), 'read_only': True,
                  'source': 'self_reported', 'board_available': False, 'agents': list(agents.values()),
                  'tasks': [], 'timeline': [], 'has_more': {'tasks': False, 'timeline': False}}
        path = self.guard.validate_path(self.DATABASE, must_exist=False)
        if not path.exists():
            return result
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = path.with_name(path.name + suffix)
            self.guard.validate_path(str(sibling), must_exist=False)
            if sibling.exists() and (not sibling.is_file() or sibling.stat().st_nlink != 1):
                raise GuardrailError('Linked board storage is forbidden.')

        def text(value, limit):
            value = redact(value)
            return ''.join(c for c in value if c in '\n\t' or not unicodedata.category(c).startswith('C'))[:limit]

        db = None
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            rows = db.execute('SELECT id,title,status,owner,creator,lease_until,updated FROM tasks '
                              'ORDER BY CASE WHEN status="claimed" THEN 0 WHEN status IN ("blocked","review","open") '
                              'THEN 1 ELSE 2 END, updated DESC, id DESC').fetchall()
            result['has_more']['tasks'] = len(rows) > 50
            for row in rows:
                stale = row['status'] == 'claimed' and (row['lease_until'] or 0) <= now
                owner = agents.get((row['owner'] or '').split(':', 1)[0])
                if owner is not None and row['status'] == 'claimed':
                    owner['claimed_count'] += 1
                    owner['stale_count'] += int(stale)
            result['tasks'] = [{'id': row['id'], 'title': text(row['title'], 200),
                'status': row['status'], 'owner': row['owner'], 'stale':
                    row['status'] == 'claimed' and (row['lease_until'] or 0) <= now,
                'lease_until': row['lease_until'], 'updated': int(row['updated']), 'source': 'self_reported'}
                for row in rows[:50]]
            # Aggregate by employee, not by unbounded session IDs. Incoming mail is not activity.
            for table, column, timestamp in [('tasks', 'creator', 'created'),
                                              ('messages', 'sender', 'created'), ('notes', 'author', 'created')]:
                query = (f'SELECT substr({column},1,instr({column},":")-1) AS employee, '
                         f'MAX({timestamp}) AS latest FROM {table} GROUP BY employee')
                for row in db.execute(query):
                    agent = agents.get(row['employee'])
                    if agent is not None and row['latest'] is not None:
                        agent['last_activity'] = max(agent['last_activity'] or 0, int(row['latest']))
            messages = db.execute('SELECT * FROM messages ORDER BY id DESC LIMIT 41').fetchall()
            notes = db.execute('SELECT * FROM notes ORDER BY id DESC LIMIT 41').fetchall()
            timeline = []
            for row in messages:
                readers = [item['reader'] for item in db.execute(
                    'SELECT reader FROM reads WHERE message_id=? ORDER BY reader LIMIT 10', (row['id'],))]
                timeline.append({'id': 'message:' + str(row['id']), 'kind': 'message',
                    'from': row['sender'], 'to': row['recipient'], 'task_id': row['task_id'],
                    'text': text(row['body'], 600), 'created': int(row['created']),
                    'source': 'self_reported', 'readers': readers, '_order': row['created']})
            for row in notes:
                timeline.append({'id': 'note:' + str(row['id']), 'kind': 'note', 'from': row['author'],
                    'to': None, 'task_id': row['task_id'], 'text': text(row['body'], 600),
                    'created': int(row['created']), 'source': 'self_reported', 'readers': [], '_order': row['created']})
            timeline.sort(key=lambda item: (item['_order'], item['id']), reverse=True)
            result['timeline'] = timeline[:40]
            for item in result['timeline']:
                item.pop('_order')
            result['has_more']['timeline'] = len(timeline) > 40
            result['board_available'] = True
            self.guard.check_running()
            return result
        except sqlite3.Error as exc:
            raise BoardError('Board activity unavailable.') from exc
        finally:
            if db is not None:
                db.close()

    def inbox(self, agent, *, limit=20, mark_read=True):
        """Unread messages for this agent, oldest first. Reading marks them read unless mark_read is False."""
        agent = self._agent(agent)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise BoardError('limit must be 1-50.')
        prefix = agent.split(':', 1)[0]
        where = ('FROM messages m WHERE m.recipient IN (?,?,?) AND m.sender!=? '
                 'AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.reader=?)')
        args = (agent, prefix, '*', agent, agent)
        with self.connection() as db:
            unread = db.execute('SELECT COUNT(*) ' + where, args).fetchone()[0]
            rows = db.execute('SELECT m.* ' + where + ' ORDER BY m.id LIMIT ?', (*args, limit)).fetchall()
            if mark_read:
                db.executemany('INSERT OR IGNORE INTO reads VALUES (?,?)', [(row['id'], agent) for row in rows])
            return {'unread': unread,
                    'messages': [{'from': row['sender'], 'to': row['recipient'], 'task_id': row['task_id'],
                                  'body': row['body'], 'created': int(row['created'])} for row in rows]}

    def list(self, *, status='', agent='', limit=50):
        if status and status not in STATUSES:
            raise BoardError('Unknown status filter.')
        if agent:
            self._agent(agent)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BoardError('limit must be 1-100.')
        now = self.clock()
        query, args = 'SELECT * FROM tasks WHERE 1=1', []
        if status:
            query += ' AND status=?'
            args.append(status)
        if agent:
            query += ' AND (owner=? OR creator=?)'
            args += [agent, agent]
        with self.connection() as db:
            rows = db.execute(query + ' ORDER BY updated DESC LIMIT ?', (*args, limit)).fetchall()
            tasks = []
            for row in rows:
                task = self._row(row, now)
                tasks.append({'id': task['id'], 'title': task['title'], 'status': task['status'],
                              'owner': task['owner'], 'stale': task['stale'], 'paths': task['paths'],
                              'notes': db.execute('SELECT COUNT(*) FROM notes WHERE task_id=?', (task['id'],)).fetchone()[0],
                              'updated': int(task['updated'])})
            # Compact by design: details and notes come from board_get; board text is data, not instructions.
            return {'tasks': tasks}
