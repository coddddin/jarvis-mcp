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

from jarvis.core.guardrails import GuardrailError

BACKOFF_START, BACKOFF_MAX = 3600, 6 * 3600
LIMIT_PATTERN = re.compile(r'usage limit|rate limit|quota|\b429\b|limit reached|exceeded your|out of (?:credits|usage)', re.I)
EMPLOYEES = ('codex', 'claude-code', 'cursor')


class EmployeeSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    enabled: bool = True
    max_turns: int = Field(default=60, ge=1, le=200)


class DispatcherConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    version: Literal[1]
    enabled: bool
    interval_minutes: int = Field(ge=5, le=240)
    daily_runs: int = Field(ge=1, le=100)
    run_timeout_minutes: int = Field(ge=5, le=120)
    employees: dict[Literal['codex', 'claude-code', 'cursor'], EmployeeSettings]


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


def next_backoff(previous):
    return BACKOFF_START if not previous else min(BACKOFF_MAX, previous * 2)


def detect_limit(text):
    return bool(LIMIT_PATTERN.search(text or ''))


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
    if employee == 'cursor':
        lines.append('수습 직원이므로 파일을 고치거나 커밋하지 않는다. 검토 의견과 제안만 board_note·board_send로 남긴다.')
    return '\n'.join(lines)


CLAUDE_TOOLS = ['Read', 'Edit', 'Write', 'Glob', 'Grep',
                'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git add:*)', 'Bash(git commit:*)',
                *(f'mcp__jarvis__board_{name}' for name in
                  ('list', 'get', 'create', 'claim', 'heartbeat', 'note', 'release', 'send', 'inbox'))]


def build_command(employee, config, worktree, prompt, mcp_json, *, executable, git_dir=None, python=None):
    worktree = str(worktree)
    if employee == 'claude-code':
        return [executable, '-p', prompt, '--output-format', 'json', '--setting-sources', '',
                '--strict-mcp-config', '--mcp-config', mcp_json, '--no-session-persistence',
                '--permission-mode', 'acceptEdits', '--max-turns', str(config.employees[employee].max_turns),
                '--allowedTools', *CLAUDE_TOOLS, *([f'Bash({python}:*)'] if python else [])]
    if employee == 'codex':
        # Sandbox writes stay inside the worktree plus its git metadata; pushes are done by the dispatcher.
        return [executable, '-a', 'never', 'exec', '-C', worktree, '--sandbox', 'workspace-write',
                '--skip-git-repo-check', '--json', '--color', 'never',
                *(['--add-dir', str(git_dir)] if git_dir else []), prompt]
    if employee == 'cursor':
        # Probation: no --force, so Cursor proposes and reviews but cannot apply edits.
        return [executable, '-p', '--approve-mcps', '--trust', '--output-format', 'json',
                '--workspace', worktree, prompt]
    raise ValueError(f'No headless command for {employee}')


class DispatchLedger:
    DIRECTORY = '.jarvis-dispatch'
    DATABASE = '.jarvis-dispatch/ledger.sqlite3'

    def __init__(self, guard, *, clock=time.time, alive=None):
        self.guard, self.clock = guard, clock
        self.alive = alive or _pid_alive

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
            yield db
            db.commit()
        finally:
            db.close()

    def start(self, employee, *, pid):
        with self.connection() as db:
            return db.execute('INSERT INTO runs(employee,pid,started,status) VALUES(?,?,?,?)',
                              (employee, pid, self.clock(), 'running')).lastrowid

    def try_start(self, employee, *, daily_runs):
        """Atomically reserve a run: refused if one is live or the daily cap is reached. Returns run id or None."""
        now = self.clock()
        day_start = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        with self.connection() as db:
            for row in db.execute('SELECT id, pid FROM runs WHERE employee=? AND status="running"', (employee,)).fetchall():
                if row['pid'] is None or self.alive(row['pid']):
                    return None
                db.execute('UPDATE runs SET status="lost", ended=? WHERE id=?', (now, row['id']))
            if db.execute('SELECT COUNT(*) FROM runs WHERE employee=? AND started>=?', (employee, day_start)).fetchone()[0] >= daily_runs:
                return None
            return db.execute('INSERT INTO runs(employee,pid,started,status) VALUES(?,?,?,?)',
                              (employee, None, now, 'running')).lastrowid

    def set_pid(self, run_id, pid):
        with self.connection() as db:
            db.execute('UPDATE runs SET pid=? WHERE id=?', (pid, run_id))

    def finish(self, run_id, *, status, exit_code, input_tokens, output_tokens, note):
        now = self.clock()
        with self.connection() as db:
            row = db.execute('SELECT employee FROM runs WHERE id=?', (run_id,)).fetchone()
            db.execute('UPDATE runs SET ended=?, status=?, exit_code=?, input_tokens=?, output_tokens=?, note=? WHERE id=?',
                       (now, status, exit_code, input_tokens, output_tokens, note[:500], run_id))
            if status == 'limit':
                prior = db.execute('SELECT backoff FROM exhaustion WHERE employee=?', (row['employee'],)).fetchone()
                backoff = next_backoff(prior['backoff'] if prior else None)
                db.execute('INSERT OR REPLACE INTO exhaustion VALUES (?,?,?)', (row['employee'], now + backoff, backoff))
            elif status == 'ok':
                db.execute('DELETE FROM exhaustion WHERE employee=?', (row['employee'],))

    def running(self, employee):
        with self.connection() as db:
            row = db.execute('SELECT id, pid FROM runs WHERE employee=? AND status="running" ORDER BY id DESC LIMIT 1',
                             (employee,)).fetchone()
            if row is None:
                return None
            if row['pid'] is not None and not self.alive(row['pid']):
                db.execute('UPDATE runs SET status="lost", ended=? WHERE id=?', (self.clock(), row['id']))
                return None
            return row['id']

    def runs_today(self, employee):
        start = time.mktime(time.localtime(self.clock())[:3] + (0, 0, 0, 0, 0, -1))
        with self.connection() as db:
            return db.execute('SELECT COUNT(*) FROM runs WHERE employee=? AND started>=?', (employee, start)).fetchone()[0]

    def exhausted_until(self, employee):
        with self.connection() as db:
            row = db.execute('SELECT until FROM exhaustion WHERE employee=?', (employee,)).fetchone()
            return row['until'] if row else None

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


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return True
