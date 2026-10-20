"""Workload policy based on account-shared Codex usage windows.

Usage comes from the account usage API and is shared by all Codex chats;
it is not a per-session token count. Pure policy: no IO, no probing,
no payment, reset or login actions.
"""

import math

KNOWN_AGENTS = ("codex", "claude", "free-verified", "gemini-verified")
DELEGATE_ORDER = ("free-verified", "claude", "gemini-verified")
MAX_SAMPLE_AGE_SECONDS = 600.0
SCOPE = "account_shared_not_session_tokens"

REASONS = {
    "normal": "quota_normal_codex_may_own_bulk_work",
    "delegate": "quota_low_delegate_bulk_work_codex_coordinates",
    "conserve": "quota_conserve_delegate_or_pause_bulk_work",
    "critical": "quota_critical_delegate_or_pause_bulk_work",
    "exhausted": "quota_exhausted_codex_paused_no_paid_fallback",
    "unknown": "quota_unknown_treated_conservatively",
}


def _is_num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _window_remaining(window, now):
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    resets_at = window.get("resetsAt")
    if not _is_num(used) or not 0 <= used <= 100:
        return None
    # An expired reset window means unknown; do not assume it reset.
    if not _is_num(resets_at) or resets_at <= now:
        return None
    return 100.0 - float(used)


def _remaining_percent(usage, sampled_at, now):
    if not isinstance(usage, dict):
        return None
    if not _is_num(sampled_at) or not _is_num(now):
        return None
    age = now - sampled_at
    if not 0 <= age <= MAX_SAMPLE_AGE_SECONDS:
        return None
    if usage.get("primary") is None:
        return None
    values = []
    for key in ("primary", "secondary"):
        window = usage.get(key)
        if window is None:
            continue
        remaining = _window_remaining(window, now)
        if remaining is None:
            # Any provided but invalid window makes the result unknown.
            return None
        values.append(remaining)
    return min(values) if values else None


def _profile(remaining):
    if remaining is None:
        return "unknown"
    if remaining <= 0:
        return "exhausted"
    if remaining >= 40:
        return "normal"
    if remaining >= 20:
        return "delegate"
    if remaining >= 10:
        return "conserve"
    return "critical"


def _normalize_agents(available_agents):
    if not isinstance(available_agents, (list, tuple)):
        return set()
    # Free verification is the caller's assertion; it is not probed here.
    return {a for a in available_agents if isinstance(a, str) and a in KNOWN_AGENTS}


def _first_delegate(agents):
    for agent in DELEGATE_ORDER:
        if agent in agents:
            return agent
    return None


def plan_workload(usage, available_agents, sampled_at, now):
    """Plan bulk work ownership from account-shared usage windows."""
    remaining = _remaining_percent(usage, sampled_at, now)
    profile = _profile(remaining)
    agents = _normalize_agents(available_agents)
    codex_available = "codex" in agents
    delegate = _first_delegate(agents)

    if profile == "normal":
        bulk_owner = "codex" if codex_available else delegate
        codex_role = "normal"
        max_workers = 1 if codex_available else 0
    elif profile == "delegate":
        if delegate is not None:
            bulk_owner = delegate
            codex_role = "coordination_and_validation"
        elif codex_available:
            # No other agent: Codex keeps bulk work but starts no new workers.
            bulk_owner = "codex"
            codex_role = "coordination_and_validation"
        else:
            bulk_owner = None
            codex_role = "paused"
        max_workers = 0
    else:
        # conserve, critical, exhausted and unknown are all conservative.
        bulk_owner = delegate
        if profile == "exhausted" or delegate is None:
            # Pause bulk work rather than spend more Codex quota.
            codex_role = "paused"
        else:
            codex_role = "coordination_and_validation"
        max_workers = 0

    return {
        "profile": profile,
        "remaining_percent": remaining,
        "bulk_owner": bulk_owner,
        "codex_role": codex_role,
        "max_new_codex_workers": max_workers,
        "required_validation": True,
        "scope": SCOPE,
        "reason": REASONS[profile],
    }


def transition_workload(previous, current, *, active_owner=None):
    """Recovery applies to new work; never cancel or replay an active task."""
    return {**current,
            'recovered': previous.get('profile') != 'normal' and current['profile'] == 'normal',
            'apply_to': 'new_tasks_only', 'active_owner': active_owner,
            'cancel_active': False, 'replay_completed': False}
