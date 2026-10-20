"""Windowed call budgets: recover over time, keep stricter tiers and daily caps."""
import pytest

from jarvis.app.runtime import TOOL_TIERS
from jarvis.core.guardrails import GuardrailError, Guardrails


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0  # fixed local-day independent start

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def guard(tmp_path, clock):
    return Guardrails(tmp_path, clock=clock)


def spend(guard, count, tier='standard'):
    for _ in range(count):
        guard.enter_call(tier)


def test_window_budget_recovers_without_restart(guard, clock):
    spend(guard, Guardrails.MAX_CALLS)
    with pytest.raises(GuardrailError, match='budget') as error:
        guard.enter_call()
    assert 'retry after' in str(error.value)
    clock.now += Guardrails.WINDOW_SECONDS - 1
    with pytest.raises(GuardrailError, match='budget'):
        guard.enter_call()
    clock.now += 1
    guard.enter_call()  # the 21st call succeeds once the window rolls over
    assert guard._calls == 1


def test_sensitive_tier_has_its_own_stricter_window_cap(guard):
    spend(guard, Guardrails.MAX_SENSITIVE, 'sensitive')
    with pytest.raises(GuardrailError, match='sensitive'):
        guard.enter_call('sensitive')
    guard.enter_call('standard')  # ordinary tools are still available
    assert guard._calls == Guardrails.MAX_SENSITIVE + 1


def test_sensitive_calls_also_count_toward_the_window_total(guard):
    spend(guard, Guardrails.MAX_CALLS - 1)
    guard.enter_call('sensitive')
    with pytest.raises(GuardrailError, match='budget'):
        guard.enter_call('standard')


def test_light_tier_does_not_consume_the_standard_window(guard):
    spend(guard, Guardrails.MAX_LIGHT, 'light')
    assert guard._calls == 0
    guard.enter_call('standard')
    with pytest.raises(GuardrailError, match='light'):
        guard.enter_call('light')


def test_daily_cap_holds_across_windows(guard, clock):
    total = 0
    while total < Guardrails.DAILY_MAX:
        batch = min(Guardrails.MAX_CALLS, Guardrails.DAILY_MAX - total)
        spend(guard, batch)
        total += batch
        clock.now += Guardrails.WINDOW_SECONDS
    with pytest.raises(GuardrailError, match='daily'):
        guard.enter_call()
    clock.now += 24 * 3600
    guard.enter_call()


def test_daily_sensitive_cap(guard, clock):
    total = 0
    while total < Guardrails.DAILY_SENSITIVE:
        batch = min(Guardrails.MAX_SENSITIVE, Guardrails.DAILY_SENSITIVE - total)
        spend(guard, batch, 'sensitive')
        total += batch
        clock.now += Guardrails.WINDOW_SECONDS
    with pytest.raises(GuardrailError, match='daily'):
        guard.enter_call('sensitive')
    guard.enter_call('standard')


def test_stop_marker_still_blocks_every_tier(guard):
    (guard.workspace / '.jarvis-stop').touch()
    for tier in ('light', 'standard', 'sensitive'):
        with pytest.raises(GuardrailError, match='stopped'):
            guard.enter_call(tier)


def test_unknown_tier_is_rejected(guard):
    with pytest.raises(GuardrailError, match='tier'):
        guard.enter_call('free')


def test_refused_calls_are_not_counted(guard, clock):
    spend(guard, Guardrails.MAX_CALLS)
    for _ in range(5):
        with pytest.raises(GuardrailError):
            guard.enter_call()
    clock.now += Guardrails.WINDOW_SECONDS
    spend(guard, Guardrails.MAX_CALLS)


def test_status_reports_usage_and_reset(guard, clock):
    spend(guard, 3)
    spend(guard, 1, 'sensitive')
    status = guard.budget_status()
    assert status['window']['used'] == 4 and status['window']['sensitive_used'] == 1
    assert status['window']['resets_at'] == pytest.approx(clock.now + Guardrails.WINDOW_SECONDS)
    assert status['daily']['used'] == 4


def test_every_registered_tool_has_a_known_tier():
    import asyncio
    import tempfile
    from pathlib import Path
    import server
    with tempfile.TemporaryDirectory() as folder:
        tools = asyncio.run(server.create_server(Guardrails(Path(folder).resolve())).list_tools())
    names = {tool.name for tool in tools}
    assert set(TOOL_TIERS) <= names  # no stale names in the tier table
    sensitive = {name for name, tier in TOOL_TIERS.items() if tier == 'sensitive'}
    assert {'apply_file_organization', 'undo_file_organization', 'restore_project_snapshot',
            'start_workflow', 'stop_workflow', 'clear_local_index', 'take_screenshot'} <= sensitive
    assert all(TOOL_TIERS[name] == 'light' for name in names if name.startswith('board_'))
