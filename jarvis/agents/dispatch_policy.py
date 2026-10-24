"""W5 dispatcher policy: decide which AI employees to wake, shape their lean headless commands,
and keep a run ledger (runs per day, token usage, limit backoff).

No model is invoked here. The CLI in jarvis/cli/dispatch.py performs launches.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
import sqlite3
import time
from typing import Literal

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

# CLAUDE_TOOLS, EMPLOYEES and OPENCODE_DEFAULT_MODEL stay importable from here for existing callers.
from jarvis.agents.worker_adapters import (ADAPTERS, CLAUDE_TOOLS, EMPLOYEES, OPENCODE_DEFAULT_MODEL, WorkRequest,  # noqa: F401
                                          adapter)
from jarvis.core.guardrails import GuardrailError

BACKOFF_START, BACKOFF_MAX = 3600, 6 * 3600
LIMIT_PATTERN = re.compile(r'usage limit|rate limit|quota|\b429\b|limit reached|exceeded your|out of (?:credits|usage)', re.I)
# Employees whose work command cannot apply edits get the probation prompt (from worker_adapters).
PROBATION_PROMPT = tuple(name for name in EMPLOYEES if not ADAPTERS[name].applies_edits)
ELIGIBILITY_PATTERN = re.compile(r'FreeTierError|free tier can only be used', re.I)
# OpenCode's own wording (opencode-cli strings: ProviderModelNotFoundError, 'Model not found: <provider>/<model>').
# Free model lists change often; a retired model is backed off like a limit so rotation moves on.
MODEL_MISSING_PATTERN = re.compile(r'\b(?:Provider)?ModelNotFoundError\b|Model not found: [A-Za-z0-9._-]+/[A-Za-z0-9._:-]+')
BACKOFF_STATUSES = ('limit', 'eligibility', 'model_missing')
# Backed off per model on a rotating employee. eligibility (403 FreeTierError) comes from how OpenCode is
# invoked, not from the model, so it stays employee-level: rotating would only walk every model first.
MODEL_BACKOFF_STATUSES = ('limit', 'model_missing')
# No leading '-': the value follows -m on the command line and must never read as an option.
ModelId = Annotated[str, Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9._][A-Za-z0-9._/:#-]*$')]


class EmployeeSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    enabled: bool = True
    max_turns: int = Field(default=60, ge=1, le=200)
    model: str = Field(default='', max_length=120, pattern=r'^(?:[A-Za-z0-9._][A-Za-z0-9._/:#-]*)?$')  # no leading '-'
    # W5: tokens (input incl. cache + output) this employee may use per local day; None means no token cap.
    daily_tokens: int | None = Field(default=None, ge=1000, le=1_000_000_000)
    # Rotation: models in priority order. A limit/model_missing run backs off only that model;
    # the employee waits only when every model is backed off. Exclusive with model, so the choice is clear.
    models: list[ModelId] | None = Field(default=None, min_length=1, max_length=8)

    @model_validator(mode='after')
    def _one_model_source(self):
        if self.models is not None:
            if self.model:
                raise ValueError('Use either model or models, not both.')
            if len(set(self.models)) != len(self.models):
                raise ValueError('models must not repeat.')
        return self


class LeadSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    enabled: bool = True
    daily_tasks: int = Field(default=3, ge=1, le=10)


class DispatcherConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    version: Literal[1]
    enabled: bool
    interval_minutes: int = Field(ge=5, le=240)
    daily_runs: int = Field(ge=1, le=100)
    run_timeout_minutes: int = Field(ge=5, le=120)
    employees: dict[Literal['codex', 'claude-code', 'cursor', 'opencode'], EmployeeSettings]
    lead: LeadSettings = Field(default_factory=LeadSettings)
    digest_hour: int = Field(default=21, ge=0, le=23)


@dataclass(frozen=True)
class EmployeeState:
    employee: str
    enabled: bool
    roster_status: str
    pending: int
    runs_today: int
    running: bool
    exhausted_until: float | None
    tokens_today: int = 0             # W5: input (incl. cache) + output of runs started today
    daily_tokens: int | None = None   # None: no token cap


@dataclass(frozen=True)
class Decision:  # reason may also carry a JARVIS workload_policy verdict
    employee: str
    wake: bool
    reason: str


def due(states, now, *, daily_runs):
    decisions = []
    for item in states:
        if not item.enabled:
            reason = 'disabled'
        elif item.roster_status not in ('active', 'probation'):
            reason = 'not_employed'
        elif item.running:
            reason = 'already_running'
        elif item.exhausted_until is not None and item.exhausted_until > now:
            reason = 'exhausted'
        elif item.runs_today >= daily_runs:
            reason = 'daily_cap'
        elif item.daily_tokens is not None and item.tokens_today >= item.daily_tokens:
            reason = 'token_cap'
        elif item.pending <= 0:
            reason = 'no_pending_work'
        else:
            reason = 'pending_work'
        decisions.append(Decision(item.employee, reason == 'pending_work', reason))
    return decisions


def should_run_lead(states, *, lead_enabled):
    """The JARVIS lead plans new work only when no employee has addressed work or a live run."""
    return lead_enabled and all(item.pending <= 0 and not item.running for item in states)


def digest_due(now, *, hour, last_sent_day):
    local = time.localtime(now)
    return local.tm_hour >= hour and time.strftime('%Y-%m-%d', local) != last_sent_day


# Korean labels for worker_adapters.PROBLEM_CODES; unknown codes are left out of user-facing text.
PROBLEM_LABELS = {'no_roster': '명부에 없음', 'retired': '퇴사', 'probation_can_edit': '수습인데 파일 수정 가능',
                  'label_mismatch': '수정 불가 표시와 실제 명령 불일치', 'privacy_open': '외부 읽기·웹 차단 안 됨'}


def _blocked_line(blocked):
    """'- 현재 점검으로 깨우지 않음: cursor(수습인데 파일 수정 가능, 받은 일 2건)' or None."""
    parts = []
    for employee, codes, pending in blocked[:10]:
        labels = [PROBLEM_LABELS[code] for code in codes if code in PROBLEM_LABELS]
        if not labels:
            continue
        reason = '·'.join(labels) + (f', 받은 일 {pending}건' if pending else '')
        parts.append(f'{employee}({reason})')
    return '- 현재 점검으로 깨우지 않음: ' + ', '.join(parts) if parts else None


def _usage_line(usage):
    """'- 사용량(마지막 관찰): codex 5h 81%·주간 44% (10-22 13:05)' with stale samples marked; display only."""
    parts = []
    for employee, item in sorted((usage or {}).items())[:5]:
        if not isinstance(item, dict) or not isinstance(item.get('five_hour'), (int, float)):
            continue
        text = f"{employee} 5h {round(item['five_hour'])}%"
        if isinstance(item.get('weekly'), (int, float)):
            text += f"·주간 {round(item['weekly'])}%"
        when = item.get('sampled_at')
        stamp = time.strftime('%m-%d %H:%M', time.localtime(when)) if isinstance(when, (int, float)) else '?'
        parts.append(text + (f' ({stamp})' if item.get('fresh') else f' ({stamp}, 오래됨)'))
    return '- 사용량(마지막 관찰): ' + ', '.join(parts) if parts else None


def build_digest(*, day, done, review, stale, runs, exhausted, pause, blocked=None, promotion=None, usage=None,
                 models=None):
    """Short Korean daily report for user:heejun (board message, max 1000 chars, no double quotes).

    blocked: [(employee, problem codes, pending messages)] for employees the adapter check keeps asleep now.
    promotion: the '- 승격 후보: ...' line from agents.promotion.digest_line, or None."""
    def names(items, limit):
        shown = [f"{i['title'][:60]}({(i.get('owner') or '-').split(':')[0]})" for i in items[:limit]]
        more = f' 외 {len(items) - limit}건' if len(items) > limit else ''
        return ', '.join(shown) + more if shown else '없음'
    lines = [f'[알림] 자비스 하루 요약 {day}',
             f'- 완료 {len(done)}건: ' + names(done, 10),
             f'- 머지·검토 대기 {len(review)}건: ' + names(review, 5),
             f'- 멈춘 담당 {len(stale)}건: ' + names(stale, 5)]
    if runs:
        lines.append('- 오늘 깨운 횟수: ' + ', '.join(
            f"{who} " + '/'.join(f'{status} {count}' for status, count in sorted(counts.items()))
            for who, counts in sorted(runs.items())))
    if exhausted:
        lines.append('- 한도 대기: ' + ', '.join(
            f"{who} {time.strftime('%H:%M', time.localtime(until))}까지" for who, until in sorted(exhausted.items())))
    if models:
        lines.append(models[:300])
    if pause.get('paused'):
        lines.append('- 디스패처 일시정지 중' + (f": {pause['reason']}" if pause.get('reason') else ''))
    usage_line = _usage_line(usage)
    if usage_line:
        lines.append(usage_line[:300])
    line = _blocked_line(blocked or [])
    if line:
        lines.append(line[:300])
    if promotion:
        lines.append(promotion[:200])
    return '\n'.join(lines).replace('"', "'")[:1000]


def block_unsafe(decisions, problems):
    """Keep employees whose adapter failed worker_adapters.roster_problems asleep (reason adapter_check_failed)."""
    return [Decision(d.employee, False, 'adapter_check_failed') if d.wake and problems.get(d.employee) else d
            for d in decisions]


def next_backoff(previous):
    return BACKOFF_START if not previous else min(BACKOFF_MAX, previous * 2)


def detect_limit(text):
    return bool(LIMIT_PATTERN.search(text or ''))


def detect_model_missing(text):
    return bool(MODEL_MISSING_PATTERN.search(text or ''))


def run_status(output, *, failed):
    """Ledger status for a finished run. A free-tier refusal can arrive as an error event with exit code 0,
    so it is checked first (as before). A missing model counts only for a failed run: a successful run's
    output may quote the wording (this repository's own tests and docs contain it)."""
    if detect_eligibility(output):
        return 'eligibility'
    if not failed:
        return 'ok'
    if detect_model_missing(output):
        return 'model_missing'
    return 'limit' if detect_limit(output) else 'error'


def models_line(rotation):
    """'- opencode 모델: 지금 opencode/a, 소진 opencode/b(14:20까지)' for rotating employees, or None.
    rotation: {employee: (models, {model: until})}. Model IDs are config-validated; fixed format, no free text."""
    parts = []
    for employee, (models, backed_off) in sorted((rotation or {}).items()):
        current = next((m for m in models if m not in backed_off), None)
        text = f'{employee} 모델: ' + (f'지금 {current}' if current else '모두 소진')
        waiting = [f"{m}({time.strftime('%H:%M', time.localtime(backed_off[m]))}까지)" for m in models if m in backed_off]
        if waiting:
            text += ', 소진 ' + '·'.join(waiting)
        parts.append(text)
    return ('- ' + ' / '.join(parts))[:300] if parts else None


def detect_eligibility(text):
    """OpenCode's free tier refusing the request (HTTP 403 FreeTierError): not a quota, but backed off the same way."""
    return bool(ELIGIBILITY_PATTERN.search(text or ''))


CACHE_FIELDS = ('cache_creation_input_tokens', 'cache_read_input_tokens')


def _usage_from(obj):
    usage = obj.get('usage') if isinstance(obj, dict) else None
    if not isinstance(usage, dict) or 'output_tokens' not in usage:
        return None
    cache = sum(v for k, v in usage.items() if k in CACHE_FIELDS and isinstance(v, int))
    plain = usage.get('input_tokens') if isinstance(usage.get('input_tokens'), int) else 0
    return {'input': plain + cache, 'cache': cache,
            'output': usage['output_tokens'] if isinstance(usage['output_tokens'], int) else None}


def parse_usage_detail(text):
    """{'input' (incl. cache), 'cache', 'output'} from a JSON result or the last JSONL event that reports usage;
    None if absent. Cache counts only the Claude cache fields (CACHE_FIELDS)."""
    try:
        found = _usage_from(json.loads(text))
        if found:
            return found
    except (ValueError, TypeError):
        pass
    found = None
    for line in (text or '').splitlines():
        try:
            candidate = _usage_from(json.loads(line))
        except (ValueError, TypeError):
            continue
        if candidate:
            found = candidate
    return found


def parse_usage(text):
    """(input incl. cache, output) tokens; (None, None) if absent."""
    found = parse_usage_detail(text)
    return (found['input'], found['output']) if found else (None, None)


def wake_prompt(employee, python='.venv/Scripts/python.exe'):
    agent, branch = f'{employee}:auto', f'auto/{employee}'
    # No double quotes: Windows argument passing to PowerShell-based launchers mangles them.
    lines = [
        f'너는 J.A.R.V.I.S. 프로젝트의 AI 직원 {agent}다. 지금 폴더는 git worktree이고 브랜치는 {branch}다.',
        f"1. jarvis MCP의 board_inbox(agent='{agent}')로 받은 요청을 읽는다. 처리할 요청이 없으면 바로 끝낸다.",
        '2. AGENTS_CONTEXT.md 규칙을 따른다. 파일을 고치기 전에 board_claim으로 경로를 맡는다. 명부 등급을 넘는 도구는 쓰지 않는다.',
        f'3. 커밋은 의미 있는 단위가 끝나고 검사({python} -m pytest)가 통과했을 때만 이 브랜치({branch})에 한다. '
        'main 브랜치 커밋·merge·push는 금지. push는 디스패처가 한다.',
        "4. 끝나면 board_release(status='review', summary에 브랜치·커밋·검증 결과)를 하고, 요청자와 user:heejun에게 "
        'board_send로 결과를 짧게 알린다. main 머지는 사용자 승인 후에만 이뤄진다.',
        "5. 다른 직원의 도움이 필요하면 board_send(to='codex' 또는 'claude-code' 또는 'cursor')로 요청한다.",
        '6. 한국어로 쓰고, 큰 문서는 필요한 부분만 읽는다. 30분 안에 끝낸다.',
    ]
    if employee in PROBATION_PROMPT:
        lines.append('수습 직원이므로 파일을 고치거나 커밋하지 않는다. 검토 의견과 제안만 board_note·board_send로 남긴다.')
    if employee == 'opencode':
        lines.append('무료 모델 직원이므로 개인정보(메일·일정·화면·개인 문서)는 다루지 않는다. 코드와 공개 문서만 본다.')
    return '\n'.join(lines)


def build_command(employee, config, worktree, prompt, mcp_json, *, executable, git_dir=None, python=None, model=None):
    """Work-mode argv for one employee; the shape lives in worker_adapters. model: the rotation's pick."""
    settings = lambda: config.employees[employee]  # noqa: E731 - read only by employees that need it
    return adapter(employee).work_command(WorkRequest(
        str(worktree), prompt, mcp_json, executable, max_turns=lambda: settings().max_turns,
        model=lambda: model or settings().model, git_dir=git_dir, python=python))


def build_env(employee, *, project_root, workspace):
    """Extra environment for an employee run. OpenCode gets its whole probation config inline."""
    item = ADAPTERS.get(employee)
    return item.work_env(project_root, workspace) if item else {}


class DispatchLedger:
    DIRECTORY = '.jarvis-dispatch'
    DATABASE = '.jarvis-dispatch/ledger.sqlite3'

    def __init__(self, guard, *, clock=time.time, alive=None, lookup=None):
        self.guard, self.clock = guard, clock
        self.alive = alive or _pid_alive
        # Creation-time lookup for pid reuse. An injected alive() alone means the caller decides liveness.
        self.lookup = lookup if lookup is not None else (None if alive else _process)

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY, must_exist=False)
        if directory.is_symlink() or directory.is_junction():
            raise GuardrailError('Linked dispatch storage is forbidden.')
        directory.mkdir(exist_ok=True)
        db = sqlite3.connect(self.guard.validate_path(self.DATABASE, must_exist=False), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT NOT NULL, '
                       'pid INTEGER, started REAL NOT NULL, ended REAL, status TEXT NOT NULL, exit_code INTEGER, '
                       'input_tokens INTEGER, output_tokens INTEGER, note TEXT NOT NULL DEFAULT "")')
            db.execute('CREATE TABLE IF NOT EXISTS exhaustion (employee TEXT PRIMARY KEY, until REAL NOT NULL, '
                       'backoff REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS digests (day TEXT PRIMARY KEY, sent REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS model_exhaustion (employee TEXT NOT NULL, model TEXT NOT NULL, '
                       'until REAL NOT NULL, backoff REAL NOT NULL, PRIMARY KEY(employee, model))')
            # W2 stage 2: when set_pid ran. Added in place for older ledgers, inside this IMMEDIATE transaction
            # so concurrent openers (service, HUD, CLI) serialize; a lost race shows up as a duplicate column.
            if 'pid_at' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                try:
                    db.execute('ALTER TABLE runs ADD COLUMN pid_at REAL')
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc).lower():
                        raise
            # W5: cache share of input_tokens, kept apart so the cap can be explained. NULL: not recorded.
            if 'cache_tokens' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                try:
                    db.execute('ALTER TABLE runs ADD COLUMN cache_tokens INTEGER')
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc).lower():
                        raise
            # Rotation: the model a run used. NULL: the employee has a single model (or an older row).
            if 'model' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                try:
                    db.execute('ALTER TABLE runs ADD COLUMN model TEXT')
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc).lower():
                        raise
            yield db
            db.commit()
        finally:
            db.close()

    def start(self, employee, *, pid):
        with self.connection() as db:
            return db.execute('INSERT INTO runs(employee,pid,started,status) VALUES(?,?,?,?)',
                              (employee, pid, self.clock(), 'running')).lastrowid

    def owner_state(self, row):
        """'alive', 'dead' or 'unverifiable' for a running row with a pid. Alive: the pid exists and was created
        by set_pid time (pid_at + 5 s), or for rows from before pid_at, by started + 120 s (worktree setup).
        Unverifiable: the pid exists but its creation time cannot be read (access denied)."""
        if not self.alive(row['pid']):
            return 'dead'
        if self.lookup is None:
            return 'alive'
        try:
            created = self.lookup(row['pid']).create_time()
        except Exception as exc:  # psutil.NoSuchProcess means gone; anything else is a doubt
            return 'dead' if type(exc).__name__ in ('NoSuchProcess', 'ZombieProcess') else 'unverifiable'
        return 'alive' if created <= owner_deadline(row['pid_at'], row['started']) else 'dead'

    def owner_alive(self, row):
        """Compatibility view of owner_state: a doubt counts as alive."""
        return self.owner_state(row) != 'dead'

    def _settle(self, db, row, now, stale_after):
        """True if the row still holds the employee's slot; otherwise close it as lost (never kills anything)."""
        if row['pid'] is None:
            if stale_after is None or now - row['started'] <= stale_after:
                return True  # still spawning (or no limit given): keep waiting
            db.execute('UPDATE runs SET status="lost", ended=?, note="no_pid" WHERE id=?', (now, row['id']))
            return False
        state = self.owner_state(row)
        if state == 'alive':
            return True
        if state == 'unverifiable':
            # A doubt holds the slot only within the run limit; a genuine run would have ended by then, so
            # closing it afterwards cannot start a second copy (main review of 2c50855f).
            if stale_after is None or now - row['started'] <= stale_after:
                return True
            db.execute('UPDATE runs SET status="lost", ended=?, note="owner_unverifiable" WHERE id=?', (now, row['id']))
            return False
        db.execute('UPDATE runs SET status="lost", ended=? WHERE id=?', (now, row['id']))
        return False

    def try_start(self, employee, *, daily_runs, stale_after=None):
        """Atomically reserve a run: refused if one is live or the daily cap is reached. Returns run id or None.

        stale_after (seconds, normally run_timeout_minutes*60): a pid-less running row older than this is closed
        as lost instead of blocking the employee forever."""
        now = self.clock()
        day_start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        with self.connection() as db:
            for row in db.execute('SELECT id, pid, pid_at, started FROM runs WHERE employee=? AND status="running"',
                                  (employee,)).fetchall():
                if self._settle(db, row, now, stale_after):
                    return None
            if db.execute('SELECT COUNT(*) FROM runs WHERE employee=? AND started>=?', (employee, day_start)).fetchone()[0] >= daily_runs:
                return None
            return db.execute('INSERT INTO runs(employee,pid,started,status) VALUES(?,?,?,?)',
                              (employee, None, now, 'running')).lastrowid

    def set_pid(self, run_id, pid):
        with self.connection() as db:
            db.execute('UPDATE runs SET pid=?, pid_at=? WHERE id=?', (pid, self.clock(), run_id))

    def set_model(self, run_id, model):
        with self.connection() as db:
            db.execute('UPDATE runs SET model=? WHERE id=?', (model, run_id))

    def finish(self, run_id, *, status, exit_code, input_tokens, output_tokens, note, cache_tokens=None):
        """limit/model_missing on a run with a model back off only that model; eligibility, or any backoff
        status on a run without a model, backs off the employee."""
        now = self.clock()
        with self.connection() as db:
            row = db.execute('SELECT employee, model FROM runs WHERE id=?', (run_id,)).fetchone()
            db.execute('UPDATE runs SET ended=?, status=?, exit_code=?, input_tokens=?, output_tokens=?, note=?, '
                       'cache_tokens=? WHERE id=?',
                       (now, status, exit_code, input_tokens, output_tokens, note[:500], cache_tokens, run_id))
            employee, model = row['employee'], row['model']
            if status in MODEL_BACKOFF_STATUSES and model:
                prior = db.execute('SELECT backoff FROM model_exhaustion WHERE employee=? AND model=?',
                                   (employee, model)).fetchone()
                backoff = next_backoff(prior['backoff'] if prior else None)
                db.execute('INSERT OR REPLACE INTO model_exhaustion VALUES (?,?,?,?)',
                           (employee, model, now + backoff, backoff))
            elif status in BACKOFF_STATUSES:
                prior = db.execute('SELECT backoff FROM exhaustion WHERE employee=?', (employee,)).fetchone()
                backoff = next_backoff(prior['backoff'] if prior else None)
                db.execute('INSERT OR REPLACE INTO exhaustion VALUES (?,?,?)', (employee, now + backoff, backoff))
            elif status == 'ok':
                db.execute('DELETE FROM exhaustion WHERE employee=?', (employee,))
                if model:
                    db.execute('DELETE FROM model_exhaustion WHERE employee=? AND model=?', (employee, model))

    def models_backed_off(self, employee):
        """{model: until} for this employee's models still backed off now."""
        now = self.clock()
        with self.connection() as db:
            return {row['model']: row['until'] for row in db.execute(
                'SELECT model, until FROM model_exhaustion WHERE employee=?', (employee,)) if row['until'] > now}

    def pick_model(self, employee, models):
        """The first model in priority order that is not backed off, or None when all are."""
        backed_off = self.models_backed_off(employee)
        return next((m for m in models if m not in backed_off), None)

    def models_exhausted_until(self, employee, models):
        """When the first model comes back if every model is backed off; otherwise None (one is usable)."""
        backed_off = self.models_backed_off(employee)
        return min(backed_off[m] for m in models) if all(m in backed_off for m in models) else None

    def rotation_exhausted_until(self, employee, models):
        """When a rotating employee may wake again: it needs both its own backoff over (eligibility) and one
        usable model, so the later of the two; None when it may wake now."""
        now = self.clock()
        own = self.exhausted_until(employee)
        waits = [t for t in (own if own is not None and own > now else None,
                             self.models_exhausted_until(employee, models)) if t is not None]
        return max(waits) if waits else None

    def running(self, employee, *, stale_after=None):
        with self.connection() as db:
            row = db.execute('SELECT id, pid, pid_at, started FROM runs WHERE employee=? AND status="running" '
                             'ORDER BY id DESC LIMIT 1', (employee,)).fetchone()
            if row is None:
                return None
            return row['id'] if self._settle(db, row, self.clock(), stale_after) else None

    def tokens_today(self, employee):
        """Tokens of runs started today (local day): {'total': input + output (the cap compares this),
        'input' (incl. cache), 'output', 'cache', 'cache_unknown_runs' (runs with tokens but no cache record)}.
        Runs whose usage could not be read count 0; daily_runs still bounds them."""
        start = time.mktime(time.localtime(self.clock())[:3] + (0, 0, 0, 0, 0, -1))
        with self.connection() as db:
            row = db.execute('SELECT COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out, '
                             'COALESCE(SUM(cache_tokens),0) cache, '
                             'SUM(CASE WHEN cache_tokens IS NULL AND input_tokens IS NOT NULL THEN 1 ELSE 0 END) unknown '
                             'FROM runs WHERE employee=? AND started>=?', (employee, start)).fetchone()
        return {'total': row['inp'] + row['out'], 'input': row['inp'], 'output': row['out'],
                'cache': row['cache'], 'cache_unknown_runs': row['unknown'] or 0}

    def runs_today(self, employee):
        start = time.mktime(time.localtime(self.clock())[:3] + (0, 0, 0, 0, 0, -1))
        with self.connection() as db:
            return db.execute('SELECT COUNT(*) FROM runs WHERE employee=? AND started>=?', (employee, start)).fetchone()[0]

    def exhausted_until(self, employee):
        with self.connection() as db:
            row = db.execute('SELECT until FROM exhaustion WHERE employee=?', (employee,)).fetchone()
            return row['until'] if row else None

    def runs_today_by_status(self):
        start = time.mktime(time.localtime(self.clock())[:3] + (0, 0, 0, 0, 0, -1))
        result = {}
        with self.connection() as db:
            for row in db.execute('SELECT employee, status, COUNT(*) n FROM runs WHERE started>=? GROUP BY employee, status',
                                  (start,)):
                result.setdefault(row['employee'], {})[row['status']] = row['n']
        return result

    def exhausted_all(self):
        with self.connection() as db:
            return {row['employee']: row['until'] for row in db.execute('SELECT employee, until FROM exhaustion')
                    if row['until'] > self.clock()}

    def last_digest_day(self):
        with self.connection() as db:
            row = db.execute('SELECT day FROM digests ORDER BY day DESC LIMIT 1').fetchone()
            return row['day'] if row else None

    def mark_digest(self, day):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO digests VALUES (?,?)', (day, self.clock()))

    def summary(self, *, recent=10):
        with self.connection() as db:
            totals: dict = {row['employee']: {'runs': row['runs'], 'total_input_tokens': row['inp'] or 0,
                                        'total_output_tokens': row['out'] or 0}
                      for row in db.execute('SELECT employee, COUNT(*) runs, SUM(input_tokens) inp, SUM(output_tokens) out '
                                            'FROM runs GROUP BY employee')}
            for row in db.execute('SELECT employee, until FROM exhaustion'):
                totals.setdefault(row['employee'], {})['exhausted_until'] = row['until']
            totals['recent'] = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (recent,))]
            return totals


PID_AT_TOLERANCE = 5        # Popen precedes set_pid, so a genuine owner was created before pid_at
LEGACY_SETUP_TOLERANCE = 120  # rows without pid_at: started is written before the worktree is prepared


def owner_deadline(pid_at, started):
    """Latest creation time a run's genuine owner process can have."""
    return pid_at + PID_AT_TOLERANCE if pid_at is not None else started + LEGACY_SETUP_TOLERANCE


def _process(pid):
    import psutil
    return psutil.Process(pid)


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return True
