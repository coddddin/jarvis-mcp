"""PA4-T1: the Telegram status poller's own ledger. Never the board, never the bot token.

cursor   the highest update_id already recorded; getUpdates asks for the next one (Bot API: an update is
         confirmed once getUpdates is called with a higher offset).
updates  one row per update_id with a fixed outcome code, so a redelivered or replayed update is answered once.
outbox   replies waiting to be sent. Created in the same transaction as the update row, before the offset moves,
         so a crash after recording never loses a reply; a crash between send and mark may repeat one read-only
         reply. Text is cleared once sent or expired.
"""
from contextlib import contextmanager
import sqlite3
import time

from jarvis.core.guardrails import GuardrailError

DIRECTORY = '.jarvis-telegram'
DATABASE = '.jarvis-telegram/state.sqlite3'
KEEP_SECONDS = 7 * 86400
OUTCOMES = ('answered', 'backlog', 'backlog_merged', 'rate_limited', 'rejected', 'ignored', 'pairing')
ANSWERS = ('answered', 'backlog')


class TelegramState:
    def __init__(self, guard, *, clock=time.time):
        self.guard, self.clock = guard, clock

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(DIRECTORY, must_exist=False)
        if directory.is_symlink() or directory.is_junction():
            raise GuardrailError('Linked Telegram storage is forbidden.')
        directory.mkdir(exist_ok=True)
        path = self.guard.validate_path(DATABASE, must_exist=False)
        for suffix in ('', '-journal', '-wal', '-shm'):
            sibling = directory / (path.name + suffix)
            if sibling.exists() and (not sibling.is_file() or sibling.is_symlink() or sibling.stat().st_nlink != 1):
                raise GuardrailError('Linked Telegram storage is forbidden.')
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS cursor (k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS updates (update_id INTEGER PRIMARY KEY, received REAL NOT NULL, '
                       'outcome TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'update_id INTEGER NOT NULL UNIQUE, chat_id INTEGER NOT NULL, text TEXT NOT NULL, '
                       'state TEXT NOT NULL, attempts INTEGER NOT NULL, next_at REAL NOT NULL, created REAL NOT NULL)')
            yield db
            db.commit()
        finally:
            db.close()

    def offset(self):
        """The getUpdates offset: one more than the highest recorded update_id, or None before the first."""
        with self.connection() as db:
            row = db.execute("SELECT v FROM cursor WHERE k='last_update_id'").fetchone()
        return None if row is None else row['v'] + 1

    def seen(self, update_ids):
        ids = [int(value) for value in update_ids]
        if not ids:
            return set()
        with self.connection() as db:
            marks = ','.join('?' * len(ids))
            return {row['update_id'] for row in
                    db.execute(f'SELECT update_id FROM updates WHERE update_id IN ({marks})', ids)}

    def recent_answers(self, now):
        """(time of the latest answered update or None, answers in the last hour) for the reply rate limit."""
        with self.connection() as db:
            row = db.execute('SELECT MAX(received) AS last, SUM(received>=?) AS hour FROM updates '
                             'WHERE outcome IN (?,?)', (now - 3600, *ANSWERS)).fetchone()
        return row['last'], int(row['hour'] or 0)

    def record(self, entries, last_update_id):
        """entries: [{'update_id', 'outcome', optional 'chat_id' and 'text'}]. Records the updates, queues the
        replies and moves the cursor in one transaction. An update_id already recorded keeps its first outcome."""
        now = self.clock()
        for entry in entries:
            if entry['outcome'] not in OUTCOMES:
                raise ValueError('Unknown Telegram update outcome.')
        with self.connection() as db:
            for entry in entries:
                inserted = db.execute('INSERT OR IGNORE INTO updates VALUES (?,?,?)',
                                      (int(entry['update_id']), now, entry['outcome'])).rowcount
                if inserted and entry.get('text'):
                    db.execute('INSERT OR IGNORE INTO outbox(update_id,chat_id,text,state,attempts,next_at,created) '
                               "VALUES (?,?,?,'pending',0,?,?)",
                               (int(entry['update_id']), int(entry['chat_id']), entry['text'], now, now))
            db.execute("INSERT INTO cursor VALUES ('last_update_id', ?) ON CONFLICT(k) DO UPDATE "
                       'SET v=MAX(v, excluded.v)', (int(last_update_id),))
            db.execute('DELETE FROM updates WHERE received < ?', (now - KEEP_SECONDS,))
            db.execute("DELETE FROM outbox WHERE state IN ('sent','expired') AND created < ?", (now - KEEP_SECONDS,))

    def due(self, now):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,chat_id,text,attempts,created FROM outbox WHERE state='pending' AND next_at<=? "
                'ORDER BY id', (now,))]

    def sent(self, reply_id):
        self._finish(reply_id, 'sent')

    def expire(self, reply_id):
        self._finish(reply_id, 'expired')

    def _finish(self, reply_id, state):
        with self.connection() as db:
            db.execute("UPDATE outbox SET state=?, text='' WHERE id=? AND state='pending'", (state, reply_id))

    def retry(self, reply_id, next_at):
        with self.connection() as db:
            db.execute("UPDATE outbox SET attempts=attempts+1, next_at=? WHERE id=? AND state='pending'",
                       (next_at, reply_id))

    def expire_chat(self, chat_id):
        with self.connection() as db:
            db.execute("UPDATE outbox SET state='expired', text='' WHERE chat_id=? AND state='pending'", (int(chat_id),))

    def pending_count(self):
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]
