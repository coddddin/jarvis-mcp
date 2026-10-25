"""W5 dispatcher: wake AI employees that have addressed work on the board.

One `tick` checks every employee, launches lean headless runs in per-employee git worktrees
(branch auto/<employee>), waits with a timeout, records usage in the ledger and pushes the branch.
It never merges to main; merges need user approval. `.jarvis-stop` or enabled=false stops it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil

from jarvis.agents.dispatch_policy import (DispatchLedger, DispatcherConfig, EmployeeState, block_unsafe, build_command,
                                           build_env, build_digest, digest_due, due, input_key, models_line,
                                           parse_usage_detail, run_status, should_run_lead, wake_prompt, WAKE_INBOX_IDS,
                                           MODEL_BACKOFF_STATUSES)
from jarvis.agents.lead_planner import LeadLedger, plan_next
from jarvis.agents import dispatch_control, process_watch
from jarvis.agents import promotion as promotion_review
from jarvis.agents.dispatch_service import DispatchService, KillOnCloseJob, codex_workload_gate, read_pause, read_usage_sample
from jarvis.agents.process_registry import ProcessRegistry, observe_spawned
from jarvis.agents.usage_collector import collect_codex, usage_overview
from jarvis.agents.worker_adapters import ADAPTERS, employed, roster_problem_codes, roster_problems
from jarvis.agents.dispatch_policy import Decision
from jarvis.app.runtime import load_guardrails
from jarvis.cli.alerts import send_alerts
from jarvis.core.guardrails import GuardrailError
from jarvis.core.paths import CONFIG_ROOT, PROJECT_ROOT
from jarvis.core.process_env import worker_environment
from jarvis.core.roster import load_roster
from jarvis.features.board.service import BoardStore
from jarvis.features.evaluation.service import evaluate

WORKTREES = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisWorktrees'
LOGS = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisDispatch' / 'logs'
NO_HOOKS = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisDispatch' / 'no-hooks'
GIT_DIR = PROJECT_ROOT / '.git'
DISPATCHER_ID = 'local:dispatcher'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_POPEN = subprocess.Popen  # bound at import: tests fake the worker spawn through subprocess.Popen
BASELINE = '.jarvis-dispatch/git-baseline.json'
_BASELINE_LOCK = threading.Lock()  # employees run in parallel threads


def new_job():
    """A kill-on-close job for one run: everything the run started dies when the run ends (terminate),
    or with the dispatcher."""
    return KillOnCloseJob()


def git(*args, cwd=PROJECT_ROOT, check=True):
    """The dispatcher's own git runs outside any sandbox, so hooks an employee could plant in the shared
    .git never run here (empty hooks folder), and fsmonitor is off."""
    NO_HOOKS.mkdir(parents=True, exist_ok=True)
    if any(NO_HOOKS.iterdir()):
        raise RuntimeError(f'{NO_HOOKS} must stay empty; dispatcher git refused.')
    result = subprocess.run(['git', '-c', f'core.hooksPath={NO_HOOKS}', '-c', 'core.fsmonitor=false', *args], cwd=cwd,
                            capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    if check and result.returncode:
        raise RuntimeError(f'git {args[0]} failed: {result.stderr.strip()[:300]}')
    return result


def git_targets(worktree):
    """Files through which git would run code, read before a run (the worktree link is trusted only then):
    the shared config and hooks first, then this worktree's link and admin pointers. Changing commondir or
    the link could point git at another config without touching .git/config."""
    worktree = Path(worktree)
    try:
        link = (worktree / '.git').read_text(encoding='utf-8').strip()
        admin = Path(link.partition('gitdir:')[2].strip()) if link.startswith('gitdir:') else None
    except OSError:
        admin = None
    admin = admin if admin is not None and admin.is_absolute() else GIT_DIR / 'worktrees' / worktree.name
    return [GIT_DIR / 'config', GIT_DIR / 'hooks', worktree / '.git',
            *(admin / name for name in ('commondir', 'gitdir', 'config.worktree'))]


def config_entries(path):
    """A git config file as git itself reads it (include entries listed, not followed), without the
    branch.<name>.* keys other than mergeOptions: other sessions set upstream tracking all the time, and the
    dispatcher's git names its remote and branches explicitly. Unparseable: the raw bytes.
    Plain git is fine here: `git config` runs no hooks and no fsmonitor."""
    with _POPEN(['git', 'config', '--file', str(path), '--no-includes', '--list', '-z'], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW) as process:
        listed, _ = process.communicate()
    if process.returncode:
        return b'invalid:' + path.read_bytes()
    kept = []
    for entry in listed.split(b'\0'):
        key = entry.partition(b'\n')[0].lower()
        if entry and not (key.startswith(b'branch.') and not key.endswith(b'.mergeoptions')):
            kept.append(entry)
    return b'\0'.join(kept)


def git_state(targets):
    """Digest of the targets; a folder counts with every file under it. Missing and unreadable count too.
    The shared config counts by its entries (config_entries)."""
    digest = hashlib.sha256()
    for target in targets:
        paths = [target, *sorted(target.rglob('*'))] if target.is_dir() and not target.is_symlink() else [target]
        for path in paths:
            digest.update(str(path).encode('utf-8', 'surrogatepass') + b'\0')
            try:
                if path.is_symlink():
                    digest.update(b'link:' + os.readlink(path).encode('utf-8', 'surrogatepass'))
                elif path == GIT_DIR / 'config' and path.is_file():
                    digest.update(b'config:' + hashlib.sha256(config_entries(path)).digest())
                elif path.is_file():
                    digest.update(b'file:' + hashlib.sha256(path.read_bytes()).digest())
                else:
                    digest.update(b'dir' if path.is_dir() else b'absent')
            except OSError:
                digest.update(b'unreadable')
    return digest.hexdigest()


def git_snapshot(targets):
    """The shared part (config, hooks) apart from this worktree's link files, so accepting one shared
    change (resume) does not take a resume per employee."""
    return {'shared': git_state(targets[:2]), 'worktree': git_state(targets[2:])}


def _baseline_path(guard):
    path = guard.validate_path(BASELINE, must_exist=False)
    if path.is_symlink():
        raise GuardrailError('Linked dispatcher git baseline is forbidden.')
    return path


def _read_baseline(path):
    """{} when missing; None when unreadable or malformed."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not (isinstance(data, dict) and isinstance(data.get('worktrees', {}), dict)):
        return None
    return data


