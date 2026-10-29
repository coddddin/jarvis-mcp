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


# --- file paths in tool arguments (board 204bc125) -------------------------------------------------------------
# A pinned tool declares which top-level arguments are file paths (path_args). At call time each such value must be
# a path inside JarvisWorkspace that the guard accepts (no UNC/device, ADS, '..', links or junctions) AND inside one
# of the server's user-approved roots or its own sandbox; the checked absolute path replaces the value. Any other
# string argument that looks like a Windows path (drive or UNC, \\?\, file: URL, ..\ segment) is refused, so an
# undeclared path argument cannot reach the server. Ordinary text and http(s) URLs pass.
PATH_WORDS = frozenset({'path', 'paths', 'pathname', 'file', 'files', 'filename', 'filenames', 'filepath',
                        'filepaths', 'dir', 'dirs', 'directory', 'directories', 'folder', 'folders', 'binary',
                        'binaries'})
LOOKS_LIKE_PATH = re.compile(r'^\s*(?:[A-Za-z]:[\\/]|\\\\|file:|\.\./)|\.\.\\', re.IGNORECASE)
MAX_PATH_VALUES = 16
MAX_ROOTS = 8


def path_like_name(name):
    """True when a word of the argument name (snake_case or camelCase) usually means a file path: path, input_file,
    outputDir, binary_path, fileName... 'profile' or 'url' are not."""
    if not isinstance(name, str):
        return False
    words = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])', name)
    return any(word.lower() in PATH_WORDS for word in words) or name.lower() in PATH_WORDS


def path_suspects(schema):
    """(top-level names, nested names) of path-like properties in a tool's input schema."""
    top, nested = [], []

    def walk(node, depth):
        if not isinstance(node, dict):
            return
        properties = node.get('properties')
        for name, child in (properties.items() if isinstance(properties, dict) else ()):
            if path_like_name(name):
                (top if depth == 0 else nested).append(name)
            walk(child, depth + 1)
        for key in ('items', 'additionalProperties'):
            walk(node.get(key), depth + 1)
        for key in ('anyOf', 'oneOf', 'allOf'):
            for child in node.get(key) or []:
                walk(child, depth)
        for child in node.get('prefixItems') or []:
            walk(child, depth + 1)
        for child in (node.get('$defs') or node.get('definitions') or {}).values():
            walk(child, 1)
    walk(schema or {}, 0)
    return sorted(set(top)), sorted(set(nested) - set(top))


def unchecked_paths(pin, schema):
    """Path-like argument names of a tool that its pin neither declares nor marks as text, plus nested ones when the
    pin is not deny. A pin approved before path_args existed shows up here and must be approved again."""
    top, nested = path_suspects(schema)
    known = {item.name for item in pin.path_args} | set(pin.not_paths) | set(pin.blocked_args)
    return [name for name in top if name not in known] + (nested if pin.effect != 'deny' else [])


def valid_root(root):
    """A workspace-relative folder: no drive, UNC, '..', dot folders (.jarvis-* state and settings) or odd characters.
    The workspace root itself is not a root."""
    if not isinstance(root, str) or not 0 < len(root) <= 200 or root.strip() != root:
        return False
    parts = re.split(r'[\\/]', root)
    return all(part and not part.startswith('.') and part.strip() == part
               and not re.search(r'[<>:"|?*%$\x00-\x1f]', part) for part in parts)


def looks_like_path(value):
    return isinstance(value, str) and LOOKS_LIKE_PATH.search(value) is not None


BASE_ENV = ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'PATHEXT', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE',
            'HOMEDRIVE', 'HOMEPATH', 'SYSTEMDRIVE', 'USERNAME', 'PROCESSOR_ARCHITECTURE')


class McpError(ValueError):
    pass


class ConfirmRequired(McpError):
    """The tool has effect 'confirm': the caller must get the user's yes for this call first."""


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class PathArg(Strict):
    name: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    mode: Literal['read', 'write'] = 'read'       # read: must exist; write: the tool may create or change it


