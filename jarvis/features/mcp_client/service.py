"""Attached MCP servers: the user-approved config, pinned tools and one isolated client session per task.

The config file is JarvisWorkspace/.jarvis-settings/mcp_servers.json. Only the HUD session writes it (the same rule
as the brain selection): McpSettings.save is called from authenticated HUD routes, and no model-facing tool can add a
server, approve a tool or change a pin. A model sees only pinned tools of approved servers, under the user-approved
description, and only while the server still reports the same name, description and input schema.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jarvis.core.guardrails import GuardrailError
from jarvis.core.paths import WORKERS_ROOT
from jarvis.core.redaction import redact

# Session limits (defaults the user may change).
START_SECONDS = 60              # starting every server of a session, npm/Python start-up included
SESSION_SECONDS = 600           # one task's session at most
CALL_GRACE_SECONDS = 5          # on top of a tool's own timeout before the session is killed
DETAIL_CHARS = 6000             # tool text kept in a trace

FAMILIES = ('public_web', 'local')        # local: never sent to an external model (jarvis.agents.brain)
EFFECTS = ('read', 'auto', 'confirm', 'deny')
# read: no effect outside the session; auto: browser actions on public pages; confirm: needs the user each time
# (login, forms, posting, payments, sending, uploads, installs); deny: never offered to a model.
ALWAYS_DENY = frozenset({'browser_run_code_unsafe', 'browser_evaluate', 'browser_file_upload', 'browser_install'})
FORBIDDEN_ARGS = frozenset({'--allow-unrestricted-file-access', '--ignore-robots-txt', '--allow-private-ips'})
# Playwright MCP: a server whose command or arguments mention playwright (the npm bin is playwright-mcp) or that
# reports itself as Playwright. It must never expose tools a web page registers (WebMCP), keep its profile in memory
# and stay headless; only the flags below are allowed, so it cannot attach to the user's own browser (--extension,
# --cdp-endpoint), load a profile, cookies or scripts, read a --config file or listen on the network.
PLAYWRIGHT_REQUIRED = frozenset({'--no-webmcp', '--isolated', '--headless'})
PLAYWRIGHT_ALLOWED = PLAYWRIGHT_REQUIRED | {
    '--browser', '--output-dir', '--output-max-size', '--image-responses', '--block-service-workers',
    '--timeout-action', '--timeout-navigation', '--timeout-settle', '--user-agent', '--viewport-size',
    '--snapshot-mode', '--proxy-server', '--proxy-bypass', '--allowed-origins', '--blocked-origins', '--idle-timeout'}
# Option values arrive through the environment too (PLAYWRIGHT_MCP_EXTENSION, ...), so those names are refused.
FORBIDDEN_ENV_PREFIXES = ('PLAYWRIGHT_MCP_',)


def is_playwright(command, args):
    return any('playwright' in str(part).lower() for part in (command, *args))
BASE_ENV = ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'PATHEXT', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE',
            'HOMEDRIVE', 'HOMEPATH', 'SYSTEMDRIVE', 'USERNAME', 'PROCESSOR_ARCHITECTURE')


class McpError(ValueError):
    pass


class ConfirmRequired(McpError):
    """The tool has effect 'confirm': the caller must get the user's yes for this call first."""


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ToolPin(Strict):
    name: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    family: Literal['public_web', 'local'] = 'local'
    effect: Literal['read', 'auto', 'confirm', 'deny'] = 'confirm'
    timeout_s: int = Field(default=30, ge=1, le=120)
    max_chars: int = Field(default=8000, ge=100, le=50000)
    schema_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    description: str = Field(default='', max_length=500)       # the user-approved text a model sees

    @model_validator(mode='after')
    def deny_unsafe(self):
        if self.name in ALWAYS_DENY and self.effect != 'deny':
            raise ValueError(f'{self.name} is always denied.')
        return self