def _changed_parts(data, employee, snapshot, history):
    """Parts that differ from the record. A part never recorded is trusted on first use, but a missing
    record after runs have finished counts ('missing': deleted, or this check is new). Unreadable: changed."""
    if data is None:
        return ['shared', 'worktree']
    if 'shared' not in data:
        return ['missing'] if history else []
    known = {'shared': data.get('shared'), 'worktree': data.get('worktrees', {}).get(employee)}
    return [part for part in ('shared', 'worktree') if known[part] is not None and known[part] != snapshot[part]]


def _write_baseline(path, employee, snapshot, data, *, shared=True):
    """shared=False keeps a recorded shared state: a clean run's 'after' must not replace one that a
    parallel run's alarm has just recorded."""
    data = data or {}
    data = {'shared': snapshot['shared'] if shared or 'shared' not in data else data['shared'],
            'worktrees': {**data.get('worktrees', {}), employee: snapshot['worktree']}}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temp, path)


def _pause(guard, reason):
    try:
        dispatch_control.pause(guard, reason)
        return True
    except Exception as exc:
        log_service_error(exc)
        return False


# Parallel runs share the record. When there is an alarm, the pause is written before the new state, both under
# the lock, so a run that reads the new record also sees the pause. If the pause fails, the old record stays and
# the next check refuses too.

def check_git_before_run(guard, employee, snapshot, *, history, reason):
    """Changed parts since the record ([] when unchanged). On a change: pause, then record (resume accepts)."""
    with _BASELINE_LOCK:
        path = _baseline_path(guard)
        data = _read_baseline(path)
        changed = _changed_parts(data, employee, snapshot, history)
        first_use = data is not None and ('shared' not in data or employee not in data.get('worktrees', {}))
        if (changed and _pause(guard, reason)) or (not changed and first_use):
            _write_baseline(path, employee, snapshot, data)
    return changed


def record_git_after_run(guard, employee, snapshot, *, alarm=None):
    """The state the next run must start from. With an alarm reason the dispatcher is paused first.
    A clean run (no alarm) left the shared part as it found it, so only its worktree part is written."""
    with _BASELINE_LOCK:
        path = _baseline_path(guard)
        if alarm is None or _pause(guard, alarm):
            _write_baseline(path, employee, snapshot, _read_baseline(path), shared=alarm is not None)


