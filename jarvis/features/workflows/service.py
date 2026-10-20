"""Operator-registered workflows; fixed argv, explicit browser dispatch, owned service lifecycle."""
from jarvis.core.paths import CONFIG_ROOT, WORKERS_ROOT
import atexit
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowError(ValueError):
    """Reportable registry, startup, or owned process error."""


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class Action(StrictModel):
    kind: Literal['service', 'app', 'web']
    program: str | None = Field(default=None, max_length=80)
    args: list[str] = Field(default_factory=list, max_length=32)
    cwd: str = Field(default='.', max_length=800)
    url: str | None = Field(default=None, max_length=2048)
    port: int | None = Field(default=None, ge=1024, le=65535)
    startup_seconds: int = Field(default=3, ge=1, le=10)

    @model_validator(mode='after')
    def shape(self):
        if self.kind == 'web':
            if not self.url or self.program or self.args or self.port:
                raise ValueError('Web action requires only a registered URL.')
        elif not self.program or self.url or (self.kind == 'service' and self.port is None):
            raise ValueError('Process requires a program; service also requires a loopback port.')
        if self.kind == 'app' and self.port is not None:
            raise ValueError('App actions do not use service ports.')
        if any(len(value) > 2048 or any(ord(c) < 32 for c in value) for value in self.args):
            raise ValueError('Invalid registered arguments.')
        return self


class Profile(StrictModel):
    description: str = Field(max_length=300)
    enabled: bool = True
    max_runtime_seconds: int = Field(default=3600, ge=1, le=86400)
    actions: list[Action] = Field(min_length=1, max_length=8)


class Registry(StrictModel):
    version: Literal[1]
    programs: dict[str, str] = Field(max_length=20)
    allowed_hosts: list[str] = Field(max_length=50)
    profiles: dict[str, Profile] = Field(max_length=20)


