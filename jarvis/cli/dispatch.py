"""W5 dispatcher: wake AI employees that have addressed work on the board.

One `tick` checks every employee, launches lean headless runs in per-employee git worktrees
(branch auto/<employee>), waits with a timeout, records usage in the ledger and pushes the branch.
It never merges to main; merges need user approval. `.jarvis-stop` or enabled=false stops it.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil

from jarvis.agents.dispatch_policy import (DispatchLedger, DispatcherConfig, EmployeeState, block_unsafe, build_command,
                                           build_env, build_digest, detect_eligibility, detect_limit, digest_due, due,
                                           parse_usage, should_run_lead, wake_prompt)
from jarvis.agents.lead_planner import LeadLedger, plan_next
from jarvis.agents import process_watch
from jarvis.agents import promotion as promotion_review
from jarvis.agents.dispatch_service import DispatchService, KillOnCloseJob, codex_workload_gate, read_pause, read_usage_sample
from jarvis.agents.process_registry import ProcessRegistry, observe_spawned
from jarvis.agents.usage_collector import collect_codex, usage_overview
from jarvis.agents.worker_adapters import ADAPTERS, employed, roster_problem_codes, roster_problems
from jarvis.agents.dispatch_policy import Decision
from jarvis.app.runtime import load_guardrails
from jarvis.cli.alerts import send_alerts
from jarvis.core.paths import CONFIG_ROOT, PROJECT_ROOT
from jarvis.core.roster import load_roster
from jarvis.features.board.service import BoardStore
from jarvis.features.evaluation.service import evaluate

WORKTREES = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisWorktrees'
LOGS = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisDispatch' / 'logs'
DISPATCHER_ID = 'local:dispatcher'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_JOB = None


def job():
    """One kill-on-close job per dispatcher process: employees die if the dispatcher dies."""
    global _JOB
    if _JOB is None:
        _JOB = KillOnCloseJob()
    return _JOB


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
    """argv prefix that starts the employee's CLI in work mode, or None (discovery lives in worker_adapters)."""
    item = ADAPTERS.get(employee)
    return item.discover_work() if item else None


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
        workspace = os.environ.get('JARVIS_WORKSPACE', str(PROJECT_ROOT / 'JarvisWorkspace'))
        env = {**os.environ, **build_env(employee, project_root=PROJECT_ROOT, workspace=workspace)}
        process = subprocess.Popen(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=NO_WINDOW)
        if not job().assign(process) and os.name == 'nt':  # bind before anything else can spawn
            log_service_error(RuntimeError(f'{employee} run {run_id} not bound to kill-on-close job'))
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
        # A free-tier refusal can arrive as an error event with exit code 0, so check it first.
        status = ('eligibility' if detect_eligibility(output)
                  else ('limit' if detect_limit(output) else 'error') if failed else 'ok')
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


def log_service_error(exc):
    """The service runs windowless (pythonw), so errors go to a log file instead of stderr."""
    try:
        _write_service_log(exc)
    except Exception:  # logging must never stop the service
        pass


def _write_service_log(exc):
    LOGS.mkdir(parents=True, exist_ok=True)
    line = json.dumps({'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'error': type(exc).__name__,
                       'detail': str(exc)[:300]}, ensure_ascii=False)
    with (LOGS / 'service.log').open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')


def load_judge():
    """The lead's local-model judge (claude-code:second, jarvis/agents/lead_judge.py). Absent: lead stays idle."""
    try:
        from jarvis.agents.lead_judge import ollama_judge
    except ImportError:
        return None
    return ollama_judge


def run_lead(guard, board, roster, config, now):
    judge = load_judge()
    if judge is None:
        return {'action': 'none', 'reason': 'no_judge'}
    try:
        plan_text = (PROJECT_ROOT / 'docs' / 'PLAN.md').read_text(encoding='utf-8')
        return plan_next(board, roster, plan_text, judge=judge, ledger=LeadLedger(guard), now=now,
                         usage_sample=read_usage_sample(guard, 'codex'), daily_cap=config.lead.daily_tasks)
    except Exception as exc:  # the lead never blocks waking employees
        log_service_error(exc)
        return {'action': 'none', 'reason': 'lead_error', 'error': type(exc).__name__}


def blocked_now(roster, config, board):
    """[(employee, problem codes, pending)] for enabled, employed employees the adapter check keeps asleep.

    Recomputed when needed rather than logged: the check depends only on code and roster, so it gives the
    same answer tick does. It reflects the current state, not a history of the day."""
    codes = roster_problem_codes(roster)
    working = employed(roster)
    return [(name, codes[name], board.pending_for_employee(name))
            for name, settings in config.employees.items()
            if settings.enabled and name in working and codes.get(name)]


