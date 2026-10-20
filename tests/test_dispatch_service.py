"""W5 integration-3: the dispatcher as a JARVIS-owned service with workload policy gating."""
import json

import pytest

from jarvis.agents.dispatch_service import (DispatchService, ServiceBusy, codex_workload_gate,
                                            read_usage_sample)
from jarvis.core.guardrails import Guardrails

NOW = 1_800_000_000.0


def sample(used, *, resets_in=3600, age=60):
    return {'usage': {'primary': {'usedPercent': used, 'resetsAt': NOW + resets_in}}, 'sampled_at': NOW - age}


def test_no_sample_defers_to_the_ledger_and_says_so():
    assert codex_workload_gate(None, NOW) == (True, 'no_usage_sample')


def test_normal_usage_allows_codex():
    assert codex_workload_gate(sample(30), NOW) == (True, 'normal')


@pytest.mark.parametrize('used,profile', [(100, 'exhausted'), (95, 'critical'), (85, 'conserve')])
def test_low_quota_pauses_codex(used, profile):
    allowed, reason = codex_workload_gate(sample(used), NOW)
    assert not allowed and reason == 'workload_paused:' + profile


def test_stale_sample_is_treated_like_no_sample():
    # A stopped collector must not silently pause Codex forever (review finding 5).
    assert codex_workload_gate(sample(30, age=3600), NOW) == (True, 'stale_usage_sample')


def test_usage_sample_is_read_from_the_workspace(tmp_path):
    guard = Guardrails(tmp_path)
    assert read_usage_sample(guard, 'codex') is None
    folder = tmp_path / '.jarvis-dispatch' / 'usage'
    folder.mkdir(parents=True)
    (folder / 'codex.json').write_text(json.dumps(sample(30)), encoding='utf-8')
    assert read_usage_sample(guard, 'codex')['sampled_at'] == NOW - 60
    (folder / 'codex.json').write_text('not json', encoding='utf-8')
    assert read_usage_sample(guard, 'codex') is None


class Fake:
    def __init__(self, guard, *, enabled=True, interval=10, stop_after_minutes=25):
        self.guard, self.enabled, self.interval = guard, enabled, interval
        self.stop_at = NOW + stop_after_minutes * 60
        self.ticks, self.sleeps, self.now = 0, [], NOW

    def config(self):
        return {'enabled': self.enabled, 'interval_minutes': self.interval}

    def tick(self):
        self.ticks += 1

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.now >= self.stop_at:
            (self.guard.workspace / '.jarvis-stop').touch()


def make_service(fake, **extra):
    return DispatchService(fake.guard, tick=fake.tick, config=fake.config, sleep=fake.sleep,
                           clock=lambda: fake.now, register=lambda: None, **extra)


def test_service_ticks_every_interval_until_the_stop_marker(tmp_path):
    fake = Fake(Guardrails(tmp_path), stop_after_minutes=25)
    result = make_service(fake).run()
    assert result['reason'] == 'stopped' and fake.ticks == 3   # t=0, 10, 20 minutes
    assert max(fake.sleeps) <= 5                                 # polls often enough to notice the stop marker


def test_disabled_service_keeps_running_but_never_ticks(tmp_path):
    fake = Fake(Guardrails(tmp_path), enabled=False)
    make_service(fake).run()
    assert fake.ticks == 0


def test_tick_failures_are_recorded_and_do_not_kill_the_service(tmp_path):
    fake = Fake(Guardrails(tmp_path))
    errors = []

    def broken():
        fake.ticks += 1
        raise RuntimeError('boom')

    service = DispatchService(fake.guard, tick=broken, config=fake.config, sleep=fake.sleep, clock=lambda: fake.now,
                              register=lambda: None, on_error=errors.append)
    assert service.run()['reason'] == 'stopped'
    assert fake.ticks >= 2 and all(isinstance(e, RuntimeError) for e in errors)


def test_single_instance_lock(tmp_path):
    guard = Guardrails(tmp_path)
    first = make_service(Fake(guard))
    first.acquire()
    with pytest.raises(ServiceBusy):
        make_service(Fake(guard)).acquire()
    first.release()
    second = make_service(Fake(guard))
    second.acquire()
    second.release()


def test_stale_lock_from_a_dead_process_is_replaced(tmp_path):
    guard = Guardrails(tmp_path)
    lock = tmp_path / '.jarvis-dispatch' / 'service.json'
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({'pid': 999999, 'created': 1.0}), encoding='utf-8')
    service = make_service(Fake(guard))
    service.acquire()
    assert json.loads(lock.read_text(encoding='utf-8'))['pid'] != 999999
    service.release()


def test_stop_marker_present_at_start_exits_without_ticking(tmp_path):
    guard = Guardrails(tmp_path)
    (tmp_path / '.jarvis-stop').touch()
    fake = Fake(guard)
    assert make_service(fake).run()['reason'] == 'stopped' and fake.ticks == 0


# --- Review fixes (claude-code:review, 2026-10-21) ---

def test_delegate_profile_still_lets_codex_coordinate():
    assert codex_workload_gate(sample(70), NOW) == (True, 'delegate')


def test_concurrent_acquire_has_one_winner(tmp_path):
    import threading
    guard = Guardrails(tmp_path)
    results, barrier = [], threading.Barrier(4)

    def attempt():
        service = make_service(Fake(guard))
        barrier.wait()
        try:
            service.acquire()
            results.append('acquired')
        except ServiceBusy:
            results.append('busy')

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count('acquired') == 1


def test_bad_config_keeps_last_good_settings_and_service_survives(tmp_path):
    fake = Fake(Guardrails(tmp_path))
    calls, errors = {'n': 0}, []

    def flaky():
        calls['n'] += 1
        if calls['n'] % 2 == 0:
            raise ValueError('half-written config')
        return fake.config()

    service = DispatchService(fake.guard, tick=fake.tick, config=flaky, sleep=fake.sleep, clock=lambda: fake.now,
                              register=lambda: None, on_error=errors.append)
    assert service.run()['reason'] == 'stopped'
    assert fake.ticks == 3 and errors and all(isinstance(e, ValueError) for e in errors)


def test_no_good_config_ever_means_disabled(tmp_path):
    fake = Fake(Guardrails(tmp_path))
    def broken():
        raise ValueError('missing')
    service = DispatchService(fake.guard, tick=fake.tick, config=broken, sleep=fake.sleep, clock=lambda: fake.now,
                              register=lambda: None, on_error=lambda exc: None)
    assert service.run()['reason'] == 'stopped' and fake.ticks == 0


def test_registry_failure_does_not_prevent_the_service(tmp_path):
    fake = Fake(Guardrails(tmp_path))
    errors = []
    def failing_register():
        raise RuntimeError('registry full')
    service = DispatchService(fake.guard, tick=fake.tick, config=fake.config, sleep=fake.sleep, clock=lambda: fake.now,
                              register=failing_register, on_error=errors.append)
    assert service.run()['reason'] == 'stopped' and fake.ticks == 3 and len(errors) == 1


def test_child_processes_die_with_the_job(tmp_path):
    import subprocess, sys, time
    import psutil
    from jarvis.agents.dispatch_service import KillOnCloseJob
    job = KillOnCloseJob()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    job.assign(child)
    job.close()
    deadline = time.time() + 10
    while child.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    assert child.poll() is not None
