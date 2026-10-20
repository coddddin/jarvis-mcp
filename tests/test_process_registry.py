import os
import json
import time
from types import SimpleNamespace
import psutil
import pytest

from jarvis.agents.process_registry import ProcessRegistry, ProcessRegistryError, observe_spawned
from jarvis.core.guardrails import GuardrailError, Guardrails


class ObservedProcess:
    def __init__(self, created, parent=0, *, alive=True, status=psutil.STATUS_RUNNING):
        self.created, self.parent, self.alive, self.state = created, parent, alive, status

    def create_time(self):
        return self.created

    def ppid(self):
        return self.parent

    def is_running(self):
        return self.alive

    def status(self):
        return self.state


@pytest.fixture
def registry(tmp_path):
    current = os.getpid()
    processes = {current: ObservedProcess(1000.0, 42), 42: ObservedProcess(900.0)}
    def lookup(pid):
        value = processes.get(pid)
        if value == 'denied':
            raise psutil.AccessDenied(pid)
        if value is None:
            raise psutil.NoSuchProcess(pid)
        return value
    clock = [1005.0]
    store = ProcessRegistry(Guardrails(tmp_path), clock=lambda: clock[0], lookup=lookup)
    return store, processes, clock


def register(store, **changes):
    return store.register_current(**{'kind':'verification', 'label':'합성 검증',
                                    'max_runtime_seconds':60, **changes})


def test_registration_reopens_without_extending_runtime_or_changing_identity(registry):
    store, _, clock = registry
    original = register(store)
    clock[0] += 20
    assert register(store) == original
    reopened = ProcessRegistry(store.guard, clock=store.clock, lookup=store.lookup)
    result = reopened.inspect()['processes'][0]
    assert result['id'] == original['id'] and result['registered_at'] == 1005
    assert result['warnings'] == [] and result['process_state'] == 'running'
    with pytest.raises(ProcessRegistryError, match='cannot be reset'):
        store.register_current(kind='verification', label='합성 검증', max_runtime_seconds=120)


def test_only_caller_and_direct_children_can_be_registered(registry):
    store, processes, _ = registry
    child = os.getpid() + 100000
    processes[child] = ObservedProcess(1002.0, os.getpid())
    saved = store.register_child(child, kind='worker', label='child', max_runtime_seconds=60)
    assert saved['parent_pid'] == os.getpid() and saved['parent_created'] == 1000
    processes[child+1] = ObservedProcess(1003.0, 123)
    with pytest.raises(ProcessRegistryError, match='direct child'):
        store.register_child(child+1, kind='worker', label='unrelated', max_runtime_seconds=60)
    with pytest.raises(ProcessRegistryError, match='direct child'):
        store.register_child(os.getpid(), kind='worker', label='self', max_runtime_seconds=60)
    assert len(store.inspect()['processes']) == 1


@pytest.mark.parametrize('change,expected,warnings', [
    ('gone','exited',[]), ('reused','pid_reused',['process_pid_reused']),
    ('denied','unverifiable',['process_unverifiable']), ('zombie','exited',[]),
])
def test_process_liveness_is_not_inferred_from_pid_alone(registry, change, expected, warnings):
    store, processes, _ = registry
    register(store)
    if change == 'gone':
        processes.pop(os.getpid())
    elif change == 'reused':
        processes[os.getpid()].created += 1
    elif change == 'denied':
        processes[os.getpid()] = 'denied'
    else:
        processes[os.getpid()].state = psutil.STATUS_ZOMBIE
    observation = store.inspect()['processes'][0]
    assert observation['process_state'] == expected and observation['warnings'] == warnings


@pytest.mark.parametrize('parent', ['gone','reused','denied'])
def test_parent_loss_reuse_and_denied_access_are_distinguished(registry, parent):
    store, processes, _ = registry
    register(store)
    if parent == 'gone':
        processes.pop(42)
    elif parent == 'reused':
        processes[42].created += 1
    else:
        processes[42] = 'denied'
    observed = store.inspect()['processes'][0]
    assert observed['warnings'] == (['parent_unverifiable'] if parent == 'denied' else ['parent_exited'])
    assert store.inspect()['termination_supported'] is False


def test_timeout_uses_original_process_start_even_after_registry_restart(registry):
    store, _, clock = registry
    register(store)
    clock[0] = 1060
    assert store.inspect()['processes'][0]['warnings'] == ['runtime_exceeded']
    clock[0] = 999
    assert store.inspect()['processes'][0]['warnings'] == ['clock_regressed']


def test_registration_identity_change_is_rejected_before_storage(registry, monkeypatch):
    store, processes, _ = registry
    values = iter([1000.0, 1001.0])
    monkeypatch.setattr(processes[os.getpid()], 'create_time', lambda: next(values))
    with pytest.raises(ProcessRegistryError, match='identity changed'):
        register(store)
    assert store.inspect()['processes'] == []


@pytest.mark.parametrize('field,value', [('kind','other'),('label','token=private-value'),
    ('label','bad\nlabel'),('max_runtime_seconds',True),('max_runtime_seconds',0)])
def test_invalid_or_sensitive_registration_data_is_rejected(registry, field, value):
    store, _, _ = registry
    args = {'kind':'verification','label':'bounded','max_runtime_seconds':60}
    args[field] = value
    with pytest.raises(ProcessRegistryError):
        store.register_current(**args)
    assert store.inspect()['processes'] == []


