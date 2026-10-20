"""W5 integration-3: run the dispatcher as a JARVIS-owned service.

JARVIS, not an interactive assistant, owns the wake-up loop: one instance per workspace,
registered as an owned service, ticking every interval while enabled, stopping on
`.jarvis-stop`. Codex wakes are gated by workload_policy when a usage sample exists.
"""
import json
import logging
import os
import time
from uuid import uuid4

import psutil

from jarvis.agents.workload_policy import plan_workload
from jarvis.core.guardrails import GuardrailError

USAGE_DIR = '.jarvis-dispatch/usage'
LOCK = '.jarvis-dispatch/service.json'
POLL_SECONDS = 5
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


def codex_workload_gate(sample, now):
    """(allowed, reason). Without a sample, defer to the ledger's limit backoff instead of pausing forever."""
    if sample is None:
        return True, 'no_usage_sample'
    plan = plan_workload(sample.get('usage'), ['codex'], sample.get('sampled_at'), now)
    if plan['codex_role'] == 'normal':
        return True, plan['profile']
    return False, 'workload_paused:' + plan['profile']


def _alive(pid, created):
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - created) < 1.0
    except (psutil.Error, TypeError, ValueError):
        return False


class DispatchService:
    def __init__(self, guard, *, tick, config, sleep=time.sleep, clock=time.time, register=None, on_error=None):
        self.guard, self.tick, self.config = guard, tick, config
        self.sleep, self.clock = sleep, clock
        self.register = register
        self.on_error = on_error or (lambda exc: LOGGER.warning('Dispatcher tick failed (%s)', type(exc).__name__))
        self._lock_path = None
        self._token = uuid4().hex

    def acquire(self):
        path = self.guard.validate_path(LOCK, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            try:
                held = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                held = {}
            if held.get('token') != self._token and _alive(held.get('pid'), held.get('created')):
                raise ServiceBusy(f"Dispatcher service already running (pid {held.get('pid')}).")
        me = psutil.Process(os.getpid())
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'pid': me.pid, 'created': me.create_time(), 'token': self._token,
                                    'started': self.clock()}),
                        encoding='utf-8')
        os.replace(temp, path)
        self._lock_path = path

    def release(self):
        if self._lock_path is not None:
            try:
                held = json.loads(self._lock_path.read_text(encoding='utf-8'))
                if held.get('token') == self._token:
                    self._lock_path.unlink()
            except (OSError, ValueError):
                pass
            self._lock_path = None

    def _stopped(self):
        try:
            self.guard.check_running()
            return False
        except GuardrailError:
            return True

    def run(self):
        if self._stopped():
            return {'reason': 'stopped', 'ticks': 0}
        self.acquire()
        ticks = 0
        try:
            if self.register:
                self.register()
            next_tick = self.clock()
            while not self._stopped():
                settings = self.config()
                if self.clock() >= next_tick:
                    if settings.get('enabled'):
                        try:
                            self.tick()
                        except Exception as exc:  # a failed tick never ends the service
                            self.on_error(exc)
                        ticks += 1
                    next_tick = self.clock() + settings.get('interval_minutes', 10) * 60
                self.sleep(min(POLL_SECONDS, max(0.0, next_tick - self.clock())) or POLL_SECONDS)
            return {'reason': 'stopped', 'ticks': ticks}
        finally:
            self.release()
