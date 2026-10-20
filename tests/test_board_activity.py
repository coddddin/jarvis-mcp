"""HUD reads declarations without consuming inboxes or inventing worker liveness."""
import json
import os

import pytest

from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.features.board.service import BoardStore


@pytest.fixture
def board(tmp_path):
    return BoardStore(Guardrails(tmp_path), clock=lambda: 1000)


def test_empty_projection_does_not_initialize_storage_or_infer_connection(board):
    snapshot = board.activity_snapshot()
    assert not snapshot['board_available']
    assert not (board.guard.workspace / '.jarvis-board').exists()
    assert snapshot['read_only']
    assert all(agent['connection_status'] == 'unknown' and agent['last_activity'] is None
               for agent in snapshot['agents'])


def test_snapshot_projects_claims_and_messages_without_consuming_mail(board):
    task = board.create('codex:one', 'Current work', '', [])['id']
    board.claim('claude-code:review', task)
    message = board.send('codex:one', 'claude-code', 'Please review', task_id=task)
    before = board.inbox('claude-code:review', mark_read=False)
    first = board.activity_snapshot()
    assert first == board.activity_snapshot()
    assert board.inbox('claude-code:review', mark_read=False) == before
    assert first['tasks'][0]['owner'] == 'claude-code:review'
    assert not first['tasks'][0]['stale']
    agent = next(item for item in first['agents'] if item['id'] == 'claude-code')
    assert agent['claimed_count'] == 1 and agent['connection_status'] == 'unknown'
    event = next(item for item in first['timeline'] if item['id'] == f"message:{message['id']}")
    assert event['readers'] == [] and event['source'] == 'self_reported'
    board.inbox('claude-code:review')
    event = next(item for item in board.activity_snapshot()['timeline'] if item['id'] == event['id'])
    assert event['readers'] == ['claude-code:review']


def test_expired_claim_is_stale_without_reassignment_or_failure(board):
    task = board.create('codex:one', 'Work', '', [])['id']
    board.claim('codex:one', task, lease_minutes=5)
    board.clock = lambda: 1400
    snapshot = board.activity_snapshot()
    assert snapshot['tasks'][0]['status'] == 'claimed'
    assert snapshot['tasks'][0]['stale']
    assert next(a for a in snapshot['agents'] if a['id'] == 'codex')['stale_count'] == 1
    assert board.get(task)['owner'] == 'codex:one'


def test_projection_bounds_and_prioritizes_active_work_and_scrubs_legacy_text(board):
    task = board.create('codex:one', 'Older active', '', [])['id']
    board.claim('codex:one', task)
    for index in range(55):
        board.create('codex:one', f'New open {index}', '', [])
        board.send('codex:one', 'cursor', f'Message {index}')
    board.note('codex:one', task, 'finding', 'Most recent finding', [])
    with board.connection() as db:
        db.execute('UPDATE notes SET body=?', ('token=do-not-display\n\u202eVISIBLE' + 'x' * 800,))
    snapshot = board.activity_snapshot()
    assert len(snapshot['tasks']) == 50 and snapshot['tasks'][0]['id'] == task
    assert len(snapshot['timeline']) == 40
    assert snapshot['has_more'] == {'tasks': True, 'timeline': True}
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert 'do-not-display' not in encoded and '\u202e' not in encoded
    assert all(len(item['text']) <= 600 for item in snapshot['timeline'])


def test_stop_and_linked_database_are_refused(board, tmp_path):
    board.create('codex:one', 'Work', '', [])
    path = board.guard.workspace / board.DATABASE
    alias = tmp_path / 'alias.sqlite3'
    os.link(path, alias)
    with pytest.raises(GuardrailError, match='Linked'):
        board.activity_snapshot()
    alias.unlink()
    (board.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError, match='stopped'):
        board.activity_snapshot()