class ServerConfig(Strict):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,31}$')
    label: str = Field(min_length=1, max_length=60)
    command: str = Field(min_length=3, max_length=500)
    args: tuple[str, ...] = Field(default=(), max_length=32)
    env_names: tuple[str, ...] = Field(default=(), max_length=16)
    state: Literal['pending', 'approved'] = 'pending'
    tools: tuple[ToolPin, ...] = Field(default=(), max_length=64)

    @model_validator(mode='after')
    def check(self):
        if not Path(self.command).is_absolute():
            raise ValueError('The server command must be an absolute path.')
        if any(len(arg) > 500 or '\n' in arg or '\x00' in arg for arg in self.args):
            raise ValueError('Invalid server argument.')
        flags = {arg.split('=', 1)[0] for arg in self.args}
        if flags & FORBIDDEN_ARGS:
            raise ValueError('A forbidden server flag is set: ' + ', '.join(sorted(flags & FORBIDDEN_ARGS)))
        if is_playwright(self.command, self.args):
            options = {flag for flag in flags if flag.startswith('-')}
            if not PLAYWRIGHT_REQUIRED <= options:
                raise ValueError('This server needs ' + ', '.join(sorted(PLAYWRIGHT_REQUIRED)) + '.')
            if options - PLAYWRIGHT_ALLOWED:
                raise ValueError('Playwright flag not allowed: ' + ', '.join(sorted(options - PLAYWRIGHT_ALLOWED)))
        if any(not re.fullmatch(r'[A-Z0-9_]{1,64}', name) for name in self.env_names):
            raise ValueError('Environment names only, never values.')
        if any(name.startswith(FORBIDDEN_ENV_PREFIXES) for name in self.env_names):
            raise ValueError('Server options may not be passed through the environment.')
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError('Duplicate tool pin.')
        return self


class McpConfig(Strict):
    version: Literal[1] = 1
    servers: tuple[ServerConfig, ...] = Field(default=(), max_length=16)

    @model_validator(mode='after')
    def unique(self):
        if len({server.id for server in self.servers}) != len(self.servers):
            raise ValueError('Duplicate server id.')
        return self


def schema_hash(tool):
    """Pin of what a model would see and send: the tool's name, server description and input schema."""
    value = json.dumps({'name': tool['name'], 'description': tool.get('description') or '',
                        'input_schema': tool.get('input_schema') or {}}, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def qualified(server_id, tool_name):
    return f'mcp:{server_id}/{tool_name}'


class McpSettings:
    """mcp_servers.json. A missing, linked or invalid file reads as no servers. Only HUD routes call the writers."""
    DIRECTORY = '.jarvis-settings'
    PATH = '.jarvis-settings/mcp_servers.json'
    MAX_BYTES = 65536

    def __init__(self, guard):
        self.guard = guard

    def load(self):
        try:
            path = self.guard.validate_path(self.PATH, must_exist=False)
            if (not path.exists() or path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
                    or path.stat().st_size > self.MAX_BYTES):
                return McpConfig()
            return McpConfig.model_validate_json(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, GuardrailError):
            return McpConfig()

    def save(self, config):
        config = McpConfig.model_validate(config.model_dump())
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY, must_exist=False)
        directory.mkdir(exist_ok=True)
        path = self.guard.validate_path(self.PATH, must_exist=False)
        if path.exists() and (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1):
            raise GuardrailError('Linked settings files are forbidden.')
        temporary = path.with_name(path.name + '.tmp')
        try:
            temporary.write_text(config.model_dump_json(indent=2), encoding='utf-8')
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def add_server(self, server_id, label, command, args=(), env_names=()):
        """A new server waits as 'pending' with no pinned tools until the user approves it."""
        config = self.load()
        if any(server.id == server_id for server in config.servers):
            raise McpError('A server with this id already exists.')
        server = ServerConfig(id=server_id, label=label, command=command, args=tuple(args), env_names=tuple(env_names))
        self.save(McpConfig(servers=(*config.servers, server)))
        return server

    def approve(self, server_id, discovered, choices):
        """Pin the chosen tools of a discovered server and mark it approved. discovered is the tool list the server
        reported in discover(); choices maps tool name -> {family, effect, description?, timeout_s?, max_chars?}.
        Tools not chosen stay hidden. Re-approving after a change replaces every pin of the server."""
        config = self.load()
        server = next((item for item in config.servers if item.id == server_id), None)
        if server is None:
            raise McpError('Unknown server.')
        by_name = {tool['name']: tool for tool in discovered}
        pins = []
        for name, choice in choices.items():
            if name not in by_name:
                raise McpError(f'The server did not report the tool {name}.')
            choice = dict(choice)
            if name in ALWAYS_DENY:
                choice['effect'] = 'deny'
            choice.setdefault('description', (by_name[name].get('description') or '')[:500])
            pins.append(ToolPin(name=name, schema_sha256=schema_hash(by_name[name]), **choice))
        approved = server.model_copy(update={'state': 'approved', 'tools': tuple(pins)})
        ServerConfig.model_validate(approved.model_dump())
        self.save(McpConfig(servers=tuple(approved if item.id == server_id else item for item in config.servers)))
        return approved

    def remove(self, server_id):
        config = self.load()
        self.save(McpConfig(servers=tuple(item for item in config.servers if item.id != server_id)))