def warn_user(board, body):
    board.send(DISPATCHER_ID, 'user:heejun', body)


def worktree_path(employee):
    return WORKTREES / employee


def ensure_worktree(employee):
    """Per-employee worktree on auto/<employee>. Existing branches are never reset."""
    branch, path = f'auto/{employee}', worktree_path(employee)
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


def prepare_run(employee, run_id, config, guard, ledger, board):
    """Checks and the worktree for one run, before any run of the tick starts. Returns what
    execute_run needs, or a status when the run does not start (its ledger row is closed)."""
    exe = executable(employee)
    if exe is None:
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                      note='executable not found')
        return 'error'
    models = config.employees[employee].models
    model = ledger.pick_model(employee, models) if models else None
    if models and model is None:  # every model backed off since this tick decided to wake
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                      note='all models backed off')
        return 'error'
    if model:
        ledger.set_model(run_id, model)
    # Checked before ensure_worktree, whose merge already runs git: a change made since the last run (by a
    # process that outlived it, or anything else) must not become the new 'before' unnoticed.
    seen = git_snapshot(git_targets(worktree_path(employee)))
    changed = check_git_before_run(guard, employee, seen, history=ledger.finished_runs() > 0,
                                   reason=f'git config/hooks changed before {employee} run {run_id}')
    if changed:
        if 'missing' in changed:
            what = (f'git 기준 기록({BASELINE})이 없는데 이전 실행 기록은 있어요(이 검사를 처음 적용했거나 '
                    '누가 지웠어요)')
        else:
            what = '지난 실행 뒤 ' + ('.git/config 또는 .git/hooks가' if 'shared' in changed
                                      else f'{employee} worktree 연결 파일이') + ' 바뀌었어요'
        warn_user(board, f'[경고] 디스패처: {what}. {employee}를 실행하지 않고 디스패처를 일시정지했어요. '
                         '무엇이 바꿨는지 확인하고, 괜찮으면(예: 사람이 원격을 추가함) HUD에서 다시 시작하세요. '
                         '그러면 지금 상태를 기준으로 삼아요.')
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                      note='git changed since last run')
        return 'error'
    if read_pause(guard)['paused']:  # a parallel run's alarm in this tick; its pause came before its record
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                      note='dispatcher paused')
        return 'error'
    worktree = ensure_worktree(employee)
    # Tests run with the project's interpreter by absolute path; a .venv junction would trip link guards.
    python = (PROJECT_ROOT / '.venv' / 'Scripts' / 'python.exe').as_posix()
    ids = board.pending_ids(employee)[:WAKE_INBOX_IDS]  # the oldest waking messages; the rest wake the next tick
    ledger.set_inputs(run_id, ids)  # shown in the HUD; input_key (set at try_start) is unchanged
    argv = build_command(employee, config, worktree, wake_prompt(employee, python, ids), mcp_config(), executable=exe[0],
                         git_dir=PROJECT_ROOT / '.git', python=python, model=model)
    argv = exe + argv[1:]
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f'{employee}-{run_id}.log'
    timeout = config.run_timeout_minutes * 60
    targets = git_targets(worktree)
    before = git_snapshot(targets)  # compared after the run, once nothing the run started is left
    return {'employee': employee, 'run_id': run_id, 'model': model, 'models': models, 'worktree': worktree,
            'argv': argv, 'log_path': log_path, 'timeout': timeout, 'targets': targets, 'before': before}


