"""W4-5 work evaluation: per-employee board counts, exact with status history, read-only."""
import hashlib
import sqlite3

import pytest

from jarvis.features.evaluation.service import EvaluationError, evaluate, summarize
from jarvis.cli import evaluation as cli
from jarvis.core.guardrails import Guardrails
from jarvis.features.board.service import BoardStore

MAIN, SECOND, CODEX = 'claude-code:main', 'claude-code:second', 'codex:chat'


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def board(tmp_path):
    clock = Clock()
    return BoardStore(Guardrails(tmp_path), clock=clock), clock


def _forget_history(store):
    """Simulate tasks made before status_events existed."""
    db = sqlite3.connect(store.guard.workspace / BoardStore.DATABASE)
    with db:
        db.execute('DELETE FROM status_events')
    db.close()


def _db_digest(store):
    return hashlib.sha256((store.guard.workspace / BoardStore.DATABASE).read_bytes()).hexdigest()


def test_legacy_tasks_use_the_note_approximation(board):
    store, clock = board
    # Codex task: reviewed by Claude with a finding, fixed, then done after 2 hours.
    reworked = store.create(CODEX, 'Fix', '', ['a.py'])['id']
    store.claim(CODEX, reworked)
    clock.now += 3600
    store.release(CODEX, reworked, 'review', 'first try')
    store.note(MAIN, reworked, 'finding', 'edge case missing')
    store.claim(CODEX, reworked)
    clock.now += 3600
    store.release(CODEX, reworked, 'done', 'fixed', verification='pytest ok')
    # Two Claude sessions: one done without findings, one still claimed past its lease (stale).
    clean = store.create(SECOND, 'Docs', '', ['b.md'])['id']
    store.claim(SECOND, clean)
    clock.now += 1800
    store.release(SECOND, clean, 'done', 'docs', verification='checked')
    stale = store.create(MAIN, 'Long', '', ['c.py'])['id']
    store.claim(MAIN, stale, lease_minutes=5)
    store.note(MAIN, stale, 'finding', 'self note is not a review finding')
    store.create(CODEX, 'Unowned', '', ['d.py'])
    clock.now += 600
    _forget_history(store)

    report = evaluate(store.guard, now=clock.now)
    assert report['approximate'] is True and 'approximate' in report['definition']
    codex, claude = report['employees']['codex'], report['employees']['claude-code']
    assert set(report['employees']) == {'codex', 'claude-code'}
    assert (codex['tasks'], codex['done'], codex['open']) == (2, 1, 1)
    assert (codex['findings_received'], codex['reworked_tasks']) == (1, 1)
    assert codex['done_lead_hours'] == {'count': 1, 'mean': 2.0, 'median': 2.0}
    assert (claude['tasks'], claude['done'], claude['claimed'], claude['stale']) == (2, 1, 1, 1)
    assert (claude['findings_received'], claude['reworked_tasks']) == (0, 0)
    assert claude['done_lead_hours'] == {'count': 1, 'mean': 0.5, 'median': 0.5}


def test_finding_without_later_owner_handoff_is_not_rework(board):
    store, clock = board
    task = store.create(CODEX, 'T', '', ['a.py'])['id']
    store.claim(CODEX, task)
    store.release(CODEX, task, 'review', 'ready')
    clock.now += 60
    store.note(MAIN, task, 'finding', 'needs a test')
    _forget_history(store)
    codex = evaluate(store.guard, now=clock.now)['employees']['codex']
    assert (codex['review'], codex['findings_received'], codex['reworked_tasks']) == (1, 1, 0)
    assert codex['done_lead_hours'] == {'count': 0, 'mean': None, 'median': None}


def test_since_filters_by_creation_time(board):
    store, clock = board
    store.create(CODEX, 'Old', '', ['a.py'])
    clock.now += 10 * 86400
    store.create(CODEX, 'New', '', ['b.py'])
    assert evaluate(store.guard, now=clock.now, since=clock.now - 86400)['employees']['codex']['tasks'] == 1
    assert evaluate(store.guard, now=clock.now)['employees']['codex']['tasks'] == 2


def test_evaluation_never_writes_the_board(board):
    store, clock = board
    store.create(CODEX, 'T', '', ['a.py'])
    before = _db_digest(store)
    evaluate(store.guard, now=clock.now)
    assert _db_digest(store) == before


def test_missing_board_gives_empty_report_and_creates_nothing(tmp_path):
    guard = Guardrails(tmp_path)
    report = evaluate(guard, now=1.0)
    assert report['employees'] == {} and report['approximate'] is False
    assert not (tmp_path / BoardStore.DIRECTORY).exists()


