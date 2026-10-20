"""W5 dispatcher: wake AI employees that have addressed work on the board.

One `tick` checks every employee, launches lean headless runs in per-employee git worktrees
(branch auto/<employee>), waits with a timeout, records usage in the ledger and pushes the branch.
It never merges to main; merges need user approval. `.jarvis-stop` or enabled=false stops it.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import psutil

from jarvis.agents.dispatch_policy import (DispatchLedger, DispatcherConfig, EmployeeState, build_command,
                                           detect_limit, due, parse_usage, wake_prompt)
from jarvis.agents.process_registry import observe_spawned
from jarvis.app.runtime import load_guardrails
from jarvis.core.paths import CONFIG_ROOT, PROJECT_ROOT
from jarvis.core.roster import load_roster
from jarvis.features.board.service import BoardStore

WORKTREES = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisWorktrees'
LOGS = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisDispatch' / 'logs'
DISPATCHER_ID = 'local:dispatcher'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def git(*args, cwd=PROJECT_ROOT, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', creationflags=NO_WINDOW)
    if check and result.returncode:
        raise RuntimeError(f'git {args[0]} failed: {result.stderr.strip()[:300]}')
    return result


def ensure_worktree(employee):
    """Per-employee worktree on auto/<employee>. Existing branches are never reset."""
    branch, path = f'auto/{employee}', WORKTREES / employee
    if not (path / '.git').exists():
        WORKTREES.mkdir(parents=True, exist_ok=True)
        exists = git('rev-parse', '--verify', '--quiet', branch, check=False).returncode == 0
        git('worktree', 'add', *( [str(path), branch] if exists else ['-b', branch, str(path), 'main']))
    # After the user merged earlier work, move the branch forward to main so work starts fresh.
    if git('merge-base', '--is-ancestor', branch, 'main', check=False).returncode == 0:
        git('merge', '--ff-only', 'main', cwd=path, check=False)
    return path


def executable(employee):
    if employee == 'claude-code':
        found = shutil.which('claude')
        return [found] if found else None
    if employee == 'codex':
        base = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI' / 'Codex' / 'bin'
        candidates = sorted(base.glob('*/codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True)
        return [str(candidates[0])] if candidates else None
    if employee == 'cursor':
        script = Path(os.environ.get('LOCALAPPDATA', '')) / 'cursor-agent' / 'cursor-agent.ps1'
        if script.is_file():
            return ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
    return None


def mcp_config():
    python, server = PROJECT_ROOT / '.venv' / 'Scripts' / 'python.exe', PROJECT_ROOT / 'server.py'
    workspace = os.environ.get('JARVIS_WORKSPACE', str(PROJECT_ROOT / 'JarvisWorkspace'))
    return json.dumps({'mcpServers': {'jarvis': {'command': str(python), 'args': [str(server)],
                       'env': {'JARVIS_WORKSPACE': workspace, 'JARVIS_DOWNLOADS_READONLY': '1'}}}})


def kill_tree(pid):
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.Error:
        pass


def run_employee(employee, run_id, config, guard, ledger, board):
    exe = executable(employee)
    if exe is None:
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                      note='executable not found')
        return 'error'
    worktree = ensure_worktree(employee)
    # Tests run with the project's interpreter by absolute path; a .venv junction would trip link guards.
    python = (PROJECT_ROOT / '.venv' / 'Scripts' / 'python.exe').as_posix()
    argv = build_command(employee, config, worktree, wake_prompt(employee, python), mcp_config(), executable=exe[0],
                         git_dir=PROJECT_ROOT / '.git', python=python)
    argv = exe + argv[1:]
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f'{employee}-{run_id}.log'
    timeout = config.run_timeout_minutes * 60
    with log_path.open('w', encoding='utf-8', errors='replace') as log:
        process = subprocess.Popen(argv, cwd=worktree, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=NO_WINDOW)
        ledger.set_pid(run_id, process.pid)
        observe_spawned(guard, process, kind='worker', label=f'dispatch {employee}', timeout_seconds=timeout)
        deadline, status = time.monotonic() + timeout, None
        while process.poll() is None:
            if (guard.workspace / '.jarvis-stop').exists():
                kill_tree(process.pid)
                status = 'stopped'
                break
            if time.monotonic() > deadline:
                kill_tree(process.pid)
                status = 'timeout'
                break
            time.sleep(2)
        process.wait()
    output = log_path.read_text(encoding='utf-8', errors='replace')
    tokens_in, tokens_out = parse_usage(output)
    failed = process.returncode != 0 or '"is_error":true' in output.replace(' ', '')
    if status is None:
        status = ('limit' if detect_limit(output) else 'error') if failed else 'ok'
    ledger.finish(run_id, status=status, exit_code=process.returncode, input_tokens=tokens_in,
                  output_tokens=tokens_out, note=log_path.name)
    branch = f'auto/{employee}'
    ahead = git('rev-list', '--count', f'main..{branch}', check=False).stdout.strip()
    if ahead not in ('', '0'):
        git('push', '-q', 'origin', branch, cwd=worktree, check=False)
    if status != 'ok':
        board.send(DISPATCHER_ID, 'user:heejun',
                   f'[알림] 디스패처: {employee} 실행 {status} (run {run_id}, exit {process.returncode}). 로그 {log_path.name}')
    return status


def tick(dry_run=False):
    guard = load_guardrails()
    guard.check_running()
    config = DispatcherConfig.model_validate_json((CONFIG_ROOT / 'dispatcher.json').read_bytes())
    if not config.enabled:
        return {'status': 'disabled'}
    roster = load_roster(workspace=guard.workspace)
    ledger, board = DispatchLedger(guard), BoardStore(guard)
    now = time.time()
    states = []
    for employee, settings in config.employees.items():
        agent = roster.agents.get(employee)
        states.append(EmployeeState(
            employee=employee, enabled=settings.enabled, roster_status=agent.status if agent else 'missing',
            pending=board.pending_for_employee(employee) if agent else 0,
            runs_today=ledger.runs_today(employee), running=ledger.running(employee) is not None,
            exhausted_until=ledger.exhausted_until(employee)))
    decisions = due(states, now, daily_runs=config.daily_runs)
    report = {'decisions': [d.__dict__ for d in decisions], 'launched': {}}
    if dry_run:
        return report
    threads = []
    for decision in decisions:
        if not decision.wake:
            continue
        run_id = ledger.try_start(decision.employee, daily_runs=config.daily_runs)
        if run_id is None:
            continue
        def work(employee=decision.employee, run_id=run_id):
            try:
                report['launched'][employee] = run_employee(employee, run_id, config, guard, ledger, board)
            except Exception as exc:  # record, never crash the scheduler
                ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                              note=type(exc).__name__ + ': ' + str(exc)[:300])
                report['launched'][employee] = 'error'
        thread = threading.Thread(target=work)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    return report


def main():
    parser = argparse.ArgumentParser(description='자비스 W5 디스패처: 게시판에 일이 온 AI 직원을 깨운다. main 머지는 하지 않는다.')
    parser.add_argument('action', choices=['tick', 'dry-run', 'status'])
    args = parser.parse_args()
    try:
        if args.action == 'status':
            result = DispatchLedger(load_guardrails()).summary()
        else:
            result = tick(dry_run=args.action == 'dry-run')
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)[:300]},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