def families(config):
    """'mcp:<server>/<tool>' -> family for every usable pin of approved servers (for jarvis.agents.brain)."""
    return {qualified(server.id, tool.name): tool.family for server in config.servers if server.state == 'approved'
            for tool in server.tools if tool.effect != 'deny'}


def kill_tree(process):
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for item in children:
        try:
            item.kill()
        except psutil.Error:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


class McpSession:
    """One task's isolated client: a worker process holding the sessions of the approved servers.

    start() reports per server: 'ready', 'failed' or 'changed' (a pinned tool is missing or no longer matches its
    pin; it is not offered until the user re-approves in the HUD). Only matching pins become tools. The worker and
    every server it started are killed on close(), on a timeout, on the session limit or when JARVIS is stopped."""
    WORKER = WORKERS_ROOT / 'mcp_session.py'
    SANDBOX = '.jarvis-mcp'

    def __init__(self, guard, config, *, only=None, clock=time.monotonic, session_seconds=SESSION_SECONDS):
        self.guard, self.clock = guard, clock
        self.servers = [server for server in config.servers
                        if (server.state == 'approved' if only is None else server.id == only)]
        self.process, self.lines = None, queue.Queue()
        self.tools, self.status, self.discovered = {}, {}, {}
        self.started_at, self.session_seconds = None, session_seconds

    # --- process ---------------------------------------------------------------------------------------------
    def _spawn(self, env):
        script = self.WORKER
        if script.is_symlink() or script.stat().st_nlink != 1:
            raise McpError('Invalid MCP worker.')
        return subprocess.Popen([sys.executable, '-I', str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, cwd=str(self.guard.workspace),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def _pump(self):
        try:
            for line in iter(self.process.stdout.readline, b''):
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        self.lines.put(None)

    def _ask(self, command, seconds):
        if self.process is None or self.process.poll() is not None:
            raise McpError('The MCP session is not running.')
        try:
            self.process.stdin.write((json.dumps(command, ensure_ascii=False) + '\n').encode('utf-8'))
            self.process.stdin.flush()
        except OSError:
            self.close()
            raise McpError('The MCP session stopped.') from None
        deadline = self.clock() + seconds
        while True:
            try:
                self.guard.check_running()
            except GuardrailError:
                self.close()
                raise
            remaining = deadline - self.clock()
            if remaining <= 0:
                self.close()
                raise McpError('The MCP server did not answer in time; the session was stopped.')
            try:
                line = self.lines.get(timeout=min(.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                self.close()
                raise McpError('The MCP session stopped.')
            try:
                return json.loads(line)
            except ValueError:
                self.close()
                raise McpError('Invalid reply from the MCP worker.') from None

    # --- lifecycle --------------------------------------------------------------------------------------------
    def start(self):
        self.guard.check_running()
        specs, names = [], set()
        for server in self.servers:
            command = Path(server.command)
            if not command.is_file() or command.is_symlink():
                self.status[server.id] = {'state': 'failed', 'reason': 'The server program was not found.'}
                continue
            sandbox = self.guard.validate_path(f'{self.SANDBOX}/{server.id}', must_exist=False)
            sandbox.mkdir(parents=True, exist_ok=True)
            names.update(server.env_names)
            specs.append({'id': server.id, 'command': str(command), 'args': list(server.args),
                          'env': list(server.env_names), 'cwd': str(sandbox), 'timeout': START_SECONDS})
        if not specs:
            return self.status
        env = {key: value for key, value in os.environ.items() if key.upper() in BASE_ENV or key in names}
        self.process = self._spawn(env)
        threading.Thread(target=self._pump, daemon=True).start()
        self.started_at = self.clock()
        answer = self._ask({'op': 'start', 'servers': specs}, START_SECONDS + CALL_GRACE_SECONDS)
        for server in self.servers:
            report = (answer.get('servers') or {}).get(server.id)
            if server.id in self.status or report is None:
                continue
            if not report.get('ok'):
                self.status[server.id] = {'state': 'failed', 'reason': 'The server did not start.'}
                continue
            if ('playwright' in str(report.get('server_name') or '').lower()
                    and not is_playwright(server.command, server.args)):
                # Reports itself as Playwright but was configured without the Playwright flag rules.
                self.status[server.id] = {'state': 'failed', 'reason': 'This is a Playwright server; add it with its '
                                                                         'Playwright package path so its flag rules apply.'}
                continue
            reported = {tool['name']: tool for tool in report.get('tools') or [] if isinstance(tool, dict)
                        and isinstance(tool.get('name'), str)}
            self.discovered[server.id] = [{**tool, 'schema_sha256': schema_hash(tool)} for tool in reported.values()]
            changed = []
            for pin in server.tools:
                tool = reported.get(pin.name)
                if tool is None or schema_hash(tool) != pin.schema_sha256:
                    changed.append(pin.name)
                elif pin.effect != 'deny':
                    self.tools[qualified(server.id, pin.name)] = (server, pin, tool.get('input_schema') or {})
            self.status[server.id] = ({'state': 'changed', 'tools': changed} if changed else {'state': 'ready'})
        return self.status

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.stdin.write(b'{"op": "close"}\n')
                process.stdin.flush()
                process.wait(3)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        finally:
            kill_tree(process)
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # --- calls ------------------------------------------------------------------------------------------------
    def offered(self):
        """Tools a model may be shown: qualified name, the user-approved description, input schema and family."""
        return [{'name': name, 'description': pin.description, 'input_schema': schema, 'family': pin.family,
                 'effect': pin.effect} for name, (_, pin, schema) in self.tools.items()]

    def call(self, name, arguments, *, confirmed=False):
        entry = self.tools.get(name)
        if entry is None:
            raise McpError('This tool is not attached or not approved.')
        server, pin, _ = entry
        if pin.effect == 'confirm' and not confirmed:
            raise ConfirmRequired('This action needs your confirmation first.')
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 20000:
            raise McpError('Tool arguments must be a small JSON object.')
        left = self.session_seconds - (self.clock() - self.started_at)
        if left <= 0:
            self.close()
            raise McpError('The MCP session time limit was reached.')
        timeout = min(pin.timeout_s, left)
        answer = self._ask({'op': 'call', 'server': server.id, 'tool': pin.name, 'arguments': arguments,
                            'timeout': timeout, 'max_chars': pin.max_chars}, timeout + CALL_GRACE_SECONDS)
        if answer.get('timeout'):
            self.close()                    # a stuck call may still be running in the server: end the session
            raise McpError('The MCP server did not answer in time; the session was stopped.')
        if not answer.get('ok'):
            raise McpError('The MCP tool failed: ' + str(answer.get('error', 'unknown'))[:200])
        return answer

    def trace(self, name, answer, evidence_id):
        """A chat/agent trace for a call result. Text is untrusted data, redacted and bounded."""
        _, pin, _ = self.tools[name]
        text = redact(answer.get('text') or '')
        return {'name': name, 'status': 'error' if answer.get('is_error') else 'ok', 'evidence_id': evidence_id,
                'detail': text[:DETAIL_CHARS], 'truncated': bool(answer.get('truncated')) or len(text) > DETAIL_CHARS,
                'family': pin.family, 'server': name.split('/', 1)[0][4:],
                'retrieved_at': datetime.now(timezone.utc).isoformat(), 'untrusted_content': True}


def discover(guard, config, server_id):
    """Start one server (approved or pending) and return its reported tools with their hashes, for the approval
    view. Nothing is saved; the HUD calls McpSettings.approve with the user's choices."""
    with McpSession(guard, config, only=server_id) as session:
        status = session.status.get(server_id, {'state': 'failed', 'reason': 'Unknown server.'})
        return {'status': status, 'tools': session.discovered.get(server_id, [])}