def test_capacity_stop_and_inspection_limit_are_enforced(registry):
    store, _, _ = registry
    store.MAX_RECORDS = 0
    with pytest.raises(ProcessRegistryError, match='capacity'):
        register(store)
    store.MAX_RECORDS = 1000
    with pytest.raises(ProcessRegistryError):
        store.inspect(limit=True)
    (store.guard.workspace / '.jarvis-stop').write_text('stop')
    with pytest.raises(GuardrailError):
        store.inspect()


def test_hardlinked_sqlite_sibling_is_rejected_without_touching_target(registry):
    store, _, _ = registry
    target = store.guard.workspace / 'unrelated.txt'
    target.write_text('preserve')
    sibling = store.guard.workspace / store.DIRECTORY / 'processes.sqlite3-wal'
    os.link(target, sibling)
    with pytest.raises(GuardrailError):
        store.inspect()
    assert target.read_text() == 'preserve'


def test_real_self_registration_reports_no_command_line_or_environment(tmp_path):
    store = ProcessRegistry(Guardrails(tmp_path))
    store.register_current(kind='verification', label='real observation', max_runtime_seconds=60)
    result = store.inspect()
    assert result['processes'][0]['pid'] == os.getpid()
    assert result['processes'][0]['process_state'] == 'running'
    assert not {'cmdline','environment','username','executable'} & result['processes'][0].keys()
    assert not hasattr(store, 'terminate') and not hasattr(store, 'kill')


def test_old_overdue_process_is_not_hidden_by_newer_healthy_registrations(registry):
    store, processes, clock = registry
    original = register(store)
    clock[0] = 1060
    child = os.getpid() + 100000
    processes[child] = ObservedProcess(1059.0, os.getpid())
    store.register_child(child, kind='worker', label='new healthy child', max_runtime_seconds=600)
    report = store.inspect(limit=1)
    assert report['has_more'] and report['scanned_count'] == 2 and report['warning_count'] == 1
    assert report['processes'][0]['id'] == original['id']
    assert report['processes'][0]['warnings'] == ['runtime_exceeded']


def test_spawn_observer_registers_bounded_duration_and_skips_finished_workers(registry, monkeypatch):
    store, _, _ = registry
    captured = []
    def registered(self, pid, **kwargs):
        captured.append((pid, kwargs))
        return {'id':'recorded'}
    monkeypatch.setattr(ProcessRegistry, 'register_child', registered)
    child = SimpleNamespace(pid=123, poll=lambda: None)
    result = observe_spawned(store.guard, child, kind='worker', label='local', timeout_seconds=1.2)
    assert result == {'status':'registered','id':'recorded'}
    assert captured[0][1]['max_runtime_seconds'] == 2
    child.poll = lambda: 0
    assert observe_spawned(store.guard, child, kind='worker', label='local', timeout_seconds=1)['status'] == 'already_exited'
    assert len(captured) == 1


def test_monitor_storage_failure_logs_fixed_warning_without_leaking_or_killing(registry, monkeypatch, caplog):
    store, _, _ = registry
    def unavailable(*args, **kwargs):
        raise ProcessRegistryError('secret private diagnostic')
    monkeypatch.setattr(ProcessRegistry, 'register_child', unavailable)
    child = SimpleNamespace(pid=123, poll=lambda: None)
    assert observe_spawned(store.guard, child, kind='worker', label='local', timeout_seconds=1)['status'] == 'unavailable'
    assert 'monitoring unavailable' in caplog.text and 'secret' not in caplog.text


def test_stop_during_spawn_registration_propagates_instead_of_fallback(registry, monkeypatch):
    store, _, _ = registry
    def stopped(*args, **kwargs):
        (store.guard.workspace / '.jarvis-stop').write_text('stop')
        raise ProcessRegistryError('interrupted')
    monkeypatch.setattr(ProcessRegistry, 'register_child', stopped)
    with pytest.raises(GuardrailError):
        observe_spawned(store.guard, SimpleNamespace(pid=123,poll=lambda:None),
                        kind='worker',label='local',timeout_seconds=1)


def test_local_runner_registers_spawn_before_processing_response(tmp_path, monkeypatch):
    from jarvis.agents.runner import LocalConfig, LocalRunner
    from jarvis.agents.store import TaskStore
    captured = []
    def observe(guard, process, **kwargs):
        captured.append(kwargs)
    def communicate(**kwargs):
        assert captured
        return json.dumps({'ok':True,'result':{'passed':True}}).encode(), None
    child = SimpleNamespace(communicate=communicate,poll=lambda:0,wait=lambda **kwargs:0,returncode=0)
    monkeypatch.setattr('jarvis.agents.runner.subprocess.Popen', lambda *args,**kwargs:child)
    monkeypatch.setattr('jarvis.agents.runner.observe_spawned', observe)
    runner = LocalRunner(TaskStore(Guardrails(tmp_path)), LocalConfig())
    assert runner._worker('qa', {'proposal':'def f(): return 1'},time.time()+10)['passed']
    assert captured[0]['kind'] == 'verification' and captured[0]['label'] == 'local-qa'
    assert 0 < captured[0]['timeout_seconds'] <= 10
