"""W3 usage collection (board fab78e52): 5-hour and weekly usage without any model call.

Writes .jarvis-dispatch/usage/<employee>.json in the shape workload_policy reads:
{'usage': {'primary': {'usedPercent', 'resetsAt', 'windowMinutes'}, 'secondary': {...}}, 'sampled_at', 'source'}.

- Codex: the TokenCount event's rate_limits (schema in openai/codex codex-rs/protocol/src/protocol.rs:
  used_percent 0-100, resets_at Unix seconds). That Codex stores these events in ~/.codex/sessions/*.jsonl is
  observed, not documented, so the source is 'codex_rollout_observed'. Those files hold conversation text:
  only lines carrying both the token_count type marker and rate_limits are ever parsed, nothing else is
  kept, and error messages never include file content.
- Claude Code: the documented statusline input rate_limits.five_hour / seven_day (code.claude.com/docs/en/
  statusline), source 'claude_statusline'. Present only for Pro/Max subscribers after the first response.

Stale samples are not special-cased here: workload_policy already ignores samples older than its limit.
"""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import time

from jarvis.core.guardrails import GuardrailError

CODEX_SOURCE = 'codex_rollout_observed'
CLAUDE_SOURCE = 'claude_statusline'
USAGE_DIR = '.jarvis-dispatch/usage'
MAX_FILES = 3
TAIL_BYTES = 256 * 1024
EMPLOYEE = re.compile(r'[a-z][a-z0-9-]{0,39}')
TOKEN_COUNT = re.compile(rb'"type"\s*:\s*"token_count"')
RATE_LIMITS = b'"rate_limits"'


SESSIONS_ROOT = None   # override for tests (tests/conftest.py points it at an empty folder)


def default_codex_sessions():
    return Path(SESSIONS_ROOT) if SESSIONS_ROOT is not None else Path.home() / '.codex' / 'sessions'


def _linked(path):
    return path.is_symlink() or path.is_junction()


def check_sessions_root(root):
    """The Codex sessions folder: absolute, existing, a plain directory with no linked ancestor."""
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('Codex sessions folder must be an existing absolute directory.')
    cursor = root
    while cursor.parent != cursor:
        if _linked(cursor):
            raise ValueError('Linked Codex sessions folder is forbidden.')
        cursor = cursor.parent
    return root


def _session_files(root):
    """Newest rollout files under root; anything linked or escaping root is skipped."""
    resolved_root = root.resolve(strict=True)
    found = []
    for path in root.rglob('rollout-*.jsonl'):
        try:
            parts = path.relative_to(root).parts
            if any(_linked(root.joinpath(*parts[:i + 1])) for i in range(len(parts))):
                continue
            if not path.is_file() or not path.resolve(strict=True).is_relative_to(resolved_root):
                continue
            found.append((path.stat().st_mtime, path))
        except (OSError, ValueError):
            continue
    return [path for _, path in sorted(found, key=lambda item: item[0], reverse=True)[:MAX_FILES]]


def _tail_lines(path):
    with open(path, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - TAIL_BYTES))
        data = stream.read(TAIL_BYTES)
    lines = data.split(b'\n')
    return lines[1:] if size > TAIL_BYTES else lines   # the first line of a tail may be cut


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _window(raw, used_key, minutes):
    if not isinstance(raw, dict):
        return None
    used, resets = raw.get(used_key), raw.get('resets_at')
    if not _number(used) or not 0 <= used <= 100 or not _number(resets):
        return None
    window_minutes = raw.get('window_minutes', minutes)
    return {'usedPercent': float(used), 'resetsAt': int(resets),
            'windowMinutes': int(window_minutes) if _number(window_minutes) else minutes}