def test_unreadable_board_raises_evaluation_error(tmp_path):
    (tmp_path / BoardStore.DIRECTORY).mkdir()
    (tmp_path / BoardStore.DATABASE).write_bytes(b'not a database')
    with pytest.raises(EvaluationError):
        evaluate(Guardrails(tmp_path), now=1.0)


def test_summarize_ignores_rows_without_an_agent():
    report = summarize([{'id': 'x', 'status': 'open', 'creator': '', 'owner': None, 'lease_until': None,
                         'created': 0.0}], [], now=1.0)
    assert report['employees'] == {}


def test_cli_prints_json_and_rejects_bad_days(board, monkeypatch, capsys):
    store, _ = board
    store.create(CODEX, 'T', '', ['a.py'])
    monkeypatch.setenv('JARVIS_WORKSPACE', str(store.guard.workspace))
    assert cli.main([]) == 0
    assert '"approximate": false' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(['--days', '0'])


def test_full_history_gives_exact_rework_failures_and_work_hours(board):
    store, clock = board
    task = store.create(MAIN, 'Fix', '', ['a.py'])['id']
    clock.now += 3600                                   # waiting in open is not work time
    store.claim(CODEX, task)
    clock.now += 1800
    store.release(CODEX, task, 'review', 'first try')
    store.note(MAIN, task, 'finding', 'edge case missing')
    store.claim(CODEX, task)
    clock.now += 1800
    store.release(CODEX, task, 'review', 'fixed')       # cycle 1, after a finding: verification failure
    store.claim(CODEX, task)
    store.release(CODEX, task, 'review', 'rebased')     # cycle 2, no new finding
    store.claim(SECOND, task)
    store.release(SECOND, task, 'done', 'merged', verification='pytest ok')
    report = evaluate(store.guard, now=clock.now)
    assert report['approximate'] is False
    claude = report['employees']['claude-code']       # done goes to the agent that marked it done
    assert (claude['tasks'], claude['done'], claude['exact_tasks'], claude['approximate_tasks']) == (1, 1, 1, 0)
    assert (claude['rework_cycles'], claude['reworked_tasks'], claude['verification_failures']) == (2, 1, 1)
    assert claude['done_work_hours'] == {'count': 1, 'mean': 1.0, 'median': 1.0}
    assert claude['done_lead_hours']['count'] == 0


def test_mixed_board_flags_only_when_legacy_tasks_remain(board):
    store, clock = board
    old = store.create(CODEX, 'Old', '', ['a.py'])['id']
    _forget_history(store)
    store.create(CODEX, 'New', '', ['b.py'])
    codex = evaluate(store.guard, now=clock.now)['employees']['codex']
    assert (codex['exact_tasks'], codex['approximate_tasks']) == (1, 1)
    assert evaluate(store.guard, now=clock.now)['approximate'] is True
    db = sqlite3.connect(store.guard.workspace / BoardStore.DATABASE)
    with db:
        db.execute('DELETE FROM tasks WHERE id=?', (old,))
    db.close()
    assert evaluate(store.guard, now=clock.now)['approximate'] is False


def test_history_that_misses_a_transition_is_not_exact(board):
    store, clock = board
    task = store.create(CODEX, 'T', '', ['a.py'])['id']
    store.claim(CODEX, task)
    db = sqlite3.connect(store.guard.workspace / BoardStore.DATABASE)
    with db:  # e.g. a claim made by an older server that did not record events
        db.execute("DELETE FROM status_events WHERE to_status='claimed'")
    db.close()
    codex = evaluate(store.guard, now=clock.now)['employees']['codex']
    assert (codex['exact_tasks'], codex['approximate_tasks']) == (0, 1)


def test_board_without_status_events_table_is_all_approximate(tmp_path):
    (tmp_path / BoardStore.DIRECTORY).mkdir()
    db = sqlite3.connect(tmp_path / BoardStore.DATABASE)
    with db:
        db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL, '
                   'status TEXT NOT NULL, creator TEXT NOT NULL, owner TEXT, lease_until REAL, paths TEXT NOT NULL, '
                   'depends_on TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')
        db.execute('CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, '
                   'author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, refs TEXT NOT NULL, created REAL NOT NULL)')
        db.execute("INSERT INTO tasks VALUES ('a'||hex(randomblob(15)),'T','','open','codex:x',NULL,NULL,'[]','[]',0,0)")
    db.close()
    report = evaluate(Guardrails(tmp_path), now=1.0)
    assert report['approximate'] is True and report['employees']['codex']['approximate_tasks'] == 1
