"""W3 alerts, stage 1: pick dispatcher events the user should hear about, once per local day per event.

Events come as plain data from the dispatcher tick (board rows, adapter check codes, process watch
findings), so this module stays in the agents layer. Text is whitelisted: task titles (redacted, max 60
characters), employee IDs and fixed Korean phrases; never notes, paths, commands or log names. Delivery
(board_send) is the caller's job; Windows toast notifications are stage 2.
"""
from contextlib import contextmanager
import sqlite3
import time

from jarvis.agents.dispatch_policy import PROBLEM_LABELS
from jarvis.core.guardrails import GuardrailError
from jarvis.core.redaction import redact

DIRECTORY = '.jarvis-dispatch'
DATABASE = '.jarvis-dispatch/alerts.sqlite3'
KEEP_DAYS = 7
MAX_LINES = 10
RECENT = 20
WATCH_TEXT = {'service_down': '디스패처 서비스가 꺼져 있음 (설정은 켜짐, 일시정지 아님)',
              'owner_dead': '실행 중 기록의 프로세스가 없음',
              'overdue': '실행 제한 시간 초과'}


def day_of(now):
    return time.strftime('%Y-%m-%d', time.localtime(now))


def _title(task):
    return redact(str(task.get('title') or ''))[:60].replace('"', "'")


def _who(agent):
    return str(agent or '-').split(':')[0]


def events(*, stale=(), blocked=(), problems=None, not_woken=(), findings=()):
    """[(key, text)] in a stable order. stale/blocked: board task rows; problems: {employee: [codes]};
    not_woken: employees with waiting work the adapter check kept asleep; findings: process_watch findings."""
    found = []
    for task in stale:
        found.append((f"stale_claim:task:{task['id']}", f'멈춘 담당: {_title(task)}({_who(task.get("owner"))})'))
    for task in blocked:
        found.append((f"blocked_task:task:{task['id']}", f'막힌 작업: {_title(task)}({_who(task.get("owner"))})'))
    for employee, codes in sorted((problems or {}).items()):
        for code in codes:
            if code in PROBLEM_LABELS:
                found.append((f'adapter:{employee}:{code}', f'어댑터 점검: {employee} {PROBLEM_LABELS[code]}'))
    for employee in sorted(set(not_woken)):
        found.append((f'adapter_check_failed:{employee}', f'받은 일이 있지만 점검 때문에 깨우지 않음: {employee}'))
    for item in findings:
        kind = item.get('kind')
        if kind not in WATCH_TEXT:
            continue
        employee = (item.get('detail') or {}).get('employee')
        suffix = f' ({_who(employee)}, {item.get("ref")})' if employee else ''
        found.append((f"watch:{kind}:{item.get('ref')}", WATCH_TEXT[kind] + suffix))
    return found


def message(lines, more=0):
    text = '[알림] 자비스 확인 필요\n' + '\n'.join('- ' + line for line in lines)
    if more:
        text += f'\n- 외 {more}건은 다음 확인 때 알림'
    return text.replace('"', "'")[:1000]


class AlertLedger:
    """Which alert keys were sent on which day. Written only by the dispatcher tick."""

    def __init__(self, guard, *, clock=time.time):
        self.guard, self.clock = guard, clock

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(DIRECTORY, must_exist=False)
        if directory.is_symlink() or directory.is_junction():
            raise GuardrailError('Linked dispatch storage is forbidden.')
        directory.mkdir(exist_ok=True)
        db = sqlite3.connect(self.guard.validate_path(DATABASE, must_exist=False), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS alerts (key TEXT NOT NULL, day TEXT NOT NULL, text TEXT NOT NULL, '
                       'sent REAL NOT NULL, PRIMARY KEY(key, day))')
            yield db
            db.commit()
        finally:
            db.close()

    def unsent(self, found, now):
        """The (key, text) pairs not yet sent today, in order."""
        day = day_of(now)
        with self.connection() as db:
            sent = {row['key'] for row in db.execute('SELECT key FROM alerts WHERE day=?', (day,))}
        return [(key, text) for key, text in found if key not in sent]

    def mark(self, items, now):
        day, cutoff = day_of(now), day_of(now - KEEP_DAYS * 86400)
        with self.connection() as db:
            db.executemany('INSERT OR IGNORE INTO alerts VALUES (?,?,?,?)',
                           [(key, day, text, now) for key, text in items])
            db.execute('DELETE FROM alerts WHERE day < ?', (cutoff,))


def notify(ledger, found, send, now):
    """Send today's new alerts as one message; record them only after send() succeeded. Returns sent count."""
    fresh = ledger.unsent(found, now)
    if not fresh:
        return 0
    shown = fresh[:MAX_LINES]
    send(message([text for _, text in shown], more=len(fresh) - len(shown)))
    ledger.mark(shown, now)
    return len(shown)


def read_today(guard, now, *, limit=RECENT):
    """Today's sent alerts for the HUD, newest first, opened read-only. No file yet: an empty list."""
    guard.check_running()
    path = guard.validate_path(DATABASE, must_exist=False)
    if not path.exists():
        return []
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise GuardrailError('Linked or unusual alert storage is refused.')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        rows = db.execute('SELECT text, sent FROM alerts WHERE day=? ORDER BY sent DESC, rowid DESC LIMIT ?',
                          (day_of(now), limit)).fetchall()
    finally:
        db.close()
    return [{'text': text, 'sent': sent} for text, sent in rows]