def execute_run(run, guard, ledger, board):
    """Spawn, wait, end everything the run started, check .git. Returns (status, push): push is the run
    when it may push, done by push_after_runs once every run of the tick has ended."""
    employee, run_id, model, models = run['employee'], run['run_id'], run['model'], run['models']
    worktree, argv, log_path, timeout = run['worktree'], run['argv'], run['log_path'], run['timeout']
    targets, before = run['targets'], run['before']
    run_job, bound = new_job(), False
    try:
        with log_path.open('w', encoding='utf-8', errors='replace') as log:
            env = worker_environment(overrides={
                **build_env(employee, project_root=PROJECT_ROOT, workspace=guard.workspace),
                'JARVIS_WORKSPACE': str(guard.workspace), 'JARVIS_DOWNLOADS_READONLY': '1',
            })
            process = subprocess.Popen(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            bound = run_job.assign(process) or os.name != 'nt'  # bind before anything else can spawn
            if not bound:
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
    finally:
        # Background children outlive the worker and could write .git after the check, so they die first.
        contained = run_job.terminate() and bound
    output = log_path.read_text(encoding='utf-8', errors='replace')
    # No usage event at all is 'missing', kept apart from 'unknown' (usage printed but unreadable).
    usage = parse_usage_detail(output) or {'input': None, 'output': None, 'cache': None, 'quality': 'missing'}
    failed = process.returncode != 0 or '"is_error":true' in output.replace(' ', '')
    if status is None:
        status = run_status(output, failed=failed)
    ledger.finish(run_id, status=status, exit_code=process.returncode, input_tokens=usage['input'],
                  output_tokens=usage['output'], cache_tokens=usage['cache'], usage_quality=usage['quality'],
                  note=log_path.name)
    after, push = git_snapshot(targets), None
    if not contained:
        # Something the run started may still be alive and could change .git after any check here.
        record_git_after_run(guard, employee, after, alarm=f'{employee} run {run_id} left processes behind')
        warn_user(board, f'[경고] 디스패처: {employee} 실행(run {run_id})이 띄운 프로그램을 모두 끝내지 못했어요. '
                         'push를 건너뛰고 디스패처를 일시정지했어요. 작업 관리자에서 남은 프로그램을 확인하고 '
                         f'.git/config와 .git/hooks를 본 뒤, 괜찮으면 HUD에서 다시 시작하세요. 로그 {log_path.name}')
    elif after != before:
        # The run changed git config, hooks or this worktree's link. Any git here (push, the next run's merge)
        # could then run its code outside the sandbox, so nothing more runs until the user has looked.
        record_git_after_run(guard, employee, after, alarm=f'git config/hooks changed by {employee} run {run_id}')
        warn_user(board, f'[경고] 디스패처: {employee} 실행(run {run_id}) 뒤 .git/config, .git/hooks 또는 worktree '
                         '연결 파일이 바뀌었어요. push를 건너뛰고 디스패처를 일시정지했어요. 이 저장소에서 git을 쓰기 '
                         f'전에 그 파일들을 확인하고, 괜찮으면 HUD에서 다시 시작하세요. 로그 {log_path.name}')
    else:
        record_git_after_run(guard, employee, after)
        push = run
    # A rotating employee's per-model backoff is routine (the digest shows it); alert once every model is out.
    quiet = model and status in MODEL_BACKOFF_STATUSES and ledger.pick_model(employee, models) is not None
    if status != 'ok' and not quiet:
        board.send(DISPATCHER_ID, 'user:heejun',
                   f'[알림] 디스패처: {employee} 실행 {status} (run {run_id}, exit {process.returncode}'
                   + (f', 모델 {model}' if model else '') + f'). 로그 {log_path.name}')
    return status, push


def push_after_runs(guard, board, ready):
    """Pushes wait until every run of the tick has ended: a run still alive could change .git/config
    (sshCommand, credential.helper) between another run's check and its push. Then one fresh check against
    the record, under the lock; any difference pauses (before recording) and nothing is pushed."""
    if not ready:
        return
    changed = []
    with _BASELINE_LOCK:
        if read_pause(guard)['paused']:
            return
        path = _baseline_path(guard)
        data = _read_baseline(path)
        now = {run['employee']: git_snapshot(run['targets']) for run in ready}
        changed = [name for name, snapshot in now.items() if _changed_parts(data, name, snapshot, True)]
        if changed:
            if _pause(guard, 'git config/hooks changed before the push of ' + ', '.join(changed)):
                for name, snapshot in now.items():
                    data = _read_baseline(path) or {}
                    _write_baseline(path, name, snapshot, data)
    if changed:
        warn_user(board, '[경고] 디스패처: 실행이 모두 끝난 뒤 push 직전에 .git/config, .git/hooks 또는 worktree 연결 '
                         f'파일이 바뀌어 있었어요({", ".join(changed)}). push를 모두 건너뛰고 디스패처를 일시정지했어요. '
                         '그 파일들을 확인하고, 괜찮으면 HUD에서 다시 시작하세요.')
        return
    for run in ready:
        branch = f"auto/{run['employee']}"
        ahead = git('rev-list', '--count', f'main..{branch}', check=False).stdout.strip()
        if ahead not in ('', '0'):
            git('push', '-q', 'origin', branch, cwd=run['worktree'], check=False)


def run_employee(employee, run_id, config, guard, ledger, board):
    """One run on its own: prepare, execute, then its push (tick does the same for several runs at once)."""
    run = prepare_run(employee, run_id, config, guard, ledger, board)
    if isinstance(run, str):
        return run
    status, push = execute_run(run, guard, ledger, board)
    push_after_runs(guard, board, [push] if push else [])
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
        try:  # display only, like the blocked line
            models = models_line({name: (settings.models, ledger.models_backed_off(name))
                                  for name, settings in config.employees.items() if settings.models})
        except Exception as exc:
            log_service_error(exc)
            models = None
        text = build_digest(day=day, done=done, review=review, stale=stale, runs=ledger.runs_today_by_status(),
                            exhausted=ledger.exhausted_all(), pause=read_pause(guard), blocked=blocked,
                            promotion=promotion, usage=usage, models=models)
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

    keys = {}  # J2 H-02: each employee's unread-input fingerprint, stored with the run it wakes

    def collect():
        states = []
        for employee, settings in config.employees.items():
            agent = roster.agents.get(employee)
            pending_ids = board.pending_ids(employee) if agent else []
            keys[employee] = input_key(pending_ids)
            states.append(EmployeeState(
                employee=employee, enabled=settings.enabled, roster_status=agent.status if agent else 'missing',
                pending=len(pending_ids),
                runs_today=ledger.runs_today(employee), running=ledger.running(employee, stale_after=stale_after) is not None,
                exhausted_until=(ledger.rotation_exhausted_until(employee, settings.models) if settings.models
                                 else ledger.exhausted_until(employee)),
                tokens_today=ledger.tokens_today(employee)['total'] if settings.daily_tokens is not None else 0,
                daily_tokens=settings.daily_tokens, input_hold=ledger.input_hold(employee, keys[employee])))
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
        unchanged = [d.employee for d in decisions if d.reason == 'unchanged_input']
        send_alerts(guard, board, roster, CONFIG_ROOT / 'dispatcher.json', now, sender=DISPATCHER_ID, not_woken=blocked,
                    unchanged=unchanged)
    except Exception as exc:
        log_service_error(exc)
    def failed(employee, run_id, exc):  # record, never crash the scheduler
        ledger.finish(run_id, status='error', exit_code=None, input_tokens=None, output_tokens=None,
                      note=type(exc).__name__ + ': ' + str(exc)[:300])
        report['launched'][employee] = 'error'

    # 1. Checks and worktrees one by one, before any run starts: no run can change .git under another's merge.
    prepared = []
    for decision in decisions:
        if not decision.wake:
            continue
        run_id = ledger.try_start(decision.employee, daily_runs=config.daily_runs, stale_after=stale_after,
                                  input_key=keys.get(decision.employee))
        if run_id is None:
            continue
        try:
            run = prepare_run(decision.employee, run_id, config, guard, ledger, board)
        except Exception as exc:
            failed(decision.employee, run_id, exc)
            continue
        if isinstance(run, str):
            report['launched'][decision.employee] = run
        else:
            prepared.append(run)
    if prepared and read_pause(guard)['paused']:  # a later employee's check alarmed after earlier ones passed
        for run in prepared:
            ledger.finish(run['run_id'], status='error', exit_code=None, input_tokens=None, output_tokens=None,
                          note='dispatcher paused')
            report['launched'][run['employee']] = 'error'
        prepared = []
    # 2. The runs, in parallel.
    ready, threads = [], []
    for run in prepared:
        def work(run=run):
            try:
                status, push = execute_run(run, guard, ledger, board)
                report['launched'][run['employee']] = status
                if push:
                    ready.append(push)
            except Exception as exc:
                failed(run['employee'], run['run_id'], exc)
        thread = threading.Thread(target=work)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    # 3. Pushes, once every run has ended and every job is emptied.
    try:
        push_after_runs(guard, board, ready)
    except Exception as exc:
        log_service_error(exc)
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

