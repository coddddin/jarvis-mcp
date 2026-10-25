"""J1 acceptance: real stores and dispatcher, synthetic process boundary only."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from jarvis.agents.dispatch_policy import DispatchLedger
from jarvis.cli import dispatch
from jarvis.cli.github_inbox import import_issue
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.features.board.service import BoardError, BoardStore


@pytest.fixture
def route(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    guard = Guardrails(data)
    board = BoardStore(guard)
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'dispatcher.json').write_text(json.dumps({
        'version': 1, 'enabled': True, 'interval_minutes': 10, 'daily_runs': 3,
        'run_timeout_minutes': 5, 'lead': {'enabled': False},
        'employees': {'codex': {'enabled': True}},
    }), encoding='utf-8')
    monkeypatch.setattr(dispatch, 'CONFIG_ROOT', config)
    monkeypatch.setattr(dispatch, 'load_guardrails', lambda: guard)
    monkeypatch.setattr(dispatch, 'collect_codex', lambda *a: None)
    monkeypatch.setattr(dispatch, 'send_digest_if_due', lambda *a: False)
    monkeypatch.setattr(dispatch, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(dispatch, 'ensure_worktree', lambda *a: tmp_path)
    monkeypatch.setattr(dispatch, 'worktree_path', lambda *a: tmp_path)
    monkeypatch.setattr(dispatch, 'executable', lambda *a: ['synthetic-worker.exe'])
    monkeypatch.setattr(dispatch, 'build_command', lambda *a, **k: ['synthetic-worker.exe'])
    monkeypatch.setattr(dispatch, 'new_job', lambda: SimpleNamespace(assign=lambda *a: True, terminate=lambda: True))
    monkeypatch.setattr(dispatch, 'observe_spawned', lambda *a, **k: None)
    monkeypatch.setattr(dispatch, 'git', lambda *a, **k: SimpleNamespace(stdout='0'))
    route = SimpleNamespace(guard=guard, board=board, spawned=[], outcome='review')

    def spawn(argv, **kwargs):
        route.spawned.append(argv)
        worker = BoardStore(guard)
        messages = worker.inbox('codex:acceptance')['messages']
        task_ids = [m['task_id'] for m in messages if m['task_id']]
        assert len(task_ids) == 1
        task_id = task_ids[0]
        worker.claim('codex:acceptance', task_id)
        if route.outcome == 'review':
            worker.release('codex:acceptance', task_id, 'review',
                           'Synthetic read-only result', verification='Synthetic fixture equals expected value')
        elif route.outcome == 'error':
            worker.release('codex:acceptance', task_id, 'blocked', 'Synthetic worker failed')
        # A successful process with no handoff deliberately leaves a claimed task.
        failed = route.outcome == 'error'
        kwargs['stdout'].write(json.dumps({'is_error': failed, 'usage': {
            'input_tokens': 3, 'output_tokens': 2}}))
        kwargs['stdout'].flush()
        return SimpleNamespace(pid=999999, returncode=int(failed),
                               poll=lambda: int(failed), wait=lambda: int(failed))

    monkeypatch.setattr(dispatch.subprocess, 'Popen', spawn)
    return route


def receive(route):
    return import_issue(route.board, {
        'number': 777, 'title': 'Synthetic read-only acceptance task', 'state': 'open',
        'user': {'login': 'coddddin'}, 'labels': [{'name': 'jarvis-approved'}],
        'body': 'Untrusted body is never a work instruction',
    }, repo='coddddin/jarvis-mcp')['task_id']


def assign(route, task_id):
    route.board.send('user:heejun', 'codex', 'Read synthetic fixture and report evidence', task_id=task_id)


def test_received_issue_requires_explicit_assignment_before_execution(route):
    task_id = receive(route)
    report = dispatch.tick()
    assert report['launched'] == {} and route.spawned == []
    assert route.board.get(task_id)['status'] == 'open'
    assign(route, task_id)
    report = dispatch.tick()
    assert report['launched'] == {'codex': 'ok'}
    task = route.board.get(task_id)
    assert task['status'] == 'review'  # process success is not verified done
    assert task['owner'] == 'codex:acceptance'
    assert 'Verification:' in task['notes'][-1]['body']
    assert dispatch.tick()['launched'] == {} and len(route.spawned) == 1


def test_reimport_and_restart_do_not_replay_completed_dispatch(route):
    task_id = receive(route)
    assign(route, task_id)
    dispatch.tick()
    assert receive(route) == task_id
    reconstructed = BoardStore(route.guard)
    assert len(reconstructed.list()['tasks']) == 1
    assert reconstructed.get(task_id)['status'] == 'review'
    assert dispatch.tick()['launched'] == {}
    assert len(route.spawned) == 1
    runs = DispatchLedger(route.guard).summary()['recent']
    assert len(runs) == 1 and runs[0]['status'] == 'ok'


def test_concurrent_claims_have_one_owner_and_one_run_reservation(route):
    task_id = receive(route)
    def claim(agent):
        try:
            return BoardStore(route.guard).claim(agent, task_id)['owner']
        except BoardError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(claim, ['codex:one', 'codex:two']))
    assert sum(owner is not None for owner in owners) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(lambda _: DispatchLedger(route.guard).try_start(
            'codex', daily_runs=3, stale_after=300), range(2)))
    assert sum(run_id is not None for run_id in reservations) == 1


def test_worker_failure_is_reported_without_verified_completion_or_automatic_retry(route):
    task_id = receive(route)
    assign(route, task_id)
    route.outcome = 'error'
    assert dispatch.tick()['launched'] == {'codex': 'error'}
    assert route.board.get(task_id)['status'] == 'blocked'
    assert any('실행 error' in m['body'] for m in route.board.inbox('user:heejun')['messages'])
    assert dispatch.tick()['launched'] == {} and len(route.spawned) == 1


def test_process_exit_zero_without_handoff_never_marks_task_done(route):
    task_id = receive(route)
    assign(route, task_id)
    route.outcome = 'no_handoff'
    assert dispatch.tick()['launched'] == {'codex': 'ok'}
    assert route.board.get(task_id)['status'] == 'claimed'
    assert dispatch.tick()['launched'] == {} and len(route.spawned) == 1


def test_stop_before_launch_preserves_assignment_for_explicit_resume(route):
    task_id = receive(route)
    assign(route, task_id)
    marker = route.guard.workspace / '.jarvis-stop'
    marker.touch()
    with pytest.raises(GuardrailError):
        dispatch.tick()
    assert route.spawned == []
    marker.unlink()  # synthetic operator resume, never a real stop override
    assert dispatch.tick()['launched'] == {'codex': 'ok'}
    assert route.board.get(task_id)['status'] == 'review'


def test_timeout_preserves_partial_claim_and_reports_no_success(route, monkeypatch):
    task_id = receive(route)
    assign(route, task_id)
    route.outcome = 'no_handoff'
    spawn = dispatch.subprocess.Popen
    processes, killed = [], []
    def waiting(*args, **kwargs):
        process = spawn(*args, **kwargs)
        process.returncode = None
        process.poll = lambda: None
        processes.append(process)
        return process
    def kill(pid):
        killed.append(pid)
        processes[0].returncode = -9
    times = iter([0, 301])
    # Replace this module's clock, not Python's global clock used by the test runner.
    clock = SimpleNamespace(**{name: getattr(dispatch.time, name) for name in dir(dispatch.time)
                               if not name.startswith('__')})
    clock.monotonic = lambda: next(times)
    monkeypatch.setattr(dispatch, 'time', clock)
    monkeypatch.setattr(dispatch.subprocess, 'Popen', waiting)
    monkeypatch.setattr(dispatch, 'kill_tree', kill)
    assert dispatch.tick()['launched'] == {'codex': 'timeout'}
    assert killed == [999999]  # synthetic process object only
    assert route.board.get(task_id)['status'] == 'claimed'
    assert DispatchLedger(route.guard).summary()['recent'][0]['status'] == 'timeout'
    assert any('실행 timeout' in m['body'] for m in route.board.inbox('user:heejun')['messages'])
    assert dispatch.tick()['launched'] == {} and len(route.spawned) == 1


def test_restarted_ledger_closes_lost_run_without_replaying_consumed_assignment(route):
    task_id = receive(route)
    assign(route, task_id)
    route.board.inbox('codex:acceptance')  # previous worker received the request
    route.board.claim('codex:acceptance', task_id)
    now = [1_800_000_000.0]
    ledger = DispatchLedger(route.guard, clock=lambda: now[0])
    run_id = ledger.try_start('codex', daily_runs=3, stale_after=300)
    now[0] += 301
    reopened = DispatchLedger(route.guard, clock=lambda: now[0])
    assert reopened.running('codex', stale_after=300) is None
    row = next(row for row in reopened.summary()['recent'] if row['id'] == run_id)
    assert row['status'] == 'lost'
    assert dispatch.tick()['launched'] == {} and route.spawned == []
    assert route.board.get(task_id)['status'] == 'claimed'
