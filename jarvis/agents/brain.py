"""The chat "brain": one contract that every model provider implements, the registry of providers, the saved
selection, and the list of what may be sent to a model outside this PC.

User decision 2026-10-28 (board 5d002040): the selection applies to chat and voice answers only; classification,
lead judging and Wiki review stay on the local default model. A request keeps the model it started with. A failed
switch keeps the old selection. There is no automatic fallback to an external model. The first switch from local
to an external provider needs an explicit confirmation that shows EXTERNAL_SENDS and NEVER_SENT.
"""
from dataclasses import asdict, dataclass, field
import http.client
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Protocol

from jarvis.agents.chat import ChatError
from jarvis.core.guardrails import GuardrailError
from jarvis.core.paths import WORKERS_ROOT


class BrainError(ChatError):
    """A provider could not answer or a model cannot be selected. code is one of CODES; the text is shown to the
    user and never holds secrets or raw provider output."""
    CODES = frozenset({'unavailable', 'auth', 'timeout', 'invalid_response', 'too_large', 'max_turns', 'rate_limited'})

    def __init__(self, code, message):
        if code not in self.CODES:
            raise ValueError('Unknown brain error code.')
        super().__init__(message)
        self.code = code


class ConfirmRequired(BrainError):
    """The first switch from local to this external provider needs the user's explicit confirmation."""

    def __init__(self, info):
        super().__init__('unavailable', 'External provider needs confirmation.')
        self.info = info


@dataclass(frozen=True)
class Capabilities:
    json_schema: bool       # required for chat: the reply is one JSON object that matches the schema
    streaming: bool         # on_delta gets the decoded "reply" text as it arrives
    tools: bool             # native tool calls; chat runs its own JSON tool loop, so this is informational


@dataclass(frozen=True)
class ModelInfo:
    provider_id: str
    model_id: str
    label: str
    external: bool
    capabilities: Capabilities
    available: bool
    reason: str | None = None

    def public(self):
        return asdict(self)


@dataclass(frozen=True)
class BrainRequest:
    messages: list
    schema: dict
    deadline: float                                     # time.monotonic()
    on_delta: Callable[[str], None] | None = None


@dataclass(frozen=True)
class Selection:
    provider_id: str
    model_id: str
    connection_ref: str | None = None                   # the name of a credential entry kept elsewhere, never a secret

    def public(self):
        return asdict(self)


class Provider(Protocol):
    provider_id: str
    label: str
    external: bool

    def models(self) -> list[ModelInfo]: ...
    def check(self, model_id: str) -> ModelInfo: ...
    def infer(self, model_id: str, request: BrainRequest) -> dict: ...
    def warm_up(self, model_id: str) -> None: ...


# --- what an external model may receive (user decision 2026-10-28, limit 2) -------------------------------------
# An allowlist: a tool or source that is not named here is local-only, so a new source never leaks by default.
EXTERNAL_SENDS = (
    ('conversation', '지금 대화(질문과 앞선 답변)'),
    ('public_web', '공개 웹 검색 발췌·공개 페이지·등록된 공개 피드'),
    ('system_status', 'PC 상태(CPU·메모리·디스크)'),
    ('approved_notes', '승인된 Vault 노트 발췌(출처 포함)'),
)
NEVER_SENT = ('메일', '일정(캘린더)', '오늘의 브리핑', '로컬 문서', '프로그램 정적 분석(REA) 결과', '승인되지 않은 노트',
              '비밀번호·키 같은 비밀값')
EXTERNAL_FAMILIES = frozenset(family for family, _ in EXTERNAL_SENDS)
TOOL_FAMILIES = {
    'search_public_web': 'public_web', 'read_public_web_page': 'public_web', 'collect_web_information': 'public_web',
    'read_registered_feed_with_agent_reach': 'public_web', 'list_web_sources': 'public_web',
    'get_system_status': 'system_status',
    # Both read only approved, non-secret notes (ObsidianStore checks review_status).
    'search_obsidian_vault': 'approved_notes', 'prepare_obsidian_context': 'approved_notes',
}
# The jarvis MCP tool names an external provider may call (P2 builds its read-only MCP allowlist from this).
EXTERNAL_TOOLS = tuple(sorted(name for name, family in TOOL_FAMILIES.items() if family in EXTERNAL_FAMILIES))


