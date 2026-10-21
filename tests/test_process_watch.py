"""W2 process watch: report orphaned dispatcher records and overdue runs, never terminate or edit."""
import json
import os

import psutil
import pytest

from jarvis.agents import process_watch
from jarvis.agents.dispatch_policy import DispatchLedger
from jarvis.core.guardrails import Guardrails

NOW = 1_800_000_000.0


@pytest.fixture
def guard(tmp_path):
    (tmp_path / 'ws').mkdir()
    return Guardrails(tmp_path / 'ws')


@pytest.fixture
def config(tmp_path):
    path = tmp_path / 'dispatcher.json'
    path.write_text(json.dumps({'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 10,
                                'run_timeout_minutes': 30, 'employees': {'codex': {'enabled': True}}}),
                    encoding='utf-8')
    return path


class Proc:
    def __init__(self, created):
        self._created = created

    def create_time(self):
        return self._created


def lookup_for(table):
    """pid -> creation time; missing pid behaves like a dead process."""
    def lookup(pid):
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        return Proc(table[pid])
    return lookup


def run(guard, *, started, pid):
    ledger = DispatchLedger(guard, clock=lambda: started)
    return ledger.start('codex', pid=pid)


def kinds(report):
    return sorted((item['kind'], item['ref']) for item in report['findings'])


def scan(guard, config, table, now=NOW):
    return process_watch.scan(guard, config, now=now, lookup=lookup_for(table))


def write_service(guard, pid, started):
    folder = guard.workspace / '.jarvis-dispatch'
    folder.mkdir(exist_ok=True)
    (folder / 'service.json').write_text(json.dumps({'pid': pid, 'started': started}), encoding='utf-8')


def test_healthy_state_has_no_findings(guard, config):
    write_service(guard, 100, NOW - 3600)
    run_id = run(guard, started=NOW - 60, pid=200)
    report = scan(guard, config, {100: NOW - 3601, 200: NOW - 61})
    assert report['findings'] == [] and report['checked_at'] == NOW and run_id


def test_dead_or_reused_owner_is_reported(guard, config):
    write_service(guard, 100, NOW - 3600)
    dead = run(guard, started=NOW - 60, pid=201)
    reused = run(guard, started=NOW - 600, pid=202)  # pid alive but created long after the run started
    report = scan(guard, config, {100: NOW - 3601, 202: NOW - 10})
    assert kinds(report) == [('owner_dead', f'run:{dead}'), ('owner_dead', f'run:{reused}')]


def test_missing_pid_is_reported_only_after_a_grace_period(guard, config):
    write_service(guard, 100, NOW - 3600)
    fresh = run(guard, started=NOW - 10, pid=None)
    old = run(guard, started=NOW - 600, pid=None)
    report = scan(guard, config, {100: NOW - 3601})
    assert kinds(report) == [('owner_unknown', f'run:{old}')]
    assert fresh


def test_overdue_run_is_reported_even_if_alive(guard, config):
    write_service(guard, 100, NOW - 7200)
    late = run(guard, started=NOW - 30 * 60 - 121, pid=300)
    report = scan(guard, config, {100: NOW - 7201, 300: NOW - 30 * 60 - 122})
    assert kinds(report) == [('overdue', f'run:{late}')]
    assert report['findings'][0]['detail']['limit_seconds'] == 30 * 60


def test_service_down_and_stale_record(guard, config):
    assert kinds(scan(guard, config, {})) == [('service_down', 'service')]
    write_service(guard, 100, NOW - 3600)
    assert kinds(scan(guard, config, {})) == [('service_down', 'service'), ('service_record_stale', 'service')]


def test_paused_or_disabled_service_is_not_down(guard, config):
    from jarvis.agents import dispatch_control
    dispatch_control.pause(guard)
    assert scan(guard, config, {})['findings'] == []
    dispatch_control.resume(guard)
    data = json.loads(config.read_text(encoding='utf-8'))
    data['enabled'] = False
    config.write_text(json.dumps(data), encoding='utf-8')
    assert scan(guard, config, {})['findings'] == []


def test_scan_is_read_only(guard, config):
    write_service(guard, 100, NOW - 3600)
    run_id = run(guard, started=NOW - 60, pid=201)
    scan(guard, config, {100: NOW - 3601})
    assert DispatchLedger(guard).summary()['recent'][0]['status'] == 'running'
    assert run_id


def test_findings_expose_whitelisted_fields(guard, config):
    run(guard, started=NOW - 60, pid=201)
    for item in scan(guard, config, {})['findings']:
        assert set(item) == {'kind', 'ref', 'since', 'detail'}
        assert 'note' not in json.dumps(item) and 'log' not in json.dumps(item)


def test_real_process_lookup_by_default(guard, config):
    write_service(guard, os.getpid(), psutil.Process(os.getpid()).create_time() + 1)
    report = process_watch.scan(guard, config, now=NOW)
    assert ('service_down', 'service') not in kinds(report)


def test_slow_worktree_setup_is_not_mistaken_for_a_reused_pid(guard, config):
    # The ledger's 'started' is written at try_start, before the worktree is ready; the employee
    # process can be created well after it (review: +10 s was misread as owner_dead).
    write_service(guard, 100, NOW - 3600)
    run(guard, started=NOW - 60, pid=400)
    assert scan(guard, config, {100: NOW - 3601, 400: NOW - 50})['findings'] == []
    # Far beyond the setup grace it is still a reused pid.
    late = run(guard, started=NOW - 600, pid=401)
    assert kinds(scan(guard, config, {100: NOW - 3601, 400: NOW - 50, 401: NOW - 600 + 121 + 1})) == \
        [('owner_dead', f'run:{late}')]


def test_pid_at_tightens_the_reused_pid_rule(guard, config):
    write_service(guard, 100, NOW - 3600)
    ledger = DispatchLedger(guard, clock=lambda: NOW - 60)
    run_id = ledger.start('codex', pid=None)
    DispatchLedger(guard, clock=lambda: NOW - 50).set_pid(run_id, 500)
    assert scan(guard, config, {100: NOW - 3601, 500: NOW - 51})['findings'] == []      # created before pid_at
    assert kinds(scan(guard, config, {100: NOW - 3601, 500: NOW - 44})) == [('owner_dead', f'run:{run_id}')]


def test_access_denied_is_not_reported_as_dead(guard, config, monkeypatch):
    write_service(guard, os.getpid(), psutil.Process(os.getpid()).create_time() + 1)
    run(guard, started=NOW - 60, pid=600)

    def lookup(pid):
        if pid == 600:
            raise psutil.AccessDenied(pid)
        return psutil.Process(pid)
    assert process_watch.scan(guard, config, now=NOW, lookup=lookup)['findings'] == []