class WorkflowLauncher:
    CONFIG = CONFIG_ROOT / 'workflows.json'
    MAX_HISTORY = 100
    BROWSER_DISPATCH_TIMEOUT = 2.0

    def __init__(self, guard):
        self.guard = guard
        self.lock = threading.RLock()
        self.previews = {}
        self.runs = {}
        self.monitor_started = False
        self.shutting_down = False

    def _registry(self):
        path = self.CONFIG.absolute()
        if path.resolve().is_relative_to(self.guard.workspace):
            raise WorkflowError('Workflow registry must be trusted operator configuration outside the data workspace.')
        if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise WorkflowError('Linked workflow registry paths are forbidden.')
        with path.open('rb') as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise WorkflowError('Hardlinked workflow registry is forbidden.')
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise WorkflowError('Workflow registry exceeds size limit.')
        return Registry.model_validate_json(raw), hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
            raise WorkflowError('Invalid workflow run or preview ID.')
        return value

    def profiles(self):
        registry, _ = self._registry()
        return {'workflows': [{'name': name, 'description': profile.description,
                              'enabled': profile.enabled, 'action_count': len(profile.actions)}
                             for name, profile in sorted(registry.profiles.items())]}

    def _program(self, registry, name):
        value = registry.programs.get(name)
        if value in {'@python', '@status-server'}:
            path = Path(sys.executable)
        elif value == '@vscode':
            candidates = [Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Microsoft VS Code/Code.exe',
                          Path(os.environ.get('ProgramFiles', '')) / 'Microsoft VS Code/Code.exe']
            path = next((p for p in candidates if p.is_absolute() and p.is_file()), None)
            if path is None:
                raise WorkflowError('Registered VS Code installation was not found.')
        elif value is not None and Path(value).is_absolute():
            path = Path(value)
        else:
            raise WorkflowError('Program is not registered with an absolute executable or supported alias.')
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() in {'.cmd', '.bat', '.ps1', '.sh', '.py'}:
            raise WorkflowError('Only registered executable files are allowed.')
        if path.stem.casefold() in {'cmd', 'powershell', 'pwsh', 'bash', 'sh', 'zsh', 'wscript', 'cscript', 'env'}:
            raise WorkflowError('Shell and script-host executables are forbidden.')
        return path

    def _resolve(self, name):
        registry, digest = self._registry()
        profile = registry.profiles.get(name)
        if profile is None or not profile.enabled:
            raise WorkflowError('Workflow is not registered or is disabled.')
        actions = []
        ports = set()
        for action in profile.actions:
            if action.kind == 'web':
                parsed = urlsplit(action.url)
                if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in registry.allowed_hosts
                        or parsed.username or parsed.password or any(ord(c) < 33 for c in action.url)
                        or (parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})):
                    raise WorkflowError('URL origin is not permitted by the registry.')
                actions.append({'kind': 'web', 'url': action.url})
            else:
                program = self._program(registry, action.program)
                cwd = self.guard.validate_path(action.cwd)
                if not cwd.is_dir():
                    raise WorkflowError('Registered working directory is not a directory.')
                if action.port in ports:
                    raise WorkflowError('Duplicate service port in workflow.')
                if action.kind == 'service':
                    ports.add(action.port)
                info = program.stat()
                args = list(action.args)
                asset_hash = None
                if registry.programs[action.program] == '@status-server':
                    script = WORKERS_ROOT / 'workflow_status.py'
                    if script.is_symlink() or script.is_junction() or script.stat().st_nlink != 1 or script.stat().st_size > 65536:
                        raise WorkflowError('Invalid built-in status server script.')
                    asset_hash = hashlib.sha256(script.read_bytes()).hexdigest()
                    args.insert(0, str(script))
                actions.append({'kind': action.kind, 'program': str(program), 'args': args,
                                'cwd': str(cwd), 'port': action.port, 'startup_seconds': action.startup_seconds,
                                'program_identity': [info.st_size, info.st_mtime_ns], 'asset_hash': asset_hash})
        return profile, digest, actions

    def preview(self, name):
        with self.lock:
            profile, digest, actions = self._resolve(name)
            now = time.monotonic()
            self.previews = {key: value for key, value in self.previews.items() if value['expires'] > now}
            if len(self.previews) >= 50:
                raise WorkflowError('Too many pending workflow previews.')
            preview_id = uuid4().hex
            self.previews[preview_id] = {'name': name, 'digest': digest, 'actions': actions, 'expires': now + 600}
            return {'preview_id': preview_id, 'workflow': name, 'actions': actions, 'expires_in_seconds': 600,
                    'service_lease_seconds': profile.max_runtime_seconds,
                    'policy': 'Only owned services are stoppable; apps/tabs may be handed to existing applications.'}

    def _store(self):
        path = self.guard.validate_path('.jarvis-workflows', must_exist=False)
        path.mkdir(exist_ok=True)
        return path

    def _persist(self, run):
        record = {key: value for key, value in run.items() if key not in {'handles', 'deadline'}}
        folder = self._store()
        temp = self.guard.validate_path(str(folder / (uuid4().hex + '.tmp')), must_exist=False)
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        target = self.guard.validate_path(str(folder / (run['id'] + '.json')), must_exist=False)
        os.replace(temp, target)

    @staticmethod
    def _port_open(port):
        with socket.socket() as sock:
            sock.settimeout(0.1)
            return sock.connect_ex(('127.0.0.1', port)) == 0

    def _open_url(self, url):
        result = []
        def dispatch():
            try:
                result.append(bool(webbrowser.open_new_tab(url)))
            except Exception:
                result.append(False)
        thread = threading.Thread(target=dispatch, daemon=True, name='jarvis-browser-dispatch')
        thread.start()
        thread.join(self.BROWSER_DISPATCH_TIMEOUT)
        if thread.is_alive():
            raise WorkflowError('Browser dispatch timed out; a tab may still open later. Do not assume it was cancelled.')
        return result[0]

    def _stop_services(self, run):
        errors = []
        for item in run['handles']:
            child, created, kind = item
            if kind != 'service' or child.poll() is not None:
                continue
            try:
                parent = psutil.Process(child.pid)
                if parent.create_time() != created:
                    raise WorkflowError('PID identity changed; process will not be terminated.')
                processes = parent.children(recursive=True)
                if len(processes) > 100:
                    raise WorkflowError('Owned process tree is too large for automatic stop.')
                identities = [(process, process.create_time()) for process in [*reversed(processes), parent]]
                for process, stamp in identities:
                    if process.is_running() and process.create_time() == stamp:
                        process.terminate()
                _, alive = psutil.wait_procs([process for process, _ in identities], timeout=2)
                stamps = {process.pid: stamp for process, stamp in identities}
                for process in alive:
                    if process.create_time() == stamps[process.pid]:
                        process.kill()
                _, remaining = psutil.wait_procs(alive, timeout=1)
                if remaining:
                    errors.append('Some owned service processes remain running.')
                child.poll()
            except psutil.NoSuchProcess:
                child.poll()
            except (OSError, psutil.Error, WorkflowError):
                errors.append('Owned service stop failed; operator review required.')
        return errors

    def _monitor(self):
        while not self.shutting_down:
            time.sleep(0.25)
            with self.lock:
                for run in self.runs.values():
                    if run['status'] != 'running':
                        continue
                    interrupted = (self.guard.workspace / '.jarvis-stop').exists()
                    expired = time.monotonic() >= run['deadline']
                    if interrupted or expired:
                        errors = self._stop_services(run)
                        run.update(status='stop_failed' if errors else ('interrupted' if interrupted else 'expired'), errors=errors)
                        try:
                            self._persist(run)
                        except Exception as exc:
                            run['errors'].append('Run history could not be saved.')
                            logging.getLogger('jarvis').error('Workflow monitor persistence failed (%s)', type(exc).__name__)

    def shutdown(self):
        self.shutting_down = True
        with self.lock:
            for run in self.runs.values():
                if run['status'] in {'running', 'stop_failed'}:
                    errors = self._stop_services(run)
                    run.update(status='stop_failed' if errors else 'shutdown', errors=errors)
                    try:
                        self._persist(run)
                    except Exception as exc:
                        logging.getLogger('jarvis').error('Workflow shutdown persistence failed (%s)', type(exc).__name__)

    def start(self, preview_id):
        with self.lock:
            plan = self.previews.pop(self._id(preview_id), None)
            if plan is None or plan['expires'] <= time.monotonic():
                raise WorkflowError('Workflow preview expired or already used.')
            profile, digest, actions = self._resolve(plan['name'])
            if digest != plan['digest'] or actions != plan['actions']:
                raise WorkflowError('Workflow configuration or executable changed; preview again.')
            if any(run['workflow'] == plan['name'] and run['status'] in {'running', 'starting', 'stop_failed'}
                   for run in self.runs.values()):
                raise WorkflowError('Workflow is already active; check status or stop it first.')
            if len(list(self._store().glob('*.json'))) >= self.MAX_HISTORY:
                raise WorkflowError('Workflow history capacity reached; operator archival required.')
            if sum(run['status'] == 'running' for run in self.runs.values()) >= 5:
                raise WorkflowError('At most five workflows may run concurrently.')
            for action in actions:
                if action['kind'] == 'service' and self._port_open(action['port']):
                    raise WorkflowError('Service port is already occupied; existing service will not be adopted.')
            run = {'id': uuid4().hex, 'workflow': plan['name'], 'status': 'starting', 'steps': [], 'errors': [],
                   'created_at': datetime.now(timezone.utc).isoformat(), 'handles': [],
                   'deadline': time.monotonic() + profile.max_runtime_seconds}
            self.runs[run['id']] = run
            self._persist(run)
            if not self.monitor_started:
                atexit.register(self.shutdown)
                self.monitor_started = True
                threading.Thread(target=self._monitor, daemon=True, name='jarvis-workflow-monitor').start()
            try:
                for action in actions:
                    self.guard.check_running()
                    if time.monotonic() >= run['deadline']:
                        raise WorkflowError('Workflow runtime lease expired during startup.')
                    if action['kind'] == 'web':
                        accepted = self._open_url(action['url'])
                        run['steps'].append({'kind': 'web', 'url': action['url'],
                                             'status': 'dispatch_accepted_unverified' if accepted else 'dispatch_failed'})
                        if not accepted:
                            raise WorkflowError('Browser did not accept the registered URL.')
                    else:
                        child = subprocess.Popen([action['program'], *action['args']], cwd=action['cwd'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            shell=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' and action['kind'] == 'service' else 0,
                            env={key: value for key, value in os.environ.items() if key.upper() in {
                                'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOME'}})
                        try:
                            created = psutil.Process(child.pid).create_time()
                        except psutil.NoSuchProcess as exc:
                            child.poll()
                            if action['kind'] == 'app' and child.returncode == 0:
                                run['steps'].append({'kind': 'app', 'pid': child.pid,
                                                     'status': 'exited_handoff_unverified'})
                                self._persist(run)
                                continue
                            raise WorkflowError('Registered process exited before tracking was established.') from exc
                        except Exception:
                            if action['kind'] == 'service':
                                child.kill()
                                child.wait(timeout=1)
                            raise
                        run['handles'].append((child, created, action['kind']))
                        step = {'kind': action['kind'], 'pid': child.pid, 'status': 'spawned'}
                        run['steps'].append(step)
                        self._persist(run)
                        if action['kind'] == 'service':
                            deadline = time.monotonic() + action['startup_seconds']
                            while time.monotonic() < deadline:
                                self.guard.check_running()
                                if time.monotonic() >= run['deadline']:
                                    raise WorkflowError('Workflow runtime lease expired during startup.')
                                if child.poll() is not None:
                                    raise WorkflowError('Service exited before readiness; check registered configuration.')
                                if self._port_open(action['port']):
                                    step['status'] = 'tcp_ready'
                                    break
                                time.sleep(0.05)
                            else:
                                raise WorkflowError('Service readiness timeout.')
                        else:
                            step['status'] = 'spawned_ui_unverified'
                    self._persist(run)
                run['status'] = 'running'
            except Exception as exc:
                errors = self._stop_services(run)
                run.update(status='stop_failed' if errors else 'failed', errors=[
                    str(exc) if isinstance(exc, WorkflowError) else f'Workflow action failed ({type(exc).__name__}).', *errors])
            self._persist(run)
            return self.status(run['id'])

    def status(self, run_id):
        with self.lock:
            self._id(run_id)
            run = self.runs.get(run_id)
            if run is None:
                path = self.guard.validate_path(str(self._store() / (run_id + '.json')))
                if path.stat().st_nlink != 1 or path.stat().st_size > 65536:
                    raise WorkflowError('Invalid workflow history file.')
                record = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(record, dict) or record.get('id') != run_id:
                    raise WorkflowError('Invalid workflow history record.')
                return {'record': record, 'owned_by_this_server': False,
                        'untrusted_historical_record': True,
                        'note': 'Historical record only. No process ownership or liveness inferred; cannot stop after restart.'}
            services = [{'pid': child.pid, 'kind': kind, 'exit_code': child.poll()}
                        for child, _, kind in run['handles']]
            return {key: value for key, value in run.items() if key not in {'handles', 'deadline'}} | {
                'processes': services, 'owned_by_this_server': True,
                'service_health': 'service_exited' if any(item['kind'] == 'service' and item['exit_code'] is not None
                                                         for item in services) else 'no_known_service_exit',
                'note': 'TCP readiness is not application health. App windows and browser tabs are not managed for stop.'}

    def history(self):
        with self.lock:
            paths = sorted(self._store().glob('*.json'))
            if len(paths) > self.MAX_HISTORY:
                raise WorkflowError('Workflow history capacity exceeded.')
            return {'runs': [self.status(path.stem) for path in paths]}

    def stop(self, run_id):
        with self.lock:
            run = self.runs.get(self._id(run_id))
            if run is None:
                raise WorkflowError('This server does not own that run; historical PIDs are never termination authority.')
            errors = self._stop_services(run)
            run.update(status='stop_failed' if errors else 'stopped', errors=errors)
            self._persist(run)
            return self.status(run_id)
