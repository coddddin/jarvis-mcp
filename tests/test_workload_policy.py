import math

import pytest

from jarvis.agents.workload_policy import plan_workload, transition_workload

NOW = 10000.0


def usage(primary=92, secondary=14):
    return {'primary': {'usedPercent': primary, 'resetsAt': NOW+100},
            'secondary': {'usedPercent': secondary, 'resetsAt': NOW+1000}}


@pytest.mark.parametrize('used,profile', [(0,'normal'),(60,'normal'),(61,'delegate'),
    (80,'delegate'),(81,'conserve'),(90,'conserve'),(91,'critical'),(100,'exhausted')])
def test_thresholds_preserve_validation_and_do_not_spawn_in_low_quota(used, profile):
    result = plan_workload(usage(used, 0), ['codex','claude'], NOW, NOW)
    assert result['profile'] == profile and result['required_validation']
    assert result['max_new_codex_workers'] == (1 if profile == 'normal' else 0)
    assert result['bulk_owner'] == ('codex' if profile == 'normal' else 'claude')


def test_shared_pool_uses_most_restrictive_window_and_verified_free_priority():
    result = plan_workload(usage(10,95), ['codex','claude','free-verified'], NOW, NOW)
    assert result['remaining_percent'] == 5 and result['bulk_owner'] == 'free-verified'
    assert result['scope'] == 'account_shared_not_session_tokens'


@pytest.mark.parametrize('sample,now', [(NOW-601,NOW),(NOW+1,NOW),(math.nan,NOW),(NOW,math.inf)])
def test_bad_or_stale_sample_cannot_claim_recovery(sample, now):
    current = plan_workload(usage(0,0), ['codex','claude'], sample, now)
    result = transition_workload({'profile':'critical'},current,active_owner='claude')
    assert result['profile'] == 'unknown' and result['remaining_percent'] is None
    assert result['bulk_owner'] == 'claude' and not result['recovered']


@pytest.mark.parametrize('invalid', [True,-1,101,math.nan,'2'])
def test_invalid_usage_never_invents_remaining_tokens(invalid):
    assert plan_workload(usage(invalid), ['claude'], NOW,NOW)['profile'] == 'unknown'


def test_no_delegate_at_critical_pauses_bulk_and_reset_time_alone_is_not_recovery():
    assert plan_workload(usage(),['codex'],NOW,NOW)['bulk_owner'] is None
    expired = usage(0,0)
    expired['primary']['resetsAt'] = NOW
    assert plan_workload(expired,['codex'],NOW,NOW)['profile'] == 'unknown'


def test_fresh_real_recovery_changes_only_new_tasks_and_preserves_active_claude_work():
    current = plan_workload(usage(0,0), ['codex','claude'],NOW,NOW)
    result = transition_workload({'profile':'critical'},current,active_owner='claude')
    assert result['recovered'] and result['bulk_owner'] == 'codex'
    assert result['active_owner'] == 'claude' and not result['cancel_active']
    assert result['apply_to'] == 'new_tasks_only' and not result['replay_completed']
