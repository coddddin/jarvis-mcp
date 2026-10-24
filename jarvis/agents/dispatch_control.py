"""W5 integration-4: operator control of the dispatcher (pause/resume) and a minimal status snapshot.

Pause is a workspace file, not an edit to the git-tracked config and not the global .jarvis-stop marker,
so pausing the dispatcher never stops the rest of JARVIS and never dirties the shared checkout. The
service reads it with dispatch_service.read_pause; this module is the only writer.
"""
import json
import math
import os
import threading
import time

import psutil

from jarvis.agents.dispatch_policy import USAGE_QUALITIES, DispatchLedger, DispatcherConfig
from jarvis.agents.dispatch_service import PAUSE, read_pause
from jarvis.core.guardrails import GuardrailError

SERVICE_INFO = '.jarvis-dispatch/service.json'
OPERATOR = 'user:heejun'  # the HUD is operator-started and token-gated
RECENT = 10
_WRITE_LOCK = threading.Lock()  # HUD POSTs run in worker threads; a double click must not race
RUN_FIELDS = ('id', 'employee', 'status', 'started', 'ended', 'exit_code', 'input_tokens', 'output_tokens',
              'usage_quality')
QUALITY_KEYS = USAGE_QUALITIES + ('unrecorded',)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _usage_quality(totals):
    """Run counts per usage quality and the known token sums, beside the old totals that show unread as 0.
    A known sum is None when no run reported that side. 'measured' only means both reported numbers were read,
    not that the whole run was observed; a Cursor cache 0 may mean not reported."""
    counts = totals.get('usage_quality_runs') or {}
    return {'usage_quality_runs': {key: _number(counts.get(key)) or 0 for key in QUALITY_KEYS},
            'known_input_tokens': _number(totals.get('known_input_tokens')),
            'known_input_runs': _number(totals.get('known_input_runs')) or 0,
            'known_output_tokens': _number(totals.get('known_output_tokens')),
            'known_output_runs': _number(totals.get('known_output_runs')) or 0}


def _write_pause(guard, paused, reason, clock):
    guard.check_running()
    if not isinstance(reason, str) or len(reason) > 200:
        raise ValueError('Reason must be text of at most 200 characters.')
    body = json.dumps({'paused': paused, 'by': OPERATOR, 'at': clock(), 'reason': reason}, ensure_ascii=False)
    with _WRITE_LOCK:  # also covers validate_path, which misreads a file another write is swapping in
        path = guard.validate_path(PAUSE, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise GuardrailError('Linked dispatcher pause marker is forbidden.')
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(body, encoding='utf-8')
        for attempt in range(7):  # backoff 20ms..640ms, about 1.3s in total
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                # Windows refuses the swap while the service is reading the marker; that read is brief.
                if attempt == 6:
                    temp.unlink(missing_ok=True)
                    raise
                time.sleep(0.02 * 2 ** attempt)


def pause(guard, reason='', *, clock=time.time):
    _write_pause(guard, True, reason, clock)


def resume(guard, reason='', *, clock=time.time):
    # An explicit 'paused: false' (not a delete) also replaces a corrupt marker the service treats as paused.
    _write_pause(guard, False, reason, clock)


def service_state(guard, *, lookup=psutil.Process):
    """Process-record observation only; config enabled and worker success are separate facts.

    running stays boolean for older clients. New clients must read status: False can mean
    not registered, stopped, identity mismatch or unknown. The record is not authenticated.
    """
    def valid_time(value):
        try:
            return _number(value) is not None and value > 0 and math.isfinite(value)
        except OverflowError:
            return False

    def result(status, reason, *, pid=None, started=None):
        return {'running': status == 'running', 'pid': pid if status == 'running' else None,
                'started': started, 'status': status, 'reason': reason, 'source': 'observed'}

    path = guard.validate_path(SERVICE_INFO, must_exist=False)
    try:
        if path.stat().st_size > 16384:
            return result('unknown', 'invalid_record')
        info = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return result('not_registered', 'missing_record')
    except (OSError, UnicodeError):
        return result('unknown', 'unreadable_record')
    except ValueError:
        return result('unknown', 'invalid_record')
    if not isinstance(info, dict):
        return result('unknown', 'invalid_record')
    pid, started = info.get('pid'), _number(info.get('started'))
    if type(pid) is not int or pid <= 0 or not valid_time(started):
        return result('unknown', 'invalid_record')
    try:
        created = lookup(pid).create_time()
    except psutil.NoSuchProcess:
        return result('stopped', 'process_missing', started=started)
    except (psutil.Error, OSError):
        return result('unknown', 'process_unreadable', started=started)
    if not valid_time(created):
        return result('unknown', 'process_unreadable', started=started)
    if created > started + 5:
        return result('identity_mismatch', 'pid_reused', started=started)
    return result('running', 'matching_pid', pid=pid, started=started)


def snapshot(guard, config_path, *, clock=time.time):
    """Whitelisted fields only: no log paths, notes, prompts or command lines."""
    config = DispatcherConfig.model_validate_json(config_path.read_bytes())
    pause_info = read_pause(guard)
    ledger = DispatchLedger(guard)
    summary = ledger.summary(recent=RECENT)
    recent = summary.pop('recent', [])
    employees = {name: {'runs': totals.get('runs') or 0,
                        'total_input_tokens': totals.get('total_input_tokens') or 0,
                        'total_output_tokens': totals.get('total_output_tokens') or 0,
                        'exhausted_until': _number(totals.get('exhausted_until')),
                        **_usage_quality(totals)}
                 for name, totals in summary.items()}
    # A rotating employee waits when every model is backed off (or its own eligibility backoff runs), the same
    # wake rule the tick uses; the HUD shows it with the existing exhausted_until line.
    for name, settings in config.employees.items():
        if settings.models and name in employees:
            employees[name]['exhausted_until'] = ledger.rotation_exhausted_until(name, settings.models)
    return {
        'config_enabled': config.enabled,
        'paused': pause_info['paused'],
        'paused_at': pause_info.get('at') if pause_info['paused'] else None,
        'pause_reason': pause_info.get('reason') if pause_info['paused'] else None,
        'effective_enabled': config.enabled and not pause_info['paused'],
        'interval_minutes': config.interval_minutes,
        'daily_runs': config.daily_runs,
        'service': service_state(guard),
        'employees': employees,
        'recent': [{key: row.get(key) for key in RUN_FIELDS} for row in recent[:RECENT]],
        'watch': _watch(guard, config_path),
        'usage': _usage(guard),
        'alerts': _alerts(guard, clock()),
        'read_at': clock(),
    }


def _usage(guard):
    """Last observed 5-hour and weekly usage per employee, display only (W3)."""
    from jarvis.agents import usage_collector
    try:
        return usage_collector.usage_overview(guard)
    except Exception:
        return {}


def _alerts(guard, now):
    from jarvis.agents import alerts  # imported here like the watch: keeps this module's import graph small
    try:
        return {'today': alerts.read_today(guard, now)}
    except Exception:  # the panel still shows the rest when the alert record cannot be read
        return {'today': [], 'error': 'alerts_unavailable'}


def _watch(guard, config_path):
    from jarvis.agents import process_watch  # imported here: process_watch imports this module
    try:
        return process_watch.scan(guard, config_path)
    except Exception:  # the panel still shows the rest when the watch cannot run
        return {'findings': [], 'checked_at': None, 'error': 'watch_unavailable'}
