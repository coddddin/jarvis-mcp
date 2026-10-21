"""W4-5 work evaluation: per-employee counts from the shared board, read-only.

The board keeps only each task's current status and its notes, not a status history, so every figure
here is an approximation (see DEFINITION). Sessions are merged: 'claude-code:main' and
'claude-code:second' both count for 'claude-code'. Nothing is written to the board.
"""
import json
import sqlite3
from statistics import mean, median

from jarvis.features.board.service import STATUSES, BoardStore

DEFINITION = ('approximate: task goes to the current owner employee (creator if unowned); done = status done; '
              'findings = finding notes by another agent ID; reworked = owner handoff after such a finding; '
              'lead time = created to last owner handoff of done tasks (claim time and status history are not recorded).')


class EvaluationError(ValueError):
    """The board could not be read for evaluation."""


def employee(agent):
    return agent.partition(':')[0] if isinstance(agent, str) else ''


def _empty():
    return {'tasks': 0, **{status: 0 for status in STATUSES}, 'stale': 0,
            'findings_received': 0, 'reworked_tasks': 0, 'lead_hours': []}


def _read(guard):
    """(tasks, notes) rows from the board database opened read-only; ([], []) when no board exists yet."""
    guard.check_running()
    path = guard.validate_path(BoardStore.DATABASE, must_exist=False)
    if not path.exists():
        return [], []
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
        return tasks, notes
    except sqlite3.Error as exc:
        raise EvaluationError('Board storage unavailable.') from exc
    finally:
        if db is not None:
            db.close()


def summarize(tasks, notes, *, now, since=None):
    """Per-employee report from board rows. since (epoch seconds) keeps tasks created at or after it."""
    by_task = {}
    for note in notes:
        by_task.setdefault(note['task_id'], []).append(note)
    employees = {}
    for task in tasks:
        if since is not None and task['created'] < since:
            continue
        holder = task['owner'] or task['creator']
        name = employee(holder)
        if not name:
            continue
        row = employees.setdefault(name, _empty())
        row['tasks'] += 1
        if task['status'] in STATUSES:
            row[task['status']] += 1
        if task['status'] == 'claimed' and (task['lease_until'] or 0) <= now:
            row['stale'] += 1
        findings = [note for note in by_task.get(task['id'], [])
                    if note['kind'] == 'finding' and note['author'] != holder]
        handoffs = [note for note in by_task.get(task['id'], [])
                    if note['kind'] == 'handoff' and note['author'] == holder]
        row['findings_received'] += len(findings)
        if findings and handoffs and handoffs[-1]['id'] > findings[0]['id']:
            row['reworked_tasks'] += 1
        if task['status'] == 'done' and handoffs:
            row['lead_hours'].append(max(0.0, handoffs[-1]['created'] - task['created']) / 3600)
    for row in employees.values():
        hours = row.pop('lead_hours')
        row['done_lead_hours'] = {'count': len(hours),
                                  'mean': round(mean(hours), 2) if hours else None,
                                  'median': round(median(hours), 2) if hours else None}
    return {'approximate': True, 'definition': DEFINITION, 'generated_at': int(now),
            'since': int(since) if since is not None else None,
            'employees': dict(sorted(employees.items()))}


def evaluate(guard, *, now, since=None):
    tasks, notes = _read(guard)
    return summarize(tasks, notes, now=now, since=since)


def dumps(report):
    return json.dumps(report, ensure_ascii=False, indent=2)
