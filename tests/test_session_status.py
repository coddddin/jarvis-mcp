import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from jarvis.agents.session_status import SessionStatusStore, SessionStatusError
from jarvis.core.guardrails import Guardrails, GuardrailError


def signal(event='turn_started', turn='turn-1', **kwargs):
    return dict(employee='codex', session_id='session-1', event_id=uuid4().hex,
                turn_id=turn, event=event, source='self_reported', **kwargs)


@pytest.fixture
def store(tmp_path):
    return SessionStatusStore(Guardrails(tmp_path), clock=lambda: 1000)


def test_duplicate_does_not_refresh_receipt_and_survives_reconstruction(store):
    event = signal()
    assert store.report(event) == {'status': 'accepted', 'applied': True}
    store.clock = lambda: 1200
    assert store.report(event) == {'status': 'duplicate', 'applied': False}
    fresh = SessionStatusStore(store.guard, clock=lambda: 1200)
    snapshot = fresh.inspect()
    assert snapshot['sessions'][0]['receipt'] == {'source': 'observed', 'received_at': 1000, 'status': 'old'}
    assert snapshot['sessions'][0]['connection_status'] == 'unknown'
    assert snapshot['sessions'][0]['verified'] == []
    assert snapshot['identity_verified'] is False
    with pytest.raises(SessionStatusError):
        store.report({**event, 'event': 'tool_started'})


@pytest.mark.parametrize('terminal', ['turn_stopped', 'interrupted'])
def test_terminal_turn_cannot_be_reactivated_by_late_start_or_tool(store, terminal):
    store.report(signal())
    store.report(signal(terminal))
    for event in ('tool_started', 'tool_finished', 'permission_requested', 'turn_started'):
        assert store.report(signal(event))['status'] == 'ignored'
        assert store.inspect()['sessions'][0]['state'] == terminal


def test_end_before_start_is_preserved_and_old_turn_cannot_stop_new(store):
    store.report(signal('turn_stopped', 'old'))
    assert store.report(signal('turn_started', 'old'))['status'] == 'ignored'
    store.report(signal('turn_started', 'new'))
    store.report(signal('turn_stopped', 'old'))
    snapshot = store.inspect()['sessions'][0]
    assert snapshot['state'] == 'turn_started' and snapshot['turn_id'] == 'new'


def test_superseded_turn_never_overwrites_current_permission_report(store):
    store.report(signal('turn_started', 'old'))
    store.report(signal('turn_started', 'new'))
    store.report(signal('permission_requested', 'new'))
    assert store.report(signal('turn_started', 'old'))['status'] == 'ignored'
    assert store.report(signal('tool_finished', 'old'))['status'] == 'ignored'
    assert store.inspect()['sessions'][0]['state'] == 'permission_requested'


def test_uncorrelated_tool_or_missing_turn_is_not_active(store):
    assert store.report(signal('tool_started', None))['status'] == 'ignored'
    assert store.report(signal('tool_started', 'unknown'))['status'] == 'ignored'
    assert store.inspect()['sessions'][0]['state'] == 'unknown'


def test_session_end_is_terminal_until_explicit_resume_and_old_turn_stays_terminal(store):
    store.report(signal())
    store.report(signal('session_ended', None))
    assert store.report(signal('tool_started'))['status'] == 'ignored'
    store.report(signal('session_started', None))
    assert store.report(signal('turn_started'))['status'] == 'ignored'
    store.report(signal('turn_started', 'new'))
    assert store.inspect()['sessions'][0]['turn_id'] == 'new'


@pytest.mark.parametrize('change', [dict(employee='claude-code'), dict(source='observed'),
    dict(event='verification_passed'), dict(session_id='../private'), dict(event_id='invalid'),
    dict(turn_id='x'*81), dict(command='secret-command'), dict(tool_output='private-output')])
def test_rejects_unknown_content_and_unsafe_ids(store, change):
    with pytest.raises(SessionStatusError):
        store.report({**signal(), **change})
    assert store.inspect()['sessions'] == []


def test_no_private_content_columns_or_success_claims(store):
    store.report(signal('turn_started'))
    store.report(signal('turn_stopped'))
    with store.connection(write=False) as db:
        columns = {row['name'] for row in db.execute('PRAGMA table_info(events)')}
    assert columns == {'event_id','employee','session_id','turn_id','event','digest','received_at','applied'}
    assert store.inspect()['sessions'][0]['source'] == 'self_reported'
    assert store.inspect()['sessions'][0]['verified'] == []


def test_limits_refuse_new_events_without_deleting_previous_or_duplicate(store):
    store.MAX_EVENTS = 1
    event = signal()
    store.report(event)
    assert store.report(event)['status'] == 'duplicate'
    with pytest.raises(SessionStatusError):
        store.report(signal('turn_stopped'))
    assert store.inspect()['sessions'][0]['state'] == 'turn_started'


def test_session_limit_and_unknown_clock_are_explicit(store):
    store.MAX_SESSIONS = 1
    store.report(signal())
    with pytest.raises(SessionStatusError):
        store.report({**signal(), 'session_id': 'other'})
    store.clock = lambda: 900
    assert store.inspect()['sessions'][0]['receipt']['status'] == 'clock_unknown'
    store.clock = lambda: float('nan')
    with pytest.raises(SessionStatusError):
        store.inspect()


def test_concurrent_duplicate_has_single_accepted_event(store):
    event = signal()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(store.report, [event]*4))
    assert [r['status'] for r in results].count('accepted') == 1
    with store.connection(write=False) as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1


def test_stop_and_hardlinked_storage_are_refused(tmp_path):
    guard = Guardrails(tmp_path)
    store = SessionStatusStore(guard)
    (tmp_path/'.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        store.report(signal())
    (tmp_path/'.jarvis-stop').unlink()
    path = tmp_path/'.jarvis-sessions/sessions.sqlite3'
    os.link(path, tmp_path/'linked.sqlite3')
    with pytest.raises(GuardrailError):
        store.inspect()


def test_short_timeout_reports_lock_failure_without_changing_prior_state(tmp_path):
    store = SessionStatusStore(Guardrails(tmp_path), busy_timeout=0.02)
    store.report(signal())
    with store.connection() as locked:
        with pytest.raises(SessionStatusError, match='unavailable'):
            store.report(signal('turn_stopped'))
    assert store.inspect()['sessions'][0]['state'] == 'turn_started'
    assert store.report(signal('turn_stopped'))['status'] == 'accepted'


@pytest.mark.parametrize('timeout', [True, -1, 3, float('nan'), '0.2'])
def test_invalid_lock_timeout_is_rejected(tmp_path, timeout):
    with pytest.raises(ValueError, match='timeout'):
        SessionStatusStore(Guardrails(tmp_path), busy_timeout=timeout)
