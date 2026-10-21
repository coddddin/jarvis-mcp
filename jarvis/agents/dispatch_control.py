"""W5 integration-4: operator control of the dispatcher (pause/resume) and a minimal status snapshot.

Pause is a workspace file, not an edit to the git-tracked config and not the global .jarvis-stop marker,
so pausing the dispatcher never stops the rest of JARVIS and never dirties the shared checkout. The
service reads it with dispatch_service.read_pause; this module is the only writer.
"""
import json
import os
import threading
import time

import psutil

from jarvis.agents.dispatch_policy import DispatchLedger, DispatcherConfig
from jarvis.agents.dispatch_service import PAUSE, read_pause
from jarvis.core.guardrails import GuardrailError

SERVICE_INFO = '.jarvis-dispatch/service.json'
OPERATOR = 'user:heejun'  # the HUD is operator-started and token-gated
RECENT = 10
_WRITE_LOCK = threading.Lock()  # HUD POSTs run in worker threads; a double click must not race
RUN_FIELDS = ('id', 'employee', 'status', 'started', 'ended', 'exit_code', 'input_tokens', 'output_tokens')


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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
    """Observed, not self-reported: the recorded pid must be alive and started no later than the record."""
    path = guard.validate_path(SERVICE_INFO, must_exist=False)
    try:
        info = json.loads(path.read_text(encoding='utf-8'))
        pid, started = info.get('pid'), _number(info.get('started'))
    except (OSError, ValueError, AttributeError):
        return {'running': False, 'pid': None, 'started': None}
    if type(pid) is not int or started is None:
        return {'running': False, 'pid': None, 'started': None}
    try:
        running = lookup(pid).create_time() <= started + 5  # a reused pid starts later
    except (psutil.Error, OSError):
        running = False
    return {'running': running, 'pid': pid if running else None, 'started': started}


def snapshot(guard, config_path, *, clock=time.time):
    """Whitelisted fields only: no log paths, notes, prompts or command lines."""
    config = DispatcherConfig.model_validate_json(config_path.read_bytes())
    pause_info = read_pause(guard)
    summary = DispatchLedger(guard).summary(recent=RECENT)
    recent = summary.pop('recent', [])
    employees = {name: {'runs': totals.get('runs') or 0,
                        'total_input_tokens': totals.get('total_input_tokens') or 0,
                        'total_output_tokens': totals.get('total_output_tokens') or 0,
                        'exhausted_until': _number(totals.get('exhausted_until'))}
                 for name, totals in summary.items()}
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
        'read_at': clock(),
    }


def _watch(guard, config_path):
    from jarvis.agents import process_watch  # imported here: process_watch imports this module
    try:
        return process_watch.scan(guard, config_path)
    except Exception:  # the panel still shows the rest when the watch cannot run
        return {'findings': [], 'checked_at': None, 'error': 'watch_unavailable'}
