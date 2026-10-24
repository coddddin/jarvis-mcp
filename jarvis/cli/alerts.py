"""W3 alerts, stage 1 wiring: gather tick events from the board, adapter check and process watch, then
board_send new ones to the user once a day. Called from the dispatcher tick; never launches a process."""
from jarvis.agents import alerts, process_watch
from jarvis.agents.worker_adapters import employed, roster_problem_codes

USER = 'user:heejun'


def gather(guard, board, roster, config_path, *, not_woken=()):
    stale = [task for task in board.list(status='claimed', limit=50)['tasks'] if task['stale']]
    blocked = board.list(status='blocked', limit=50)['tasks']
    working = set(employed(roster))
    problems = {name: codes for name, codes in roster_problem_codes(roster).items() if name in working}
    findings = process_watch.scan(guard, config_path)['findings']
    return alerts.events(stale=stale, blocked=blocked, problems=problems, not_woken=not_woken, findings=findings)


def send_alerts(guard, board, roster, config_path, now, *, sender, not_woken=(), ledger=None):
    ledger = ledger or alerts.AlertLedger(guard)
    found = gather(guard, board, roster, config_path, not_woken=not_woken)
    return alerts.notify(ledger, found, lambda text: board.send(sender, USER, text), now)
