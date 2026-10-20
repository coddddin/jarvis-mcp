"""Pure dispatch policy; discovery alone never authorizes a model invocation."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderCandidate(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    id: str = Field(min_length=1, max_length=120, pattern=r'^[a-zA-Z0-9_.:/-]+$')
    runtime: Literal['opencode', 'gemini', 'codex', 'claude-code']
    model: str = Field(min_length=1, max_length=120)
    capabilities: list[Literal['research', 'coding', 'review', 'summary']] = Field(max_length=4)
    cost: Literal['free', 'subscription', 'paid', 'unknown'] = 'unknown'
    available: bool = False
    access_verified: bool = False
    verified_at: datetime | None = None
    authentication_required: bool = True
    authenticated: bool = False
    remaining_calls: int | None = Field(default=None, ge=0)
    pending: int = Field(default=0, ge=0, le=100)
    max_input_tokens: int = Field(default=4096, ge=1, le=1000000)
    blocked: bool = False


class DispatchAttempt(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    provider_id: str
    reserved_tokens: int = Field(gt=0)
    actual_tokens: int | None = Field(default=None, ge=0)
    status: Literal['reserved', 'completed', 'failed', 'unknown'] = 'reserved'
    reason: Literal['', 'rate_limit', 'quota_exhausted', 'context_limit', 'authentication',
                    'invalid_response', 'timeout', 'provider_error'] = ''


class DispatchBudget(BaseModel):
    """Serializable per-request state. Caller must persist before invoking a worker."""
    model_config = ConfigDict(strict=True, extra='forbid')
    max_calls: int = Field(default=3, ge=1, le=3)
    max_tokens: int = Field(default=12000, ge=1, le=100000)
    attempts: list[DispatchAttempt] = Field(default_factory=list, max_length=3)

    @property
    def charged_tokens(self):
        # A reservation is never refunded after errors or model switching.
        return sum(max(item.reserved_tokens, item.actual_tokens or 0) for item in self.attempts)


def next_provider(candidates: list[ProviderCandidate], budget: DispatchBudget, *, capability: str,
                  input_tokens: int, output_tokens: int, now: datetime | None = None) -> dict:
    if type(output_tokens) is not int or output_tokens <= 0:
        raise ValueError('Positive output token reservation required.')
    if any(item.status in ('reserved', 'unknown') for item in budget.attempts):
        return {'status': 'reconciliation_required', 'selected': None}
    if any(item.status == 'completed' for item in budget.attempts):
        return {'status': 'already_completed', 'selected': None}
    if len(budget.attempts) >= budget.max_calls or budget.charged_tokens + input_tokens + output_tokens > budget.max_tokens:
        return {'status': 'request_budget_exhausted', 'selected': None}
    tried = {item.provider_id for item in budget.attempts}
    decision = select_provider([item for item in candidates if item.id not in tried],
                               capability=capability, input_tokens=input_tokens, now=now, free_only=True)
    decision['attempted'] = sorted(tried)
    return decision


def reserve_attempt(candidates: list[ProviderCandidate], budget: DispatchBudget, *, capability: str,
                    input_tokens: int, output_tokens: int, now: datetime | None = None) -> DispatchBudget:
    decision = next_provider(candidates, budget, capability=capability, input_tokens=input_tokens,
                             output_tokens=output_tokens, now=now)
    if decision['selected'] is None:
        raise ValueError('Dispatch refused: ' + decision['status'])
    updated = budget.model_copy(deep=True)
    updated.attempts.append(DispatchAttempt(provider_id=decision['selected'],
                                           reserved_tokens=input_tokens + output_tokens))
    return updated


def finish_attempt(budget: DispatchBudget, *, status: Literal['completed', 'failed', 'unknown'],
                   reason: str = '', actual_tokens: int | None = None) -> DispatchBudget:
    if not budget.attempts or budget.attempts[-1].status != 'reserved':
        raise ValueError('Only an active reservation can finish; duplicate results are forbidden.')
    if status not in ('completed', 'failed', 'unknown'):
        raise ValueError('Invalid dispatch outcome.')
    if (status == 'completed' and reason) or (status != 'completed' and not reason):
        raise ValueError('Outcome and failure reason disagree.')
    if reason == 'timeout' and status != 'unknown':
        raise ValueError('A timeout must be reconciled before replay.')
    updated = budget.model_copy(deep=True)
    prior = updated.attempts[-1]
    updated.attempts[-1] = DispatchAttempt(provider_id=prior.provider_id,
        reserved_tokens=prior.reserved_tokens, actual_tokens=actual_tokens, status=status, reason=reason)
    return updated


def select_provider(candidates: list[ProviderCandidate], *, capability: str,
                    input_tokens: int, now: datetime | None = None, free_only: bool = True) -> dict:
    """Select one verified seat; never invoke, retry, log in, or silently upgrade."""
    if capability not in ('research', 'coding', 'review', 'summary'):
        raise ValueError('Unknown task capability.')
    if type(input_tokens) is not int or input_tokens <= 0:
        raise ValueError('A positive input token estimate is required.')
    if type(free_only) is not bool:
        raise ValueError('free_only must be boolean.')
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Current time must include timezone.')
    if len(candidates) > 32 or len({item.id for item in candidates}) != len(candidates):
        raise ValueError('Provider candidates must have unique IDs, at most 32.')
    eligible, rejected = [], []
    for item in candidates:
        age = ((current - item.verified_at).total_seconds()
               if item.verified_at is not None and item.verified_at.tzinfo is not None else None)
        if not item.available or item.blocked:
            reason = 'unavailable'
        elif not item.access_verified:
            reason = 'access_not_verified'
        elif age is None or not 0 <= age <= 86400:
            reason = 'verification_expired'
        elif item.authentication_required and not item.authenticated:
            reason = 'authentication_required'
        elif item.cost == 'unknown' or free_only and item.cost != 'free':
            reason = 'cost_not_allowed'
        elif item.remaining_calls == 0:
            reason = 'quota_exhausted'
        elif capability not in item.capabilities:
            reason = 'capability_mismatch'
        elif input_tokens > item.max_input_tokens:
            reason = 'input_budget_exceeded'
        else:
            eligible.append(item)
            continue
        rejected.append({'id': item.id, 'reason': reason})
    # Cost first, then work backlog, then stable ID. This is not random rotation.
    cost_order = {'free': 0, 'subscription': 1, 'paid': 2}
    eligible.sort(key=lambda item: (cost_order[item.cost], item.pending, item.id))
    return {'status': 'selected' if eligible else 'no_eligible_provider',
            'selected': eligible[0].id if eligible else None,
            'eligible': [item.id for item in eligible], 'rejected': rejected,
            'free_only': free_only, 'input_tokens_estimate': input_tokens,
            'quota_note': 'Unknown remaining quota is not a promise of free availability.'}
