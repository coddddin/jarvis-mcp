"""W2 process watch, stage 1: find dispatcher records whose owner process is gone and runs past their limit.

Scope is the dispatcher only (its ledger's running rows and .jarvis-dispatch/service.json). General run
observation belongs to the activity collector. This module reports; it never terminates a process and
never edits the ledger (stale rows are still closed as 'lost' by the dispatch path).
"""
import time

import psutil

from jarvis.agents import dispatch_control
from jarvis.agents.dispatch_policy import DispatchLedger, DispatcherConfig, owner_deadline
from jarvis.agents.dispatch_service import read_pause

PID_GRACE_SECONDS = 120     # a run gets its pid right after spawn; only a long gap is suspicious
OVERDUE_GRACE_SECONDS = 120  # the dispatcher itself kills at the limit; allow it time to finish
MAX_FINDINGS = 50
# Reused-pid rule is shared with the ledger (dispatch_policy.owner_deadline): creation by pid_at + 5 s, or
# started + 120 s for rows written before pid_at existed.


def _finding(kind, ref, since, **detail):
    return {'kind': kind, 'ref': ref, 'since': since, 'detail': detail}


def _owner_alive(pid, pid_at, started, lookup):
    try:
        return lookup(pid).create_time() <= owner_deadline(pid_at, started)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.Error, OSError):  # access denied: a doubt, as in the ledger, is not a finding
        return True


def _running_rows(guard):
    ledger = DispatchLedger(guard)
    with ledger.connection() as db:
        return [dict(row) for row in db.execute(
            'SELECT id, employee, pid, pid_at, started FROM runs WHERE status="running" ORDER BY id LIMIT ?',
            (MAX_FINDINGS,))]


def scan(guard, config_path, *, now=None, lookup=psutil.Process):
    """{'findings': [...], 'checked_at': epoch}. Findings: service_down, service_record_stale, owner_dead,
    owner_unknown, overdue. Fields are whitelisted: no command lines, paths, notes or log names."""
    now = time.time() if now is None else now
    config = DispatcherConfig.model_validate_json(config_path.read_bytes())
    limit = config.run_timeout_minutes * 60
    findings = []

    service = dispatch_control.service_state(guard, lookup=lookup)
    if service['status'] in ('stopped', 'identity_mismatch'):
        try:
            paused = read_pause(guard)['paused']
        except Exception:  # an unreadable switch counts as paused, as in the service loop
            paused = True
        if config.enabled and not paused:
            findings.append(_finding('service_down', 'service', None))
        if service['started'] is not None:
            findings.append(_finding('service_record_stale', 'service', service['started']))

    for row in _running_rows(guard):
        ref, started, pid = f"run:{row['id']}", row['started'], row['pid']
        if pid is None:
            if now - started > PID_GRACE_SECONDS:
                findings.append(_finding('owner_unknown', ref, started, employee=row['employee']))
        elif not _owner_alive(pid, row['pid_at'], started, lookup):
            findings.append(_finding('owner_dead', ref, started, employee=row['employee'], pid=pid))
            continue  # a dead owner is the root cause; do not also report overdue
        if now - started > limit + OVERDUE_GRACE_SECONDS:
            findings.append(_finding('overdue', ref, started, employee=row['employee'],
                                     elapsed_seconds=round(now - started), limit_seconds=limit))
    report = {'findings': findings[:MAX_FINDINGS], 'checked_at': now}
    if service['status'] == 'unknown':
        report['error'] = 'service_observation_unavailable'
    return report
