"""Read-only, fixed-operation adapters. No arbitrary CLI forwarding or browsing."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from pydantic import BaseModel, ConfigDict, Field

from jarvis.core.paths import CONFIG_ROOT, PROJECT_ROOT, WORKERS_ROOT
from jarvis.core.redaction import redact
from jarvis.features.web.service import plain


class IntegrationError(ValueError):
    """Rejected integration request or bounded execution failure."""


class Settings(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    version: int = Field(ge=1, le=1)
    node: str
    rea_entry: str
    agent_python: str
    ghidra: str
    java_home: str


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    path: str = Field(min_length=1, max_length=800)


def scrub_result(value):
    """Redact strings before JSON encoding so escaping/structure stay intact."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub_result(item) for item in value]
    if isinstance(value, dict):
        return {redact(str(key)): scrub_result(item) for key, item in value.items()}
    return value


class IntegrationHub:
    CONFIG = CONFIG_ROOT / 'integrations.json'

    def __init__(self, guard, web):
        self.guard, self.web = guard, web

    def _trusted(self, value, *, directory=False):
        path = Path(value)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if '..' in path.parts or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise IntegrationError('Linked or traversing integration paths are forbidden.')
        path = path.resolve(strict=True)
        if path.is_relative_to(self.guard.workspace):
            raise IntegrationError('Executable/configuration paths must be outside the data workspace.')
        if directory:
            if not path.is_dir():
                raise IntegrationError('Integration directory unavailable.')
        elif not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrationError('Integration files must be regular files without hard links.')
        return path

    def _settings(self, names=None):
        config = self._trusted(self.CONFIG)
        with config.open('rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise IntegrationError('Integration settings too large.')
        settings = Settings.model_validate_json(raw)
        return {key: self._trusted(value, directory=key in {'ghidra', 'java_home'})
                for key, value in settings.model_dump().items() if key != 'version' and (names is None or key in names)}

    def _run(self, command, *, payload=b'', timeout=30, environment=None):
        self.guard.check_running()
        if len(payload) > 524288:
            raise IntegrationError('Worker input exceeds limit.')
        env = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
        if environment:
            env.update(environment)
        process = subprocess.Popen([str(value) for value in command], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=PROJECT_ROOT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        chunks, count, lock, overflow, input_failed = [], [0], threading.Lock(), threading.Event(), threading.Event()
        def read():
            while block := process.stdout.read(8192):
                with lock:
                    count[0] += len(block)
                    if count[0] > 1_048_576:
                        overflow.set()
                        return
                    chunks.append(block)
        def write():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                input_failed.set()
        reader = threading.Thread(target=read, daemon=True)
        writer = threading.Thread(target=write, daemon=True)
        reader.start(); writer.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                self.guard.check_running()
                if overflow.is_set() or time.monotonic() >= deadline:
                    raise IntegrationError('Integration exceeded its time/output limit.')
                time.sleep(.02)
            reader.join(1);writer.join(1)
            if process.returncode or overflow.is_set() or input_failed.is_set() or reader.is_alive() or writer.is_alive():
                raise IntegrationError('Integration failed; no successful analysis is reported.')
            with lock:
                output = b''.join(chunks)
            return json.loads(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            # The fixed child may itself own bounded analysis workers. REA's
            # provider ownership handles native jobs; this wrapper is not an OS sandbox.

    def analyze(self, path):
        request = AnalysisRequest(path=path)
        source = self.guard.validate_path(request.path)
        relative = source.relative_to(self.guard.workspace)
        if len(relative.parts) < 3 or relative.parts[0] != 'projects' or any(p.startswith('.') for p in relative.parts):
            raise IntegrationError('Select a source in projects/<name>/.')
        if source.suffix.lower() not in {'.js', '.mjs', '.cjs', '.exe', '.dll'} or not source.is_file():
            raise IntegrationError('Only JavaScript files or Windows PE executables/libraries are supported.')
        with source.open('rb') as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise IntegrationError('Hard-linked source files are forbidden.')
            raw = stream.read(8_388_609)
        if not raw or len(raw) > 8_388_608:
            raise IntegrationError('Select a nonempty program up to 8 MiB.')
        if source.suffix.lower() in {'.exe', '.dll'} and raw[:2] != b'MZ':
            raise IntegrationError('Native analysis requires a Windows PE file.')
        if source.suffix.lower() in {'.js', '.mjs', '.cjs'} and len(raw) > 131072:
            raise IntegrationError('Single-source JavaScript analysis is limited to 128 KiB.')
        native = source.suffix.lower() in {'.exe', '.dll'}
        settings = self._settings(('node', 'rea_entry', 'ghidra', 'java_home') if native else ('node', 'rea_entry'))
        internal = self.guard.validate_path('.jarvis-analysis', must_exist=False)
        internal.mkdir(exist_ok=True)
        self.guard.validate_path(str(internal))
        staging = Path(tempfile.mkdtemp(prefix='static-', dir=internal))
        try:
            target = staging / ('input' + source.suffix.lower())
            target.write_bytes(raw)
            base = [settings['node'], settings['rea_entry']]
            if source.suffix.lower() in {'.js', '.mjs', '.cjs'}:
                (staging / 'package.json').write_text(json.dumps({'name': 'jarvis-static-input', 'version': '1.0.0', 'main': target.name}), encoding='utf-8')
                command = base + ['analyze-javascript-application', staging, '--source-path', target.name,
                    '--max-heap-mb', '128', '--analysis-timeout-ms', '10000', '--format', 'json']
                deadline = 25
            else:
                command = base + ['analyze', target, '--provider', 'ghidra', '--format', 'json']
                deadline = 90
            environment = {'PATH': str(settings['node'].parent)}
            if native:
                environment.update({'GHIDRA_INSTALL_DIR': str(settings['ghidra']), 'JAVA_HOME': str(settings['java_home']),
                    'GHIDRA_HEADLESS_MAXMEM': '1G', 'PATH': environment['PATH'] + os.pathsep + str(settings['java_home'] / 'bin')})
            result = self._run(command, timeout=deadline, environment=environment)
            if isinstance(result, dict) and (result.get('ok') is False or result.get('error')):
                raise IntegrationError('REA returned an analysis error; inspect the operator diagnostics.')
            return {'source': relative.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'read_only': True, 'scope': 'Copied single input; statically analyzed, never executed.',
                    'analysis': scrub_result(result)}
        finally:
            resolved = staging.resolve()
            if resolved.is_relative_to(internal) and resolved == staging and not staging.is_symlink() and not staging.is_junction():
                shutil.rmtree(staging)
            else:
                raise IntegrationError('Temporary analysis cleanup requires operator review.')

    def feed(self, source_id):
        source, text = self.web.feed_input(source_id)
        settings = self._settings(('agent_python',))
        worker = self._trusted(WORKERS_ROOT / 'agent_reach_feed.py')
        result = self._run([settings['agent_python'], '-I', worker], payload=text.encode('utf-8'), timeout=8,
            environment={'USERPROFILE': str(settings['agent_python'].parents[2] / 'state')})
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            raise IntegrationError('Invalid Agent Reach feed result.')
        items = []
        for item in result['items'][:10]:
            link = self.web._link(item.get('url', ''), source)
            if link:
                items.append({'title': plain(item.get('title', ''), 300), 'summary': plain(item.get('summary', ''), 500),
                              'url': link, 'published_at': redact(item.get('published_at', ''))[:100]})
        if not items:
            raise IntegrationError('No articles with registered public links were returned.')
        return {'source_id': source_id, 'backend': 'Agent Reach RSS / feedparser', 'items': items[:5],
                'scope': 'Registered public feed only; no accounts, arbitrary URLs or browser cookies.'}
