"""Bounded, local conversation records; independent of model and tool execution."""
from contextlib import contextmanager
import json
import re
import sqlite3
import time
from uuid import uuid4

from jarvis.core.guardrails import GuardrailError
from jarvis.core.redaction import redact


class ChatError(ValueError):
    """A chat request cannot be safely completed."""


class ChatBusy(ChatError):
    """An earlier request must complete before another starts."""


class ChatStore:
    DIRECTORY = '.jarvis-chat'
    DATABASE = '.jarvis-chat/conversations.sqlite3'
    MAX_SESSIONS = 100
    MAX_TURNS = 40
    MAX_RESERVATION = 64 * 9216

    def __init__(self, guard):
        self.guard = guard
        guard.check_running()
        guard.validate_path(self.DIRECTORY, must_exist=False).mkdir(exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS conversations '
                       '(id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, reserved INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS turns '
                       '(conversation TEXT NOT NULL, id TEXT NOT NULL, created REAL NOT NULL, '
                       'deadline REAL NOT NULL, status TEXT NOT NULL, user TEXT NOT NULL, '
                       'assistant TEXT NOT NULL DEFAULT "", tools TEXT NOT NULL DEFAULT "[]", '
                       'PRIMARY KEY(conversation,id))')
            columns = {row[1] for row in db.execute('PRAGMA table_info(turns)')}
            if 'mode' not in columns:
                db.execute('ALTER TABLE turns ADD COLUMN mode TEXT NOT NULL DEFAULT "chat"')
            if 'team' not in columns:
                db.execute('ALTER TABLE turns ADD COLUMN team TEXT NOT NULL DEFAULT "{}"')
            if 'requested_mode' not in columns:
                db.execute('ALTER TABLE turns ADD COLUMN requested_mode TEXT NOT NULL DEFAULT "chat"')
                db.execute('UPDATE turns SET requested_mode=mode')
            if 'request_plan' not in columns:
                db.execute('ALTER TABLE turns ADD COLUMN request_plan TEXT NOT NULL DEFAULT "{}"')
            if 'dispatch' not in columns:
                db.execute('ALTER TABLE turns ADD COLUMN dispatch TEXT NOT NULL DEFAULT "{}"')

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY)
        path = self.guard.validate_path(self.DATABASE, must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = self.guard.validate_path(str(directory / (path.name + suffix)), must_exist=False)
            if sibling.exists() and (not sibling.is_file() or sibling.stat().st_nlink != 1):
                raise GuardrailError('Linked chat storage is forbidden.')
        db = None
        try:
            db = sqlite3.connect(path, timeout=1, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            yield db
            self.guard.check_running()
            db.commit()
        except sqlite3.Error as exc:
            raise ChatError('Chat storage unavailable.') from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
            raise ChatError('Invalid conversation or request ID.')
        return value

    def create(self):
        with self.connection() as db:
            if db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0] >= self.MAX_SESSIONS:
                raise ChatError('Conversation limit reached; retain/export records before operator cleanup.')
            identity, now = uuid4().hex, time.time()
            db.execute('INSERT INTO conversations VALUES (?,?,?,?,0)', (identity, '새 대화', now, now))
        return self.get(identity)

    def listing(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM conversations ORDER BY updated DESC LIMIT 100')]

    def get(self, identity):
        self.identifier(identity)
        with self.connection() as db:
            row = db.execute('SELECT * FROM conversations WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ChatError('Conversation not found.')
            turns = []
            for item in db.execute('SELECT * FROM turns WHERE conversation=? ORDER BY created, id', (identity,)):
                turn = dict(item)
                turn['tools'] = json.loads(turn['tools'])
                turn['team'] = json.loads(turn['team'])
                turn['request_plan'] = json.loads(turn['request_plan'])
                turn['dispatch'] = json.loads(turn['dispatch'])
                # A dead process is visible as interrupted; never fabricate an answer or replay tools.
                if turn['status'] == 'pending' and time.time() > turn['deadline']:
                    turn['status'] = 'interrupted'
                turns.append(turn)
            return {**dict(row), 'turns': turns}

    def begin(self, identity, request_id, message, deadline, mode='chat'):
        self.identifier(identity)
        self.identifier(request_id)
        if mode not in {'chat', 'team', 'team-local', 'auto', 'free'}:
            raise ChatError('Invalid conversation mode.')
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ChatError('Messages must contain 1–2000 characters.')
        if redact(message) != message:
            raise ChatError('Remove credentials or control characters before sending.')
        with self.connection() as db:
            if db.execute('SELECT id FROM conversations WHERE id=?', (identity,)).fetchone() is None:
                raise ChatError('Conversation not found.')
            previous = db.execute('SELECT * FROM turns WHERE conversation=? AND id=?', (identity, request_id)).fetchone()
            if previous is not None:
                if previous['user'] != message or previous['requested_mode'] != mode:
                    raise ChatError('Request ID already belongs to a different message.')
                return False
            if db.execute('SELECT id FROM turns WHERE conversation=? AND status="pending" AND deadline>?',
                          (identity, time.time())).fetchone():
                raise ChatBusy('A response is already in progress.')
            if db.execute('SELECT COUNT(*) FROM turns WHERE conversation=?', (identity,)).fetchone()[0] >= self.MAX_TURNS:
                raise ChatError('Conversation turn limit reached; start a new conversation.')
            now = time.time()
            db.execute('INSERT INTO turns(conversation,id,created,deadline,status,user,mode,requested_mode) VALUES(?,?,?,?,?,?,?,?)',
                       (identity, request_id, now, deadline, 'pending', message, mode, mode))
            db.execute('UPDATE conversations SET title=CASE WHEN title="새 대화" THEN ? ELSE title END, updated=? WHERE id=?',
                       (message.strip()[:60], now, identity))
        return True

    def resolve_auto(self, identity, request_id, mode, plan):
        self.identifier(identity)
        self.identifier(request_id)
        if mode not in {'chat', 'team-local', 'free'}:
            raise ChatError('Automatic routing permits chat/local or verified free dispatch only.')
        encoded = json.dumps(plan, ensure_ascii=False)
        if len(encoded.encode()) > 2048:
            raise ChatError('Request plan exceeds storage limit.')
        with self.connection() as db:
            changed = db.execute('UPDATE turns SET mode=?,request_plan=?,deadline=MIN(deadline,created+?) '
                'WHERE conversation=? AND id=? AND requested_mode="auto" AND mode="auto" '
                'AND status="pending" AND deadline>?',
                (mode, encoded, 180 if mode == 'chat' else 660, identity, request_id, time.time())).rowcount
            if changed != 1:
                raise ChatError('Automatic request already resolved or expired; replay is forbidden.')

    def update_dispatch(self, identity, request_id, previous, state):
        self.identifier(identity)
        self.identifier(request_id)
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        encoded = encode(state)
        if len(encoded.encode()) > 32768:
            raise ChatError('Dispatch state exceeds storage limit.')
        with self.connection() as db:
            count = db.execute('UPDATE turns SET dispatch=? WHERE conversation=? AND id=? AND mode="free" '
                'AND status="pending" AND deadline>? AND dispatch=?',
                (encoded, identity, request_id, time.time(), encode(previous))).rowcount
            if count != 1:
                raise ChatBusy('Dispatch state changed or request expired; execution refused.')

    def progress(self, identity, request_id, state):
        self.identifier(identity)
        self.identifier(request_id)
        encoded = json.dumps(state, ensure_ascii=False)
        if len(encoded.encode()) > 100000:
            raise ChatError('Team state exceeds storage limit.')
        with self.connection() as db:
            db.execute('UPDATE turns SET team=? WHERE conversation=? AND id=? AND status="pending"',
                       (encoded, identity, request_id))

    def reserve(self, identity):
        with self.connection() as db:
            row = db.execute('SELECT reserved FROM conversations WHERE id=?', (identity,)).fetchone()
            if row is None or row[0] + 9216 > self.MAX_RESERVATION:
                raise ChatError('Conversation model budget exhausted.')
            db.execute('UPDATE conversations SET reserved=reserved+9216 WHERE id=?', (identity,))

    def finish(self, identity, request_id, *, reply='', status='completed', tools=()):
        if status not in {'completed', 'failed'}:
            raise ChatError('Invalid result status.')
        with self.connection() as db:
            db.execute('UPDATE turns SET assistant=?,status=?,tools=? WHERE conversation=? AND id=? AND status="pending"',
                       (redact(reply)[:6000], status, json.dumps(tools, ensure_ascii=True), identity, request_id))
            db.execute('UPDATE conversations SET updated=? WHERE id=?', (time.time(), identity))