class ToolPin(Strict):
    name: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    family: Literal['public_web', 'local'] = 'local'
    effect: Literal['read', 'auto', 'confirm', 'deny'] = 'confirm'
    timeout_s: int = Field(default=30, ge=1, le=120)
    max_chars: int = Field(default=8000, ge=100, le=50000)
    schema_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    description: str = Field(default='', max_length=500)       # the user-approved text a model sees
    path_args: tuple[PathArg, ...] = Field(default=(), max_length=8)
    not_paths: tuple[str, ...] = Field(default=(), max_length=16)  # path-like names the user said are plain text
    blocked_args: tuple[str, ...] = Field(default=(), max_length=16)  # arguments a model may never set (e.g. filename)

    @model_validator(mode='after')
    def deny_unsafe(self):
        if self.name in ALWAYS_DENY and self.effect != 'deny':
            raise ValueError(f'{self.name} is always denied.')
        names = [item.name for item in self.path_args] + list(self.not_paths) + list(self.blocked_args)
        if len(set(names)) != len(names):
            raise ValueError('A path argument is listed twice.')
        if any(item.mode == 'write' for item in self.path_args) and self.effect in ('read', 'auto'):
            raise ValueError('A tool that writes files needs confirm or deny.')
        return self


class ServerConfig(Strict):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,31}$')
    label: str = Field(min_length=1, max_length=60)
    command: str = Field(min_length=3, max_length=500)
    args: tuple[str, ...] = Field(default=(), max_length=32)
    env_names: tuple[str, ...] = Field(default=(), max_length=16)
    state: Literal['pending', 'approved'] = 'pending'
    tools: tuple[ToolPin, ...] = Field(default=(), max_length=64)
    roots: tuple[str, ...] = Field(default=(), max_length=MAX_ROOTS)   # workspace folders path arguments may use

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
        if not all(valid_root(root) for root in self.roots) or len(set(self.roots)) != len(self.roots):
            raise ValueError('A root must be a folder inside the workspace (not the workspace itself or a dot folder).')
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
        reported in discover(); choices maps tool name -> {family, effect, description?, timeout_s?, max_chars?,
        path_args?, not_paths?, blocked_args?}. Tools not chosen stay hidden. Re-approving replaces every pin.

        Path arguments fail closed: a top-level property with a path-like name becomes a read path argument unless
        the choice lists it in path_args (to set the mode), not_paths (plain text) or blocked_args (a model may not
        set it at all, e.g. Playwright's optional filename that saves a file). A path-like name nested inside
        an object or array cannot be checked, so such a tool can only be pinned as deny. A tool with path arguments
        stays family local (never sent to an external model) unless the choice says public_web."""
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
            schema = by_name[name].get('input_schema') or {}
            top, nested = path_suspects(schema)
            if nested and choice.get('effect', 'confirm') != 'deny':
                raise McpError(f'{name}: 안쪽 인자({", ".join(nested[:3])})에 파일 경로가 들어갈 수 있어 검사할 수 '
                               '없습니다. 이 도구는 거부로만 둘 수 있습니다.')
            declared = [dict(item) if isinstance(item, dict) else {'name': item} for item in choice.pop('path_args', ())]
            not_paths = tuple(choice.pop('not_paths', ()))
            blocked = tuple(choice.pop('blocked_args', ()))
            properties = schema.get('properties') if isinstance(schema.get('properties'), dict) else {}
            if (any(item.get('name') not in properties for item in declared)
                    or (set(not_paths) | set(blocked)) - set(properties)):
                raise McpError(f'{name}: 도구에 없는 인자 이름입니다.')
            if set(blocked) & set((schema.get('required') or [])):
                raise McpError(f'{name}: 꼭 필요한 인자는 막을 수 없습니다.')
            known = {item['name'] for item in declared} | set(not_paths) | set(blocked)
            declared += [{'name': arg, 'mode': 'read'} for arg in top if arg not in known]
            pins.append(ToolPin(name=name, schema_sha256=schema_hash(by_name[name]), path_args=tuple(declared),
                                not_paths=not_paths, blocked_args=blocked, **choice))
        approved = server.model_copy(update={'state': 'approved', 'tools': tuple(pins)})
        ServerConfig.model_validate(approved.model_dump())
        self.save(McpConfig(servers=tuple(approved if item.id == server_id else item for item in config.servers)))
        return approved

    def set_roots(self, server_id, roots):
        """Replace the folders this server's path arguments may use. Each must be an existing folder inside the
        workspace, reached without links; the workspace itself and dot folders (.jarvis-* state, settings) are
        refused. Returns the resolved folders for the approval view."""
        config = self.load()
        server = next((item for item in config.servers if item.id == server_id), None)
        if server is None:
            raise McpError('Unknown server.')
        try:
            updated = ServerConfig.model_validate({**server.model_dump(), 'roots': tuple(roots)})
        except ValidationError:
            raise McpError('작업공간 안의 일반 폴더만 승인할 수 있습니다(작업공간 자체와 점(.)으로 시작하는 폴더 제외).') from None
        resolved = root_view(self.guard, updated)
        bad = [item for item in resolved if not item['ok']]
        if bad:
            raise McpError(f"폴더를 쓸 수 없습니다: {bad[0]['root']} ({bad[0]['reason']})")
        self.save(McpConfig(servers=tuple(updated if item.id == server_id else item for item in config.servers)))
        return resolved

    def remove(self, server_id):
        config = self.load()
        self.save(McpConfig(servers=tuple(item for item in config.servers if item.id != server_id)))


def root_view(guard, server):
    """Each root with the folder it resolves to now, for the approval view: [{root, path, ok, reason}]."""
    result = []
    for root in server.roots:
        try:
            if not valid_root(root):
                raise GuardrailError('작업공간 안의 일반 폴더만 쓸 수 있습니다.')
            path = guard.validate_path(root)
            if path == guard.workspace or not path.is_dir():
                raise GuardrailError('작업공간 안의 폴더가 아닙니다.')
            result.append({'root': root, 'path': str(path), 'ok': True, 'reason': None})
        except GuardrailError as exc:
            result.append({'root': root, 'path': None, 'ok': False, 'reason': str(exc)})
    return result


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
                if (tool is None or schema_hash(tool) != pin.schema_sha256
                        or unchecked_paths(pin, tool.get('input_schema') or {})):
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

    def offered_for(self, external):
        """offered(), without local-family tools when the answering model is external (jarvis.agents.brain)."""
        return [tool for tool in self.offered() if not external or tool['family'] == 'public_web']

    def _allowed_folders(self, server):
        folders = [self.guard.validate_path(f'{self.SANDBOX}/{server.id}', must_exist=False)]
        for root in server.roots:
            if valid_root(root):
                folder = self.guard.validate_path(root)
                if folder != self.guard.workspace and folder.is_dir():
                    folders.append(folder)
        return folders

    def _checked_path(self, server, value, mode):
        if not isinstance(value, str) or value.strip().lower().startswith('file:'):
            raise McpError('파일 경로는 작업공간 안의 경로 문자열이어야 합니다.')
        try:
            path = self.guard.validate_path(value.strip(), must_exist=(mode == 'read'))
            folders = self._allowed_folders(server)
        except GuardrailError as exc:
            raise McpError(f'이 경로는 쓸 수 없습니다: {exc}') from None
        if not any(path == folder or path.is_relative_to(folder) for folder in folders):
            raise McpError('승인된 폴더 밖의 경로입니다. HUD에서 이 서버의 폴더를 승인해 주십시오.')
        return str(path)

    def checked_arguments(self, server, pin, arguments):
        """The arguments with every declared path replaced by its checked absolute path. Raises McpError when a
        declared path is outside the approved folders or an undeclared string looks like a path."""
        declared = {item.name: item.mode for item in pin.path_args}
        result = dict(arguments)
        blocked = [name for name in pin.blocked_args if result.get(name) is not None]
        if blocked:
            raise McpError(f'이 도구에서는 {blocked[0]} 인자를 쓸 수 없습니다.')
        for name, mode in declared.items():
            if result.get(name) is None:
                continue
            value = result[name]
            if isinstance(value, list):
                if len(value) > MAX_PATH_VALUES:
                    raise McpError('파일 경로가 너무 많습니다.')
                result[name] = [self._checked_path(server, item, mode) for item in value]
            else:
                result[name] = self._checked_path(server, value, mode)

        def scan(value):
            if isinstance(value, str):
                if looks_like_path(value):
                    raise McpError('경로로 보이는 값은 승인된 경로 인자로만 보낼 수 있습니다.')
            elif isinstance(value, list):
                for item in value:
                    scan(item)
            elif isinstance(value, dict):
                for item in value.values():
                    scan(item)
        for key, value in result.items():
            if key not in declared and key not in pin.not_paths:     # not_paths: the user approved it as plain text
                scan(value)
        return result

    def call(self, name, arguments, *, confirmed=False):
        entry = self.tools.get(name)
        if entry is None:
            raise McpError('This tool is not attached or not approved.')
        server, pin, _ = entry
        if pin.effect == 'confirm' and not confirmed:
            raise ConfirmRequired('This action needs your confirmation first.')
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 20000:
            raise McpError('Tool arguments must be a small JSON object.')
        arguments = self.checked_arguments(server, pin, arguments)
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
        tools = [{**tool, 'path_suspects': dict(zip(('top', 'nested'), path_suspects(tool.get('input_schema') or {})))}
                 for tool in session.discovered.get(server_id, [])]
        server = next((item for item in config.servers if item.id == server_id), None)
        return {'status': status, 'tools': tools, 'roots': root_view(guard, server) if server is not None else []}
