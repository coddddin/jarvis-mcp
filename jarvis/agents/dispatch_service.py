"""W5 integration-3: run the dispatcher as a JARVIS-owned service.

JARVIS, not an interactive assistant, owns the wake-up loop: one instance per workspace
(OS-level file lock), registered as an owned service when possible, ticking every interval
while enabled, stopping on `.jarvis-stop`. Codex wakes are gated by workload_policy when a
fresh usage sample exists. Employee processes are bound to a kill-on-close job so they die
with the service.
"""
import json
import logging
import os
import time

from jarvis.agents.workload_policy import MAX_SAMPLE_AGE_SECONDS, plan_workload
from jarvis.core.guardrails import GuardrailError

USAGE_DIR = '.jarvis-dispatch/usage'
LOCK = '.jarvis-dispatch/service.lock'
PAUSE = '.jarvis-dispatch/paused.json'
POLL_SECONDS = 5
DEFAULT_INTERVAL_MINUTES = 10
LOGGER = logging.getLogger(__name__)


class ServiceBusy(RuntimeError):
    """Another dispatcher service already runs for this workspace."""


def read_usage_sample(guard, employee):
    """A usage sample written by a collector (W3): {'usage': {...windows...}, 'sampled_at': epoch}."""
    path = guard.validate_path(f'{USAGE_DIR}/{employee}.json', must_exist=False)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16384:
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('usage'), dict) else None


def read_pause(guard):
    """Pause switch written by the HUD backend. Missing file: running. Unreadable or malformed: paused
    (conservative). Contract agreed with claude-code:third on 2026-10-21."""
    try:
        # validate_path can fail spuriously while the HUD swaps the file in (Windows resolves a
        # pending-delete file to an extended-length path). The service loop must not die on that: stay paused.
        path = guard.validate_path(PAUSE, must_exist=False)
    except GuardrailError:
        return {'paused': True, 'reason': 'unreadable_pause_file'}
    if not path.exists():
        return {'paused': False}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError('unsafe pause file')
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'paused': True, 'reason': 'unreadable_pause_file'}
    if not isinstance(data, dict) or not isinstance(data.get('paused'), bool):
        return {'paused': True, 'reason': 'malformed_pause_file'}
    result = {'paused': data['paused']}
    for key, kind, limit in (('by', str, 80), ('reason', str, 200)):
        if isinstance(data.get(key), kind):
            result[key] = data[key][:limit]
    if isinstance(data.get('at'), (int, float)) and not isinstance(data.get('at'), bool):
        result['at'] = data['at']
    return result


def codex_workload_gate(sample, now):
    """(allowed, reason). Only an explicit 'paused' verdict on a fresh sample blocks Codex.

    No sample or a stale sample defers to the ledger's limit backoff, so a stopped collector
    can never pause Codex forever."""
    if sample is None:
        return True, 'no_usage_sample'
    sampled_at = sample.get('sampled_at')
    if (isinstance(sampled_at, bool) or not isinstance(sampled_at, (int, float))
            or not 0 <= now - sampled_at <= MAX_SAMPLE_AGE_SECONDS):
        return True, 'stale_usage_sample'
    plan = plan_workload(sample.get('usage'), ['codex'], sampled_at, now)
    if plan['profile'] == 'unknown':  # unparseable windows: a broken collector must not pause Codex
        return True, 'unparsed_usage_sample'
    if plan['codex_role'] == 'paused':
        return False, 'workload_paused:' + plan['profile']
    return True, plan['profile']