def _codex_sample(line):
    """A sample from one candidate line, or None. Called only for lines carrying both markers."""
    try:
        record = json.loads(line)
        payload = record['payload']
        limits = payload['rate_limits']
        when = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if payload.get('type') != 'token_count' or not isinstance(limits, dict):
        return None
    primary = _window(limits.get('primary'), 'used_percent', 300)
    if primary is None:
        return None
    usage = {'primary': primary}
    secondary = _window(limits.get('secondary'), 'used_percent', 10080)
    if secondary is not None:
        usage['secondary'] = secondary
    return {'usage': usage, 'sampled_at': when, 'source': CODEX_SOURCE}


def read_codex_usage(root=None):
    """The newest observed Codex sample, or None. Never raises with file content in the message."""
    try:
        root = check_sessions_root(root if root is not None else default_codex_sessions())
        for path in _session_files(root):
            for line in reversed(_tail_lines(path)):
                if RATE_LIMITS in line and TOKEN_COUNT.search(line):
                    sample = _codex_sample(line)
                    if sample is not None:
                        return sample
    except (OSError, ValueError):
        return None
    return None


def claude_statusline_sample(data, *, now=None):
    """A sample from Claude Code statusline input, or None when the five-hour window is absent or invalid."""
    limits = data.get('rate_limits') if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return None
    primary = _window(limits.get('five_hour'), 'used_percentage', 300)
    if primary is None:
        return None
    usage = {'primary': primary}
    secondary = _window(limits.get('seven_day'), 'used_percentage', 10080)
    if secondary is not None:
        usage['secondary'] = secondary
    return {'usage': usage, 'sampled_at': time.time() if now is None else now, 'source': CLAUDE_SOURCE}


def write_usage_sample(guard, employee, sample):
    """Atomically replace .jarvis-dispatch/usage/<employee>.json; the reader is dispatch_service.read_usage_sample."""
    if not isinstance(employee, str) or not EMPLOYEE.fullmatch(employee):
        raise ValueError('Invalid employee ID.')
    if (not isinstance(sample, dict) or not isinstance(sample.get('usage'), dict)
            or not _number(sample.get('sampled_at')) or not isinstance(sample.get('source'), str)):
        raise ValueError('Invalid usage sample.')
    guard.check_running()
    path = guard.validate_path(f'{USAGE_DIR}/{employee}.json', must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _linked(path) or _linked(path.parent):
        raise GuardrailError('Linked usage storage is forbidden.')
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps({key: sample[key] for key in ('usage', 'sampled_at', 'source')}), encoding='utf-8')
    os.replace(temp, path)


def collect_codex(guard, root=None):
    """Store the newest observed Codex sample. False (and the previous file untouched) when none is found."""
    sample = read_codex_usage(root)
    if sample is None:
        return False
    write_usage_sample(guard, 'codex', sample)
    return True


def usage_overview(guard, *, employees=('codex', 'claude-code'), now=None):
    """Last observed values for display (HUD, digest). Whether a sample is fresh enough to steer the
    dispatcher is decided by workload_policy alone; here 'fresh' only labels it."""
    from jarvis.agents.dispatch_service import read_usage_sample
    from jarvis.agents.workload_policy import MAX_SAMPLE_AGE_SECONDS
    now = time.time() if now is None else now
    view = {}
    for employee in employees:
        sample = read_usage_sample(guard, employee)
        usage = sample.get('usage') if sample else None
        primary = usage.get('primary') if isinstance(usage, dict) else None
        if not isinstance(primary, dict) or not _number(primary.get('usedPercent')) or not _number(sample.get('sampled_at')):
            continue
        secondary = usage.get('secondary') if isinstance(usage.get('secondary'), dict) else {}
        weekly = secondary.get('usedPercent')
        view[employee] = {'five_hour': float(primary['usedPercent']),
                          'weekly': float(weekly) if _number(weekly) else None,
                          'sampled_at': sample['sampled_at'],
                          'source': sample.get('source') if isinstance(sample.get('source'), str) else None,
                          'fresh': 0 <= now - sample['sampled_at'] <= MAX_SAMPLE_AGE_SECONDS}
    return view