def send_digest_if_due(guard, board, ledger, config, now, roster=None):
    if not digest_due(now, hour=config.digest_hour, last_sent_day=ledger.last_digest_day()):
        return False
    day = time.strftime('%Y-%m-%d', time.localtime(now))
    day_start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    try:
        done = [t for t in board.list(status='done', limit=100)['tasks'] if t['updated'] >= day_start]
        review = board.list(status='review', limit=50)['tasks']
        stale = [t for t in board.list(status='claimed', limit=50)['tasks'] if t['stale']]
        try:  # the blocked line is extra information; its failure must not hold back the digest
            roster = roster if roster is not None else load_roster(workspace=guard.workspace)
            blocked = blocked_now(roster, config, board)
        except Exception as exc:
            log_service_error(exc)
            blocked = None
        try:  # suggestion only, same rule: never holds back the digest
            promotion = promotion_review.digest_line(promotion_review.review(
                roster, evaluate(guard, now=now), roster_problem_codes(roster))) if roster is not None else None
        except Exception as exc:
            log_service_error(exc)
            promotion = None
        try:  # display only, like the blocked line
            usage = usage_overview(guard)
        except Exception as exc:
            log_service_error(exc)
            usage = None
        text = build_digest(day=day, done=done, review=review, stale=stale, runs=ledger.runs_today_by_status(),
                            exhausted=ledger.exhausted_all(), pause=read_pause(guard), blocked=blocked,
                            promotion=promotion, usage=usage)
        board.send(DISPATCHER_ID, 'user:heejun', text)
        ledger.mark_digest(day)
        return True
    except Exception as exc:
        log_service_error(exc)
        return False


def tick(dry_run=False):
    guard = load_guardrails()
    guard.check_running()
    config = DispatcherConfig.model_validate_json((CONFIG_ROOT / 'dispatcher.json').read_bytes())
    if not config.enabled:
        return {'status': 'disabled'}
    roster = load_roster(workspace=guard.workspace)
    ledger, board = DispatchLedger(guard), BoardStore(guard)
    now = time.time()
    # W2 stage 2: a pid-less running row older than the run limit no longer blocks its employee forever.
    stale_after = config.run_timeout_minutes * 60
    if not dry_run:
        # W3: refresh the observed Codex usage sample before the workload gate reads it (no model call).
        try:
            collect_codex(guard)
        except Exception as exc:  # usage is advisory; a failed read never stops the tick
            log_service_error(exc)

    def collect():
        states = []
        for employee, settings in config.employees.items():
            agent = roster.agents.get(employee)
            states.append(EmployeeState(
                employee=employee, enabled=settings.enabled, roster_status=agent.status if agent else 'missing',
                pending=board.pending_for_employee(employee) if agent else 0,
                runs_today=ledger.runs_today(employee), running=ledger.running(employee, stale_after=stale_after) is not None,
                exhausted_until=ledger.exhausted_until(employee)))
        return states

    states = collect()
    lead = {'action': 'skipped', 'reason': 'employees_busy_or_disabled'}
    if not dry_run and should_run_lead(states, lead_enabled=config.lead.enabled):
        lead = run_lead(guard, board, roster, config, now)
        if lead.get('action') not in ('none', 'skipped'):
            states = collect()  # the lead just assigned work; wake that employee in this tick
    decisions = due(states, now, daily_runs=config.daily_runs)
    # JARVIS workload policy decides whether Codex may take new work (no sample: ledger backoff only).
    allowed, workload = codex_workload_gate(read_usage_sample(guard, 'codex'), now)
    decisions = [Decision(d.employee, False, workload) if d.employee == 'codex' and d.wake and not allowed else d
                 for d in decisions]
    # An adapter that disagrees with the roster (e.g. a probation employee whose command can edit) is never woken.
    problems = roster_problems(roster)
    blocked = [d.employee for d in decisions if d.wake and problems.get(d.employee)]
    decisions = block_unsafe(decisions, problems)
    report = {'decisions': [d.__dict__ for d in decisions], 'launched': {}, 'codex_workload': workload, 'lead': lead}
    if dry_run:
        return report
    if blocked:
        log_service_error(RuntimeError('Adapter check failed; not woken: '
                                       + ' | '.join(p for name in blocked for p in problems[name])))
    send_digest_if_due(guard, board, ledger, config, now, roster)
    try:  # W3 alerts: new events once a day to the user; never stops the tick
        send_alerts(guard, board, roster, CONFIG_ROOT / 'dispatcher.json', now, sender=DISPATCHER_ID, not_woken=blocked)
    except Exception as exc:
        log_service_error(exc)
    threads = []
    for decision in decisions:
        if not decision.wake:
            continue
        run_id = ledger.try_start(decision.employee, daily_runs=config.daily_runs, stale_after=stale_after)
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
    parser.add_argument('action', choices=['serve', 'tick', 'dry-run', 'status', 'watch'])
    args = parser.parse_args()
    try:
        if args.action == 'serve':
            guard = load_guardrails()
            def config():
                return DispatcherConfig.model_validate_json((CONFIG_ROOT / 'dispatcher.json').read_bytes()).model_dump()
            service = DispatchService(
                guard, tick=tick, config=config,
                register=lambda: ProcessRegistry(guard).register_current(
                    kind='service', label='JARVIS dispatcher', max_runtime_seconds=604800),
                on_error=log_service_error)
            result = service.run()
        elif args.action == 'status':
            guard = load_guardrails()
            result = {**DispatchLedger(guard).summary(), 'pause': read_pause(guard)}
        elif args.action == 'watch':  # W2: report only; never terminates or edits
            result = process_watch.scan(load_guardrails(), CONFIG_ROOT / 'dispatcher.json')
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