class KillOnCloseJob:
    """Windows job object: every assigned process (and its children) dies when this closes or the
    owning service process exits. No-op elsewhere."""

    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                       ctypes.c_void_p]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        self._kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'CreateJobObject failed')
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), 'SetInformationJobObject failed')
        self.handle = handle

    def assign(self, process):
        if self.handle is None:
            return False
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))

    def close(self):
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None

    def active_processes(self):
        """Live processes in the job, or None when unknown."""
        import ctypes

        class Accounting(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
            _fields_ = [(name, ctypes.c_longlong) for name in (
                'TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, ctypes.c_ulong) for name in (
                'TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]

        if self.handle is None:
            return None
        info = Accounting()
        if not self._kernel32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return info.ActiveProcesses

    def terminate(self, timeout=10.0):
        """Kill every process in the job, wait until none is left, then close. True only when the job was
        empty at the end; without a job (not Windows) there is nothing to wait for."""
        if self.handle is None:
            return os.name != 'nt'
        try:
            self._kernel32.TerminateJobObject(self.handle, 1)
            deadline = time.monotonic() + timeout
            while True:
                left = self.active_processes()
                if left == 0:
                    return True
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.05)
        finally:
            self.close()


class DispatchService:
    def __init__(self, guard, *, tick, config, sleep=time.sleep, clock=time.time, register=None, on_error=None):
        self.guard, self.tick, self.config = guard, tick, config
        self.sleep, self.clock = sleep, clock
        self.register = register
        self.on_error = on_error or (lambda exc: LOGGER.warning('Dispatcher error (%s)', type(exc).__name__))
        self._lock_file = None
        self._settings = None  # last validated settings

    def acquire(self):
        """OS-level exclusive lock held for the service lifetime; released by the OS on crash."""
        path = self.guard.validate_path(LOCK, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - non-Windows hosts
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ServiceBusy('Dispatcher service already running for this workspace.') from None
        self._lock_file = handle
        self._info = path.with_name('service.json')
        self._info.write_text(json.dumps({'pid': os.getpid(), 'started': self.clock()}), encoding='utf-8')

    def release(self):
        if self._lock_file is not None:
            # Remove the record while still holding the lock, so a successor's record is never deleted.
            try:
                self._info.unlink(missing_ok=True)
            except OSError:
                pass  # a leftover record is reported by the watch, not fatal
            try:
                if os.name == 'nt':
                    import msvcrt
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._lock_file.close()
            self._lock_file = None

    def _stopped(self):
        try:
            self.guard.check_running()
            return False
        except GuardrailError:
            return True

    def _current_settings(self):
        """Validated settings; on a transient read/validation error keep the last good ones.
        With no good settings yet the service stays idle (disabled)."""
        try:
            settings = self.config()
            interval = settings.get('interval_minutes')
            if (not isinstance(settings.get('enabled'), bool) or isinstance(interval, bool)
                    or not isinstance(interval, int) or not 5 <= interval <= 240):
                raise ValueError('Invalid dispatcher settings.')
            self._settings = {'enabled': settings['enabled'], 'interval_minutes': interval}
        except Exception as exc:
            self.on_error(exc)
        return self._settings or {'enabled': False, 'interval_minutes': DEFAULT_INTERVAL_MINUTES}

    def run(self):
        if self._stopped():
            return {'reason': 'stopped', 'ticks': 0}
        self.acquire()
        ticks = 0
        try:
            if self.register:
                try:
                    self.register()
                except Exception as exc:  # monitoring is best effort, like observe_spawned
                    self.on_error(exc)
            next_tick = self.clock()
            while not self._stopped():
                if self.clock() >= next_tick:
                    settings = self._current_settings()
                    try:
                        paused = read_pause(self.guard)['paused']
                    except Exception as exc:  # conservative: an unexpected error counts as paused
                        self.on_error(exc)
                        paused = True
                    if paused:
                        # Stay due: the first poll after resume ticks immediately.
                        self.sleep(POLL_SECONDS)
                        continue
                    if settings['enabled']:
                        try:
                            self.tick()
                        except Exception as exc:  # a failed tick never ends the service
                            self.on_error(exc)
                        ticks += 1
                    next_tick = self.clock() + settings['interval_minutes'] * 60
                self.sleep(min(POLL_SECONDS, max(0.0, next_tick - self.clock())) or POLL_SECONDS)
            return {'reason': 'stopped', 'ticks': ticks}
        finally:
            self.release()
