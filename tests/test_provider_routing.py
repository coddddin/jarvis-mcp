from datetime import datetime, timedelta, timezone
import itertools

import pytest

from jarvis.agents.provider_routing import (ProviderCandidate, select_provider, DispatchBudget,
                                           next_provider, reserve_attempt, finish_attempt)

NOW = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)


def seat(name, **changes):
    return ProviderCandidate(**dict({'id': name, 'runtime': 'opencode', 'model': name,
        'capabilities': ['coding', 'summary'], 'cost': 'free', 'available': True,
        'access_verified': True, 'verified_at': NOW, 'authentication_required': False}, **changes))


def test_free_dispatch_is_stable_by_cost_then_backlog_without_silent_paid_upgrade():
    candidates = [seat('free-b'), seat('free-a', pending=1), seat('paid', cost='paid')]
    for order in itertools.permutations(candidates):
        decision = select_provider(list(order), capability='coding', input_tokens=100, now=NOW)
        assert decision['selected'] == 'free-b'
        assert decision['eligible'] == ['free-b', 'free-a']
    decision = select_provider([seat('paid', cost='paid')], capability='coding', input_tokens=100, now=NOW)
    assert decision['status'] == 'no_eligible_provider' and decision['selected'] is None


@pytest.mark.parametrize('changes,reason', [
    ({'available': False}, 'unavailable'), ({'blocked': True}, 'unavailable'),
    ({'access_verified': False}, 'access_not_verified'),
    ({'verified_at': NOW - timedelta(days=2)}, 'verification_expired'),
    ({'verified_at': NOW + timedelta(seconds=1)}, 'verification_expired'),
    ({'verified_at': None}, 'verification_expired'),
    ({'authentication_required': True, 'authenticated': False}, 'authentication_required'),
    ({'cost': 'unknown'}, 'cost_not_allowed'), ({'remaining_calls': 0}, 'quota_exhausted'),
    ({'capabilities': ['summary']}, 'capability_mismatch'),
    ({'max_input_tokens': 50}, 'input_budget_exceeded'),
])
def test_ineligible_provider_never_selected(changes, reason):
    result = select_provider([seat('candidate', **changes)], capability='coding', input_tokens=100, now=NOW)
    assert result['selected'] is None
    assert result['rejected'] == [{'id': 'candidate', 'reason': reason}]


def test_blocked_free_provider_can_yield_to_another_verified_free_provider_only():
    result = select_provider([seat('a', blocked=True), seat('b'), seat('c', cost='subscription')],
                             capability='coding', input_tokens=100, now=NOW)
    assert result['selected'] == 'b' and result['eligible'] == ['b']


def test_paid_selection_requires_explicit_policy_and_free_still_has_priority():
    result = select_provider([seat('paid', cost='paid'), seat('free', pending=99)],
                             capability='coding', input_tokens=100, now=NOW, free_only=False)
    assert result['selected'] == 'free'


def test_duplicate_ids_and_invalid_estimates_or_time_are_rejected():
    with pytest.raises(ValueError):
        select_provider([seat('x'), seat('x')], capability='coding', input_tokens=1, now=NOW)
    with pytest.raises(ValueError):
        select_provider([], capability='coding', input_tokens=True, now=NOW)
    with pytest.raises(ValueError):
        select_provider([], capability='coding', input_tokens=1, now=NOW.replace(tzinfo=None))


def request(candidates, budget):
    return reserve_attempt(candidates, budget, capability='coding', input_tokens=100, output_tokens=200, now=NOW)


def test_quota_failover_preserves_shared_budget_across_serialization():
    candidates = [seat('a'), seat('b'), seat('c'), seat('paid', cost='paid')]
    budget = DispatchBudget(max_tokens=1000)
    for name in ('a', 'b', 'c'):
        budget = request(candidates, budget)
        assert budget.attempts[-1].provider_id == name
        budget = finish_attempt(budget, status='failed', reason='quota_exhausted')
        budget = DispatchBudget.model_validate_json(budget.model_dump_json())
    assert budget.charged_tokens == 900 and len(budget.attempts) == 3
    with pytest.raises(ValueError, match='request_budget_exhausted'):
        request(candidates, budget)


def test_timeout_or_inflight_reservation_never_blindly_replays():
    candidates = [seat('a'), seat('b')]
    budget = request(candidates, DispatchBudget())
    with pytest.raises(ValueError, match='reconciliation_required'):
        request(candidates, budget)
    budget = finish_attempt(budget, status='unknown', reason='timeout')
    with pytest.raises(ValueError, match='reconciliation_required'):
        request(candidates, budget)


def test_success_is_not_repeated_and_actual_usage_cannot_refund_reserved_tokens():
    candidates = [seat('a'), seat('b')]
    budget = request(candidates, DispatchBudget())
    budget = finish_attempt(budget, status='completed', actual_tokens=50)
    assert budget.charged_tokens == 300
    with pytest.raises(ValueError, match='already_completed'):
        request(candidates, budget)
    with pytest.raises(ValueError, match='duplicate'):
        finish_attempt(budget, status='completed')


def test_actual_usage_over_estimate_stops_next_provider_at_total_cap():
    candidates = [seat('a'), seat('b')]
    budget = request(candidates, DispatchBudget(max_tokens=600))
    budget = finish_attempt(budget, status='failed', reason='rate_limit', actual_tokens=500)
    assert budget.charged_tokens == 500
    with pytest.raises(ValueError, match='request_budget_exhausted'):
        request(candidates, budget)
