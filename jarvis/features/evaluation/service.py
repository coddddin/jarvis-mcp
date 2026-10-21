"""W4-5 work evaluation: per-employee counts from the shared board, read-only.

Tasks with a complete status history (status_events from creation up to the current status) are counted
exactly; older tasks, or tasks whose history was pruned, fall back to note-based approximations and set
approximate=true (see DEFINITION). Sessions are merged: 'claude-code:main' and
'claude-code:second' both count for 'claude-code'. Nothing is written to the board.
"""
import json
import sqlite3
from statistics import mean, median

from jarvis.features.board.service import STATUSES, BoardStore

DEFINITION = ('exact tasks (full status history): done goes to the agent of the done event; rework cycle = a return to '
              'review after an earlier review; verification failure = such a return after a finding by another agent; '
              'work hours = first claim to done. approximate tasks (no history): current owner (creator if unowned); '
              'reworked = owner handoff after a finding by another agent; lead hours = created to last owner handoff.')


class EvaluationError(ValueError):
    """The board could not be read for evaluation."""


def employee(agent):
    return agent.partition(':')[0] if isinstance(agent, str) else ''


def _empty():
    return {'tasks': 0, **{status: 0 for status in STATUSES}, 'stale': 0,
            'exact_tasks': 0, 'approximate_tasks': 0, 'exact_done': 0, 'exact_reworked_done': 0,
            'findings_received': 0, 'reworked_tasks': 0,
            'rework_cycles': 0, 'verification_failures': 0, 'work_hours': [], 'lead_hours': []}


def _read(guard):
    """(tasks, notes, events) rows from the board opened read-only; empty lists when no board exists yet."""
    guard.check_running()
    path = guard.validate_path(BoardStore.DATABASE, must_exist=False)
    if not path.exists():
        return [], [], []
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise EvaluationError('Linked or unusual board storage is refused.')
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        tasks = [dict(row) for row in db.execute(
            'SELECT id,status,creator,owner,lease_until,created FROM tasks')]
        notes = [dict(row) for row in db.execute(
            'SELECT id,task_id,author,kind,created FROM notes ORDER BY id')]
        has_events = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='status_events'").fetchone()
        events = [dict(row) for row in db.execute(
            'SELECT id,task_id,from_status,to_status,agent,at FROM status_events ORDER BY id')] if has_events else []
        return tasks, notes, events
    except sqlite3.Error as exc:
        raise EvaluationError('Board storage unavailable.') from exc
    finally:
        if db is not None:
            db.close()


def _complete(history, task):
    """True when the history starts at creation and ends at the task's current status (nothing missed)."""
    return (bool(history) and history[0]['from_status'] is None and history[0]['to_status'] == 'open'
            and history[-1]['to_status'] == task['status'])


def _hours(values):
    return {'count': len(values), 'mean': round(mean(values), 2) if values else None,
            'median': round(median(values), 2) if values else None}


def summarize(tasks, notes, events=(), *, now, since=None):
    """Per-employee report from board rows. since (epoch seconds) keeps tasks created at or after it."""
    notes_by, events_by = {}, {}
    for note in notes:
        notes_by.setdefault(note['task_id'], []).append(note)
    for event in events:
        events_by.setdefault(event['task_id'], []).append(event)
    employees = {}
    for task in tasks:
        if since is not None and task['created'] < since:
            continue
        task_notes, history = notes_by.get(task['id'], []), events_by.get(task['id'], [])
        exact = _complete(history, task)
        holder = task['owner'] or task['creator']
        if exact and task['status'] == 'done':
            holder = history[-1]['agent']
        name = employee(holder)
        if not name:
            continue
        row = employees.setdefault(name, _empty())
        row['tasks'] += 1
        row['exact_tasks' if exact else 'approximate_tasks'] += 1
        if task['status'] in STATUSES:
            row[task['status']] += 1
        if task['status'] == 'claimed' and (task['lease_until'] or 0) <= now:
            row['stale'] += 1
        findings = [note for note in task_notes if note['kind'] == 'finding' and note['author'] != holder]
        row['findings_received'] += len(findings)
        if exact:
            reviews = [event['at'] for event in history if event['to_status'] == 'review']
            cycles = list(zip(reviews, reviews[1:]))
            row['rework_cycles'] += len(cycles)
            row['reworked_tasks'] += int(bool(cycles))
            if task['status'] == 'done':
                row['exact_done'] += 1
                row['exact_reworked_done'] += int(bool(cycles))
            row['verification_failures'] += sum(
                any(start <= note['created'] <= stop for note in findings) for start, stop in cycles)
            claims = [event['at'] for event in history if event['to_status'] == 'claimed']
            if task['status'] == 'done' and claims:
                row['work_hours'].append(max(0.0, history[-1]['at'] - claims[0]) / 3600)
        else:
            handoffs = [note for note in task_notes if note['kind'] == 'handoff' and note['author'] == holder]
            if findings and handoffs and handoffs[-1]['id'] > findings[0]['id']:
                row['reworked_tasks'] += 1
            if task['status'] == 'done' and handoffs:
                row['lead_hours'].append(max(0.0, handoffs[-1]['created'] - task['created']) / 3600)
    for row in employees.values():
        row['done_work_hours'] = _hours(row.pop('work_hours'))
        row['done_lead_hours'] = _hours(row.pop('lead_hours'))
    return {'approximate': any(row['approximate_tasks'] for row in employees.values()),
            'definition': DEFINITION, 'generated_at': int(now),
            'since': int(since) if since is not None else None,
            'employees': dict(sorted(employees.items()))}


def evaluate(guard, *, now, since=None):
    tasks, notes, events = _read(guard)
    return summarize(tasks, notes, events, now=now, since=since)


def dumps(report):
    return json.dumps(report, ensure_ascii=False, indent=2)
