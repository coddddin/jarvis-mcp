"""W3 alerts, stage 1 wiring: gather tick events from the board, adapter check and process watch, then
board_send new ones to the user once a day. Called from the dispatcher tick; never launches a process."""
from jarvis.agents import alerts, dispatch_control, process_watch
from jarvis.agents.dispatch_policy import DispatcherConfig
from jarvis.agents.dispatch_service import read_pause
from jarvis.agents.worker_adapters import employed, roster_problem_codes

USER = 'user:heejun'


def gather(guard, board, roster, config_path, *, not_woken=(), unchanged=()):
    stale = [task for task in board.list(status='claimed', limit=50)['tasks'] if task['stale']]
    blocked = board.list(status='blocked', limit=50)['tasks']
    working = set(employed(roster))
    problems = {name: codes for name, codes in roster_problem_codes(roster).items() if name in working}
    findings = process_watch.scan(guard, config_path)['findings']
    return alerts.events(stale=stale, blocked=blocked, problems=problems, not_woken=not_woken, findings=findings,
                         service=_service_status(guard, config_path), unchanged=unchanged)


def _service_status(guard, config_path):
    """Observed service status, or None when the dispatcher is off or paused (nothing to warn about)."""
    if not DispatcherConfig.model_validate_json(config_path.read_bytes()).enabled:
        return None
    try:
        if read_pause(guard)['paused']:
            return None
    except Exception:  # an unreadable switch counts as paused, as in process_watch
        return None
    return dispatch_control.service_state(guard)['status']


def send_alerts(guard, board, roster, config_path, now, *, sender, not_woken=(), unchanged=(), ledger=None):
    ledger = ledger or alerts.AlertLedger(guard)
    found = gather(guard, board, roster, config_path, not_woken=not_woken, unchanged=unchanged)
    return alerts.notify(ledger, found, lambda text: board.send(sender, USER, text), now)
