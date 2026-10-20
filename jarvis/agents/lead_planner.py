"""W5 local lead: when the board is idle, turn one unchecked PLAN item into a board task and assign it.

Pure orchestration. The model judgment (Ollama), the board and the ledger are injected, so this module
never calls a model or imports the board feature. Sensitive work is never created as a task: the lead
only asks user:heejun for approval. One action per tick, bounded by a daily cap; a tick first reserves
its slot in the ledger so concurrent ticks cannot double-create or exceed the cap.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import re
import sqlite3
import time

from jarvis.agents.dispatch_service import codex_workload_gate
from jarvis.agents.workload_policy import plan_workload
from jarvis.core.guardrails import GuardrailError

LEAD_AGENT = 'local:lead'
APPROVER = 'user:heejun'
BUSY_STATUSES = ('open', 'claimed')
TIERS = ('light', 'standard', 'sensitive')
CAPABILITIES = ('research', 'coding', 'review', 'summary')
MAX_CANDIDATES = 10
RESERVATION_SECONDS = 600
TAG = re.compile(r'\[PLAN:([0-9a-f]{12})\]')
UNCHECKED = re.compile(r'^\s*[-*] \[ \]\s+(.+?)\s*$')
HEADING = re.compile(r'^(#{2,4})\s+(.+?)\s*$')
# Optional stable ID in a PLAN item (<!-- plan:w4-2 -->): rewording the item then keeps the same key.
STABLE_ID = re.compile(r'<!--\s*plan:([A-Za-z0-9._-]{1,40})\s*-->')
# Stems that always force approval, whatever the model says. The model may raise a tier, never lower it.
# Checked against item text, section heading and model title. Over-matching only costs an approval message.
SENSITIVE_WORDS = re.compile(
    r'토큰|인증|권한|암호|비밀|자격\s*증명|삭제|제거|초기화|외부|전송|업로드|배포|병합|머지|푸시|덮어|'
    r'\bAPI\s*키|키\s*발급|token|auth|credential|secret|password|passwd|api[\s_-]?key|permission|'
    r'merg|delet|remov|wipe|purge|reset|push|deploy|upload|publish|overwrit|exfil', re.I)
# Force approval only when the work may change code or config; explicit Markdown-only work stays allowed.
CHANGE_SENSITIVE_WORDS = re.compile(r'복원|백업|restor|backup', re.I)
# Paths whose change always needs approval (prefix match, case-insensitive).
SENSITIVE_PATHS = ('config/', 'scripts/backup/', '.jarvis-auth', '.env', '.git', '.claude/', '.cursor/',
                   '.codex/', 'sources/', 'agents.md', 'claude.md', 'agents_context.md', 'google_auth_setup.py',
                   'jarvis/core/guardrails.py', 'jarvis/core/roster.py', 'jarvis/core/redaction.py',
                   'jarvis/app/runtime.py', 'requirements')
# Roster employee ID -> name used by workload_policy.
WORKLOAD_NAMES = {'codex': 'codex', 'claude-code': 'claude'}


@dataclass(frozen=True)
class Candidate:
    key: str        # 12 hex chars: sha256 of the stable ID if present, else of the normalized text
    section: str
    text: str


def _normalize(text):
    return ' '.join(text.replace('**', '').replace('`', '').split())


def item_key(text):
    stable = STABLE_ID.search(text)
    basis = 'id:' + stable.group(1) if stable else _normalize(STABLE_ID.sub('', text))
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:12]


def plan_candidates(plan_text):
    """Unchecked '- [ ]' items in PLAN order, de-duplicated by key.

    Without a stable ID the key is the text alone, not the section: PLAN mirrors some items in two
    sections, and a section-scoped key would create the same work twice."""
    section, seen, found = '', set(), []
    for line in plan_text.splitlines():
        heading = HEADING.match(line)
        if heading:
            section = _normalize(heading.group(2))
            continue
        item = UNCHECKED.match(line)
        if not item:
            continue
        key, text = item_key(item.group(1)), _normalize(STABLE_ID.sub('', item.group(1)))
        if text and key not in seen:
            seen.add(key)
            found.append(Candidate(key, section, text))
    return found


def _day_start(now):
    return time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))


def board_is_idle(board):
    return all(not board.list(status=status, limit=1)['tasks'] for status in BUSY_STATUSES)


def tagged_keys(board):
    """Plan keys already on the board under any status (newest 100 per status)."""
    keys = set()
    for status in ('open', 'claimed', 'blocked', 'review', 'done', 'cancelled'):
        for task in board.list(status=status, limit=100)['tasks']:
            keys.update(TAG.findall(task['title']))
    return keys


def _safe_paths(values):
    if not isinstance(values, list) or len(values) > 20:
        return []
    paths = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 300 or '\\' in value or ':' in value:
            return []
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts:
            return []
        paths.append(value)
    return paths


def _covers_sensitive(path):
    """True if the path is inside a sensitive path or is a folder containing one ('.' is the whole repo)."""
    path = str(PurePosixPath(path)).lower()  # './x' -> 'x', 'a/b/' -> 'a/b'
    if path == '.':
        return True
    return path.startswith(SENSITIVE_PATHS) or any(item.startswith(path + '/') for item in SENSITIVE_PATHS)


def is_sensitive(candidate, capability, title, paths):
    words = ' '.join((candidate.text, candidate.section, title))
    if SENSITIVE_WORDS.search(words):
        return True
    if any(_covers_sensitive(p) for p in paths) or (capability == 'coding' and not paths):
        return True  # coding with no declared scope may touch anything
    # Docs-only means explicit paths, all Markdown, and no coding; anything vaguer counts as a change.
    docs_only = capability != 'coding' and bool(paths) and all(p.lower().endswith('.md') for p in paths)
    return bool(CHANGE_SENSITIVE_WORDS.search(words)) and not docs_only


def validate_judgment(raw, candidates):
    """Model output -> (candidate, capability, tier, title, paths) or None when anything is off."""
    if not isinstance(raw, dict):
        return None
    index, capability, tier = raw.get('index'), raw.get('capability'), raw.get('tier')
    if type(index) is not int or not 0 <= index < len(candidates):
        return None
    if capability not in CAPABILITIES or tier not in TIERS:
        return None
    candidate = candidates[index]
    paths = _safe_paths(raw.get('paths', []))
    title = raw.get('title')
    if not isinstance(title, str) or not title.strip():
        title = candidate.text
    title = _normalize(title)[:170]
    if is_sensitive(candidate, capability, title, paths):
        tier = 'sensitive'
    return candidate, capability, tier, title, paths


def eligible_employees(roster, capability, tier):
    """Roster order, roster facts only: active status, the capability, and a max_tier covering the tier.

    Probation employees are never auto-assigned: the capability label comes from the model, so an
    implementation item mislabelled 'review' must not reach them. Humans can still assign them reviews."""
    rank = TIERS.index(tier)
    return [name for name, agent in roster.agents.items()
            if agent.status == 'active' and capability in agent.capabilities
            and TIERS.index(agent.max_tier) >= rank]


def choose_employee(names, usage_sample, now):
    """(employee or None, workload label).

    Codex is excluded only by dispatch_service.codex_workload_gate (a fresh sample saying 'paused'), so a
    missing, stale or unparsed sample never shuts Codex out. With a usable sample the bulk owner wins."""
    allowed, gate = codex_workload_gate(usage_sample, now)
    if not allowed:
        names = [name for name in names if name != 'codex']
    if gate.endswith('_usage_sample'):  # no, stale or unparsed sample: no workload preference
        return (names[0] if names else None), gate
    workload = plan_workload(usage_sample.get('usage'), [WORKLOAD_NAMES.get(n, n) for n in names],
                             usage_sample.get('sampled_at'), now)
    for name in names:
        if WORKLOAD_NAMES.get(name) == workload['bulk_owner']:
            return name, workload['profile']
    return (names[0] if names else None), workload['profile']


def assignment_message(task, candidate, capability):
    return (f'[요청] 팀장 배정: {task["title"]}'[:200] +
            f'\n- 할 일: PLAN 항목({candidate.section}) 처리. 역량: {capability}.'
            '\n- 방법: board_claim 후 진행, 끝나면 board_release(review)로 커밋·검증 결과를 남긴다.'
            f'\n- 참고: task={task["id"][:8]}, plan={candidate.key}, file=docs/PLAN.md'
            '\n- 답은 한국어로.')


def approval_message(candidate, capability):
    text = candidate.text[:600]
    return (f'[검토] 민감 작업 승인 요청 (plan={candidate.key})'
            f'\n- 할 일: {text}'
            f'\n- 상태: 팀장이 민감 등급으로 판단해 작업을 만들지 않음. 역량: {capability}.'
            '\n- 필요한 것: 진행하려면 사용자가 직접 작업을 만들거나 지시해 주세요.')[:1000]


def plan_next(board, roster, plan_text, *, judge, ledger, now, usage_sample=None, daily_cap=3, agent=LEAD_AGENT):
    """One lead tick. Returns {'action': ..., 'reason': ..., ...}; at most one task or approval request.

    usage_sample has the dispatch_service shape: {'usage': {...}, 'sampled_at': epoch} or None.
    The ledger slot is reserved before anything else and released unless an action is recorded."""
    slot, refusal = ledger.reserve(now, _day_start(now), daily_cap)
    if slot is None:
        return {'action': 'none', 'reason': refusal}
    try:
        result = _tick(board, roster, plan_text, judge, ledger, now, usage_sample, agent, slot)
    finally:
        ledger.release(slot)  # no-op once committed
    return result


def _tick(board, roster, plan_text, judge, ledger, now, usage_sample, agent, slot):
    if not board_is_idle(board):
        return {'action': 'none', 'reason': 'board_busy'}
    done = tagged_keys(board) | ledger.keys()
    candidates = [c for c in plan_candidates(plan_text) if c.key not in done][:MAX_CANDIDATES]
    if not candidates:
        return {'action': 'none', 'reason': 'no_candidates'}
    payload = {
        'candidates': [{'index': i, 'section': c.section, 'text': c.text} for i, c in enumerate(candidates)],
        'capabilities': list(CAPABILITIES), 'tiers': list(TIERS),
        'answer': 'JSON {index, capability, tier, title, paths}',
    }
    try:
        judged = validate_judgment(judge(payload), candidates)
    except Exception as exc:  # the model is untrusted; any failure means no action this tick
        return {'action': 'none', 'reason': 'judge_error', 'error': type(exc).__name__}
    if judged is None:
        return {'action': 'none', 'reason': 'judge_invalid'}
    candidate, capability, tier, title, paths = judged

    if tier == 'sensitive':
        sent = board.send(agent, APPROVER, approval_message(candidate, capability))
        ledger.commit(slot, candidate.key, 'approval_requested', str(sent.get('id', '')))
        return {'action': 'approval_requested', 'reason': 'sensitive', 'plan': candidate.key}

    employee, workload = choose_employee(eligible_employees(roster, capability, tier), usage_sample, now)
    if employee is None:
        return {'action': 'none', 'reason': 'no_eligible_employee', 'plan': candidate.key, 'workload': workload}

    detail = (f'plan_item_hash: {candidate.key}\nsection: {candidate.section}\ncapability: {capability}\n'
              f'tier: {tier}\nassigned: {employee} (workload {workload})\n\n{candidate.text}')[:2000]
    task = board.create(agent, f'[PLAN:{candidate.key}] {title}', detail, paths)
    ledger.commit(slot, candidate.key, 'task_created', task['id'])
    board.send(agent, employee, assignment_message(task, candidate, capability), task_id=task['id'])
    return {'action': 'task_created', 'reason': 'assigned', 'plan': candidate.key, 'task_id': task['id'],
            'employee': employee, 'workload': workload}


class LeadLedger:
    """What the lead did, one row per created task or approval request (dedupe + daily cap).

    A tick first inserts a 'reserved' row in the same transaction that checks the cap and that no other
    tick is in flight; the row becomes the action (commit) or is deleted (release). A reservation older
    than RESERVATION_SECONDS is treated as a crashed tick and removed."""
    DIRECTORY = '.jarvis-dispatch'
    DATABASE = '.jarvis-dispatch/lead.sqlite3'

    def __init__(self, guard):
        self.guard = guard

    @contextmanager
    def connection(self):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY, must_exist=False)
        if directory.is_symlink() or directory.is_junction():
            raise GuardrailError('Linked dispatch storage is forbidden.')
        directory.mkdir(exist_ok=True)
        db = sqlite3.connect(self.guard.validate_path(self.DATABASE, must_exist=False), timeout=5, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS lead_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'plan_key TEXT UNIQUE, action TEXT NOT NULL, at REAL NOT NULL, ref TEXT NOT NULL DEFAULT "")')
            yield db
            db.commit()
        finally:
            db.close()

    def reserve(self, now, day_start, daily_cap):
        """(slot id, None) or (None, 'tick_in_progress' | 'daily_cap')."""
        with self.connection() as db:
            db.execute('DELETE FROM lead_actions WHERE action="reserved" AND at<?', (now - RESERVATION_SECONDS,))
            if db.execute('SELECT 1 FROM lead_actions WHERE action="reserved"').fetchone():
                return None, 'tick_in_progress'
            if db.execute('SELECT COUNT(*) FROM lead_actions WHERE at>=?', (day_start,)).fetchone()[0] >= daily_cap:
                return None, 'daily_cap'
            return db.execute('INSERT INTO lead_actions(plan_key, action, at) VALUES (NULL, "reserved", ?)',
                              (now,)).lastrowid, None

    def commit(self, slot, plan_key, action, ref=''):
        with self.connection() as db:
            if db.execute('SELECT 1 FROM lead_actions WHERE plan_key=?', (plan_key,)).fetchone():
                db.execute('DELETE FROM lead_actions WHERE id=? AND action="reserved"', (slot,))
                return
            updated = db.execute('UPDATE lead_actions SET plan_key=?, action=?, ref=? WHERE id=? AND action="reserved"',
                                 (plan_key, action, ref[:100], slot)).rowcount
            if not updated:  # reservation expired meanwhile: still record what was done
                db.execute('INSERT OR IGNORE INTO lead_actions(plan_key, action, at, ref) VALUES (?,?,?,?)',
                           (plan_key, action, time.time(), ref[:100]))

    def release(self, slot):
        with self.connection() as db:
            db.execute('DELETE FROM lead_actions WHERE id=? AND action="reserved"', (slot,))

    def keys(self):
        with self.connection() as db:
            return {row[0] for row in db.execute('SELECT plan_key FROM lead_actions WHERE plan_key IS NOT NULL')}
