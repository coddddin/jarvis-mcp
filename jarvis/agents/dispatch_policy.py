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

from pydantic import BaseModel, ConfigDict, Field

# CLAUDE_TOOLS, EMPLOYEES and OPENCODE_DEFAULT_MODEL stay importable from here for existing callers.
from jarvis.agents.worker_adapters import (ADAPTERS, CLAUDE_TOOLS, EMPLOYEES, OPENCODE_DEFAULT_MODEL, WorkRequest,  # noqa: F401
                                          adapter)
from jarvis.core.guardrails import GuardrailError

BACKOFF_START, BACKOFF_MAX = 3600, 6 * 3600
LIMIT_PATTERN = re.compile(r'usage limit|rate limit|quota|\b429\b|limit reached|exceeded your|out of (?:credits|usage)', re.I)
# Employees whose work command cannot apply edits get the probation prompt (from worker_adapters).
PROBATION_PROMPT = tuple(name for name in EMPLOYEES if not ADAPTERS[name].applies_edits)
ELIGIBILITY_PATTERN = re.compile(r'FreeTierError|free tier can only be used', re.I)


class EmployeeSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    enabled: bool = True
    max_turns: int = Field(default=60, ge=1, le=200)
    model: str = Field(default='', max_length=120, pattern=r'^[A-Za-z0-9._/:#-]*$')


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


def build_digest(*, day, done, review, stale, runs, exhausted, pause, blocked=None, promotion=None):
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
    if pause.get('paused'):
        lines.append('- 디스패처 일시정지 중' + (f": {pause['reason']}" if pause.get('reason') else ''))
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


def detect_eligibility(text):
    """OpenCode's free tier refusing the request (HTTP 403 FreeTierError): not a quota, but backed off the same way."""
    return bool(ELIGIBILITY_PATTERN.search(text or ''))


def _usage_from(obj):
    usage = obj.get('usage') if isinstance(obj, dict) else None
    if not isinstance(usage, dict) or 'output_tokens' not in usage:
        return None
    fields = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
    inputs = sum(v for k, v in usage.items() if k in fields and isinstance(v, int))
    return inputs, usage['output_tokens'] if isinstance(usage['output_tokens'], int) else None


def parse_usage(text):
    """Token usage from a JSON result or the last JSONL event that reports usage; (None, None) if absent."""
    try:
        found = _usage_from(json.loads(text))
        if found:
            return found
    except (ValueError, TypeError):
        pass
    found = (None, None)
    for line in (text or '').splitlines():
        try:
            candidate = _usage_from(json.loads(line))
        except (ValueError, TypeError):
            continue
        if candidate:
            found = candidate
    return found


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


def build_command(employee, config, worktree, prompt, mcp_json, *, executable, git_dir=None, python=None):
    """Work-mode argv for one employee; the shape lives in worker_adapters."""
    settings = lambda: config.employees[employee]  # noqa: E731 - read only by employees that need it
    return adapter(employee).work_command(WorkRequest(
        str(worktree), prompt, mcp_json, executable, max_turns=lambda: settings().max_turns,
        model=lambda: settings().model, git_dir=git_dir, python=python))


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
            # W2 stage 2: when set_pid ran. Added in place for older ledgers, inside this IMMEDIATE transaction
            # so concurrent openers (service, HUD, CLI) serialize; a lost race shows up as a duplicate column.
            if 'pid_at' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                try:
                    db.execute('ALTER TABLE runs ADD COLUMN pid_at REAL')
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

    def finish(self, run_id, *, status, exit_code, input_tokens, output_tokens, note):
        now = self.clock()
        with self.connection() as db:
            row = db.execute('SELECT employee FROM runs WHERE id=?', (run_id,)).fetchone()
            db.execute('UPDATE runs SET ended=?, status=?, exit_code=?, input_tokens=?, output_tokens=?, note=? WHERE id=?',
                       (now, status, exit_code, input_tokens, output_tokens, note[:500], run_id))
            if status in ('limit', 'eligibility'):
                prior = db.execute('SELECT backoff FROM exhaustion WHERE employee=?', (row['employee'],)).fetchone()
                backoff = next_backoff(prior['backoff'] if prior else None)
                db.execute('INSERT OR REPLACE INTO exhaustion VALUES (?,?,?)', (row['employee'], now + backoff, backoff))
            elif status == 'ok':
                db.execute('DELETE FROM exhaustion WHERE employee=?', (row['employee'],))

    def running(self, employee, *, stale_after=None):
        with self.connection() as db:
            row = db.execute('SELECT id, pid, pid_at, started FROM runs WHERE employee=? AND status="running" '
                             'ORDER BY id DESC LIMIT 1', (employee,)).fetchone()
            if row is None:
                return None
            return row['id'] if self._settle(db, row, self.clock(), stale_after) else None

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
