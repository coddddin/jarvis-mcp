"""Fixed local Linux-container QA. Candidate source is never executed on the host."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from pydantic import Field

from jarvis.agents.models import Contract, IntegerCase, QAEvidence


class SandboxError(ValueError):
    """An unavailable/failed boundary must not fall back to host execution."""


class DockerRuntimeConfig(Contract):
    executable: str = Field(min_length=1, max_length=1024)
    image_id: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')


# This trusted driver is sent to an immutable local image, never host Python.
# Expected answers remain on the host; only source and call arguments go in.
DRIVER = r'''
import json, sys
request = json.loads(sys.stdin.buffer.read(65537))
namespace = {'__name__': '__candidate__'}
try:
    exec(compile(request['source'], '<candidate>', 'exec'), namespace)
    values = []
    for call in request['calls']:
        result = namespace[call['function']](*call['args'])
        if type(result) is not int or abs(result) > 1000000000:
            raise ValueError('Unsupported return type or range')
        values.append(result)
    print(json.dumps({'values': values}))
except BaseException:
    print(json.dumps({'candidate_failed': True}))
'''


class SandboxRunner:
    MAX_OUTPUT = 65536
    TIMEOUT_SECONDS = 15

    def __init__(self, guard, config: DockerRuntimeConfig, *, cancel_check=None):
        self.guard = guard
        self.cancel_check = cancel_check or (lambda: None)
        self.config = DockerRuntimeConfig.model_validate(config.model_dump())
        candidate = Path(self.config.executable)
        if (not candidate.is_absolute() or str(candidate).startswith(('\\\\', '//'))
                or candidate.name.casefold() not in {'docker.exe', 'docker'}):
            raise SandboxError('A trusted absolute local Docker executable is required.')
        try:
            self.executable = candidate.resolve(strict=True)
        except OSError:
            raise SandboxError('Docker executable is unavailable; host fallback is forbidden.') from None
        if not self.executable.is_file() or self.executable.is_relative_to(guard.workspace):
            raise SandboxError('Docker executable must be installed outside the data workspace.')
        self.endpoint = 'npipe:////./pipe/dockerDesktopLinuxEngine' if os.name == 'nt' else 'unix:///var/run/docker.sock'

    def _environment(self):
        env = {k: v for k, v in os.environ.items() if k.upper() not in {
            'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'}}
        env['PATH'] = str(self.executable.parent) + os.pathsep + env.get('PATH', '')
        return env

    def _command(self, arguments):
        return [str(self.executable), '--host', self.endpoint, *arguments]

    def _trusted(self, arguments, *, timeout=5):
        try:
            result = subprocess.run(self._command(arguments), stdin=subprocess.DEVNULL,
                capture_output=True, timeout=timeout, env=self._environment(),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError('Local Docker command unavailable or timed out.') from exc
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise SandboxError('Local Docker command failed; inspect runtime availability.')
        return result.stdout.decode('utf-8').strip()

    def _exchange(self, arguments, payload, deadline):
        process = subprocess.Popen(self._command(arguments), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=self._environment(),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        chunks, errors = [], []
        exceeded, read_done = threading.Event(), threading.Event()

        def read():
            total = 0
            try:
                while True:
                    block = process.stdout.read(4096)
                    if not block:
                        break
                    total += len(block)
                    if total > self.MAX_OUTPUT:
                        exceeded.set()
                        break
                    chunks.append(block)
            except OSError:
                errors.append('read')
            finally:
                process.stdout.close()
                read_done.set()

        def write():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except OSError:
                errors.append('write')

        reader = threading.Thread(target=read, daemon=True)
        writer = threading.Thread(target=write, daemon=True)
        reader.start()
        writer.start()
        stop = time.monotonic() + self.TIMEOUT_SECONDS
        try:
            while process.poll() is None or not read_done.is_set():
                self.guard.check_running()
                self.cancel_check()
                if exceeded.is_set():
                    raise SandboxError('Container output limit exceeded.')
                if time.monotonic() >= stop or time.time() >= deadline:
                    raise SandboxError('Container QA timed out.')
                time.sleep(0.02)
            reader.join(timeout=1)
            writer.join(timeout=1)
            if process.returncode != 0 or errors or exceeded.is_set() or writer.is_alive():
                raise SandboxError('Container execution/transport failed.')
            return b''.join(chunks)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            # A separate rm --force in check() also stops a detached container.

    def check(self, source, cases, deadline):
        self.guard.check_running()
        self.cancel_check()
        if not isinstance(source, str) or not source.strip() or len(source) > 12000 or not 1 <= len(cases) <= 32:
            raise SandboxError('Invalid bounded container QA input.')
        cases = [IntegerCase.model_validate_json(json.dumps(case)).model_dump(mode='json') for case in cases]
        if self._trusted(['info', '--format', '{{.OSType}}']) != 'linux':
            raise SandboxError('A local Linux container engine is required.')
        if self._trusted(['image', 'inspect', '--format', '{{.Id}}', self.config.image_id]) != self.config.image_id:
            raise SandboxError('Pinned local image differs; automatic pull is forbidden.')
        self.guard.check_running()
        self.cancel_check()
        name = 'jarvis-qa-' + uuid4().hex
        arguments = ['run', '--rm', '--pull=never', '--name', name, '--network=none', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--pids-limit=32',
            '--memory=128m', '--cpus=0.5', '--user=65534:65534', '--ipc=none', '--log-driver=none',
            '--tmpfs=/tmp:rw,noexec,nosuid,size=8m', '--workdir=/tmp', '--interactive',
            '--entrypoint=/usr/local/bin/python', self.config.image_id, '-I', '-S', '-c', DRIVER]
        payload = json.dumps({'source': source, 'calls': [
            {'function': case['function'], 'args': case['args']} for case in cases]}, ensure_ascii=True).encode()
        if len(payload) > 65536:
            raise SandboxError('Container input limit exceeded.')
        cleanup_error = None
        try:
            output = self._exchange(arguments, payload, deadline)
        finally:
            try:
                # Only this invocation's random container name; never global pruning.
                self._trusted(['rm', '--force', name])
            except SandboxError:
                try:
                    # --rm may have already removed it; verify absence explicitly.
                    names = self._trusted(['ps', '--all', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}'])
                    if names:
                        cleanup_error = name
                except Exception:
                    cleanup_error = name
            if cleanup_error:
                raise SandboxError('Container cleanup unverified; operator review required: ' + name)
        try:
            data = json.loads(output)
        except (ValueError, UnicodeError):
            raise SandboxError('Container emitted unexpected output; QA cannot be claimed.') from None
        values = data.get('values') if isinstance(data, dict) else None
        passed_count = 0
        if isinstance(values, list) and len(values) == len(cases):
            passed_count = sum(type(value) is int and value == case['expected'] for value, case in zip(values, cases))
        passed = passed_count == len(cases)
        return QAEvidence(checker='python_container_cases_v1', sha256=hashlib.sha256(source.encode()).hexdigest(),
            runtime_image_id=self.config.image_id,
            passed=passed, cases_passed=passed_count, cases_total=len(cases),
            cases_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            detail=f'{passed_count}/{len(cases)} local Linux-container cases passed; no host mounts/network; not a proof of general correctness.')
