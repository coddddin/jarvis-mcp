"""W4-5 work evaluation: per-employee board counts, approximate and read-only."""
import hashlib

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


def _db_digest(store):
    return hashlib.sha256((store.guard.workspace / BoardStore.DATABASE).read_bytes()).hexdigest()


def test_sessions_merge_and_findings_rework_lead_time_are_counted(board):
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
    assert report['employees'] == {} and report['approximate'] is True
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
    assert '"approximate": true' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(['--days', '0'])