def external_allowed(tool_name):
    """True when results of this tool may reach an external model. Unknown names are local-only."""
    return TOOL_FAMILIES.get(tool_name) in EXTERNAL_FAMILIES


def consent_payload(info):
    return {'provider': info.label, 'sends': [label for _, label in EXTERNAL_SENDS], 'never_sends': list(NEVER_SENT)}


# --- local Ollama ------------------------------------------------------------------------------------------------
OLLAMA_HOST, OLLAMA_PORT = '127.0.0.1', 11434
MODEL_TAG = re.compile(r'[a-zA-Z0-9_.-]+:[a-zA-Z0-9_.-]+')
MAX_OUTPUT = 262144
MAX_INPUT = 65536
MAX_MODELS = 32
OLLAMA_CAPABILITIES = Capabilities(json_schema=True, streaming=True, tools=False)


def spawn_chat_worker():
    return subprocess.Popen([sys.executable, '-I', str(WORKERS_ROOT / 'chat_step.py')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}},
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def ollama_get(path, body=None, *, timeout=2):
    """One bounded request to the fixed local Ollama: /api/tags or /api/show only, no redirects or proxies."""
    if path not in {'/api/tags', '/api/show'}:
        raise ValueError('Unsupported Ollama path.')
    connection = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=timeout)
    try:
        if body is None:
            connection.request('GET', path, headers={'Accept-Encoding': 'identity'})
        else:
            connection.request('POST', path, json.dumps(body).encode(),
                               {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200 or (response.getheader('Content-Encoding') or 'identity').lower() != 'identity':
            raise ValueError('Ollama returned a non-success response.')
        data = response.read(MAX_OUTPUT + 1)
        if len(data) > MAX_OUTPUT:
            raise ValueError('Ollama response too large.')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('Malformed Ollama response.')
        return value
    finally:
        connection.close()


class OllamaProvider:
    """Installed local Ollama models, answered by the fixed chat_step worker (no tools, no credentials)."""
    provider_id = 'ollama'
    label = '로컬 Ollama'
    external = False

    def __init__(self, guard, *, spawn=None, fetch=None):
        self.guard = guard
        self.spawn = spawn or spawn_chat_worker
        self.fetch = fetch or ollama_get

    def _info(self, model_id, available, reason=None):
        return ModelInfo(self.provider_id, model_id, model_id, False, OLLAMA_CAPABILITIES, available, reason)

    def _judge(self, model_id, installed):
        if not isinstance(model_id, str) or not MODEL_TAG.fullmatch(model_id) or 'cloud' in model_id.lower():
            return self._info(str(model_id)[:80], False, '로컬 모델 이름이 아닙니다.')
        if model_id not in installed:
            return self._info(model_id, False, '설치되지 않은 모델입니다. 자동으로 내려받지 않습니다.')
        try:
            show = self.fetch('/api/show', {'model': model_id})
        except (OSError, ValueError, http.client.HTTPException):
            return self._info(model_id, False, '모델 정보를 확인하지 못했습니다.')
        details = show.get('details') if isinstance(show.get('details'), dict) else {}
        capabilities = show.get('capabilities')
        if show.get('remote_host') or show.get('remote_model') or details.get('format') != 'gguf':
            return self._info(model_id, False, '이 PC에서 실행되는 모델이 아닙니다.')
        if not isinstance(capabilities, list) or 'completion' not in capabilities:
            return self._info(model_id, False, '대화용 모델이 아닙니다.')
        return self._info(model_id, True)

    def _installed(self):
        tags = self.fetch('/api/tags')
        models = tags.get('models')
        if not isinstance(models, list):
            raise ValueError('Malformed Ollama response.')
        return [item['name'] for item in models if isinstance(item, dict) and isinstance(item.get('name'), str)]

    def models(self):
        self.guard.check_running()
        try:
            installed = self._installed()[:MAX_MODELS]
        except (OSError, ValueError, http.client.HTTPException):
            return []
        return [self._judge(name, installed) for name in installed]

    def check(self, model_id):
        self.guard.check_running()
        try:
            installed = self._installed()
        except (OSError, ValueError, http.client.HTTPException):
            return self._info(str(model_id)[:80], False, 'Ollama가 응답하지 않습니다.')
        return self._judge(model_id, installed)

    def warm_up(self, model_id):
        process = None
        try:
            self.guard.check_running()
            process = self.spawn()
            process.communicate(json.dumps({'model': model_id, 'warm': True}).encode(), timeout=120)
        except Exception:
            pass
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    def infer(self, model_id, request):
        self.guard.check_running()
        body = {'model': model_id, 'messages': request.messages, 'schema': request.schema}
        if request.on_delta is not None:
            body['stream'] = True
        payload = json.dumps(body, ensure_ascii=False)
        if len(payload.encode()) > MAX_INPUT:
            raise BrainError('too_large', 'Chat context limit reached.')
        process = self.spawn()
        if request.on_delta is not None:
            return self.stream(process, payload.encode(), request.deadline, request.on_delta)
        try:
            pending = payload.encode()
            while True:
                self.guard.check_running()
                if time.monotonic() > request.deadline:
                    raise BrainError('timeout', 'Local model response timed out.')
                try:
                    output, _ = process.communicate(pending, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pending = None
            if len(output) > MAX_OUTPUT or process.returncode:
                raise BrainError('invalid_response', 'Local model unavailable or returned an invalid response.')
            value = json.loads(output)
            if value.get('ok') is not True:
                raise BrainError('unavailable', 'Local model response failed.')
            return value['result']
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def stream(self, process, payload, deadline, on_delta):
        """Same guards as the blocking path (stop marker, deadline, 256 KB output cap, exit code, ok flag), but
        reads the worker's lines as they arrive and passes each reply delta to on_delta. A failing on_delta only
        loses the draft; the answer itself still comes from the final line."""
        lines = queue.Queue()
        invalid = 'Local model unavailable or returned an invalid response.'

        def write():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (OSError, ValueError):  # worker gone or pipe already closed
                pass

        def read():
            try:
                for line in iter(process.stdout.readline, b''):
                    lines.put(line)
            except (OSError, ValueError):  # pipe closed by the finally below
                pass
            finally:
                lines.put(None)

        threading.Thread(target=write, daemon=True).start()
        threading.Thread(target=read, daemon=True).start()
        total, final = 0, None
        try:
            while True:
                self.guard.check_running()
                if time.monotonic() > deadline:
                    raise BrainError('timeout', 'Local model response timed out.')
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                total += len(line)
                if total > MAX_OUTPUT:
                    raise BrainError('invalid_response', invalid)
                try:
                    value = json.loads(line)
                except ValueError:
                    raise BrainError('invalid_response', invalid) from None
                if isinstance(value, dict) and set(value) == {'delta'} and isinstance(value['delta'], str):
                    try:
                        on_delta(value['delta'])
                    except Exception:
                        pass
                else:
                    final = value
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise BrainError('invalid_response', invalid) from None
            if process.returncode or not isinstance(final, dict):
                raise BrainError('invalid_response', invalid)
            if final.get('ok') is not True:
                raise BrainError('unavailable', 'Local model response failed.')
            return final['result']
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


# --- saved selection ---------------------------------------------------------------------------------------------
class BrainSettings:
    """The selection on this PC: JarvisWorkspace/.jarvis-settings/brain.json. It holds no secrets. A missing,
    linked or invalid file reads as "no selection" (the default local model)."""
    DIRECTORY = '.jarvis-settings'
    PATH = '.jarvis-settings/brain.json'
    MAX_BYTES = 4096

    def __init__(self, guard):
        self.guard = guard

    def load(self):
        empty = {'selection': None, 'external_confirmed': []}
        try:
            path = self.guard.validate_path(self.PATH, must_exist=False)
            if not path.exists():
                return empty
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > self.MAX_BYTES:
                return empty
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, GuardrailError):
            return empty
        if not isinstance(data, dict) or data.get('version') != 1:
            return empty
        raw, confirmed = data.get('selection'), data.get('external_confirmed', [])
        selection = None
        if (isinstance(raw, dict) and set(raw) == {'provider_id', 'model_id', 'connection_ref'}
                and all(isinstance(raw[key], str) and 0 < len(raw[key]) <= 120 for key in ('provider_id', 'model_id'))
                and (raw['connection_ref'] is None or isinstance(raw['connection_ref'], str))):
            selection = Selection(raw['provider_id'], raw['model_id'], raw['connection_ref'])
        if not isinstance(confirmed, list) or not all(isinstance(item, str) and len(item) <= 64 for item in confirmed):
            confirmed = []
        return {'selection': selection, 'external_confirmed': confirmed[:16]}

    def save(self, selection, external_confirmed):
        self.guard.check_running()
        directory = self.guard.validate_path(self.DIRECTORY, must_exist=False)
        directory.mkdir(exist_ok=True)
        path = self.guard.validate_path(self.PATH, must_exist=False)
        if path.exists() and (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1):
            raise GuardrailError('Linked settings files are forbidden.')
        text = json.dumps({'version': 1, 'selection': selection.public(),
                           'external_confirmed': sorted(set(external_confirmed))[:16]}, ensure_ascii=False, indent=2)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, path)


# --- registry ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Choice:
    """The model one request uses, fixed when the request starts."""
    selection: Selection
    provider: object = field(compare=False)
    label: str = ''
    external: bool = False

    def record(self, local_reason=None):
        value = {'provider_id': self.selection.provider_id, 'model_id': self.selection.model_id,
                 'label': self.label, 'external': self.external}
        if local_reason:
            value['local_reason'] = local_reason
        return value


class BrainRegistry:
    def __init__(self, providers, default, settings=None):
        self.providers = {provider.provider_id: provider for provider in providers}
        if default.provider_id not in self.providers or self.providers[default.provider_id].external:
            raise ValueError('The default brain must be a registered local provider.')
        self.default, self.settings = default, settings
        self.lock = threading.Lock()

    def _choice(self, selection, provider):
        return Choice(selection, provider, selection.model_id if not provider.external else
                      f'{provider.label} · {selection.model_id}', provider.external)

    def default_choice(self):
        return self._choice(self.default, self.providers[self.default.provider_id])

    def current(self):
        """The saved selection when its provider is registered and, if external, confirmed; else the default."""
        saved = self.settings.load() if self.settings is not None else {'selection': None, 'external_confirmed': []}
        selection = saved['selection']
        provider = self.providers.get(selection.provider_id) if selection is not None else None
        if provider is None or (provider.external and selection.provider_id not in saved['external_confirmed']):
            return self.default_choice()
        return self._choice(selection, provider)

    def listing(self):
        current = self.current()
        providers = []
        for provider in self.providers.values():
            try:
                models = [info.public() for info in provider.models()]
            except GuardrailError:
                raise
            except Exception:
                models = []
            providers.append({'provider_id': provider.provider_id, 'label': provider.label,
                              'external': provider.external, 'models': models})
        return {'selection': {**current.selection.public(), 'label': current.label, 'external': current.external},
                'default': {**self.default.public(), 'label': self.default_choice().label, 'external': False},
                'scope': ['chat', 'voice'], 'providers': providers}

    def select(self, provider_id, model_id, connection_ref=None, *, confirm_external=False):
        """Validate and save a new selection. Any failure keeps the old one and says why."""
        provider = self.providers.get(provider_id)
        if provider is None:
            raise BrainError('unavailable', '등록되지 않은 제공자입니다.')
        info = provider.check(model_id)
        if not info.available:
            raise BrainError('unavailable', info.reason or '이 모델을 쓸 수 없습니다.')
        if not info.capabilities.json_schema:
            raise BrainError('unavailable', '이 모델은 자비스 답변 형식(JSON)을 지원하지 않습니다.')
        with self.lock:
            saved = self.settings.load() if self.settings is not None else {'selection': None, 'external_confirmed': []}
            confirmed = list(saved['external_confirmed'])
            if provider.external and provider_id not in confirmed:
                if not confirm_external:
                    raise ConfirmRequired(info)
                confirmed.append(provider_id)
            selection = Selection(provider_id, model_id, connection_ref)
            if self.settings is not None:
                self.settings.save(selection, confirmed)
        return self._choice(selection, provider)
