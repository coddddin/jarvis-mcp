"""Chat brains over company APIs with the user's own key (brain-swap P3, board d897dad4).

One provider per company, no fixed model list: models come from the company's models endpoint, and the user may
also type a model id. The key lives in Windows Credential Manager under connection_ref 'JARVIS/brain/<provider_id>'
(jarvis.core.credentials); it is read for each request and never returned, logged or saved elsewhere.

Formats, checked against the official docs on 2026-10-29:
- Anthropic: GET /v1/models, POST /v1/messages with output_config.format json_schema and stream:true; headers
  x-api-key and anthropic-version 2023-06-01. platform.claude.com/docs/en/api/models/list,
  /build-with-claude/structured-outputs, /build-with-claude/streaming
- OpenAI: GET /v1/models, POST /v1/chat/completions with response_format json_schema (strict) and stream:true;
  Authorization: Bearer. developers.openai.com/api/docs/guides/structured-outputs, /api/reference (chat, models)
- xAI: GET /v1/language-models, POST /v1/chat/completions (same shape as OpenAI). docs.x.ai/developers/
  rest-api-reference/inference/models.md, chat-completions.md, model-capabilities/text/structured-outputs.md
- DeepSeek: GET /models, POST /chat/completions; response_format is only text or json_object, so the schema goes
  into the prompt (the docs ask for the word "json" and an example) and the answer is checked here.
  api-docs.deepseek.com/api/create-chat-completion, /guides/json_mode
- Google Gemini: GET /v1beta/models, POST /v1beta/models/{m}:streamGenerateContent?alt=sse with
  generationConfig.responseMimeType application/json + responseJsonSchema; header x-goog-api-key.
  ai.google.dev/api/models, /api/generate-content, /gemini-api/docs/structured-output, /migrate-to-interactions
No provider accepts the full chat schema (maxLength, maxItems and minItems > 1 are missing in some), so each gets
the common subset and every answer is validated here against the full schema before ChatService sees it.
"""
import http.client
import json
import queue
import re
import ssl
import threading
import time
from urllib.parse import quote, urlencode

from jarvis.agents.brain import BrainError, BrainRequest, Capabilities, ModelInfo
from jarvis.agents.claude_brain import reply_text
from jarvis.core.credentials import CredentialError, valid_secret
from jarvis.core.redaction import redact

MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}')
MAX_OUTPUT = 256 * 1024          # same cap as the local worker and the Claude CLI
MAX_INPUT = 64 * 1024
MAX_LIST = 2 * 1024 * 1024       # model listings carry prices and capabilities; still bounded
MAX_MODELS = 120
MAX_PAGES = 3
ANSWER_TOKENS = 16000            # upper bound only; replies are at most 6000 characters
PROBE_TOKENS = 1024              # reasoning models spend part of this before the tiny answer
LIST_TTL = 600.0
LIST_TIMEOUT = 10
SOCKET_TIMEOUT = 60
SUMMARY_CHARS = 120
SCHEMA_CAPABILITIES = Capabilities(json_schema=True, streaming=True, tools=False)
PROBE_SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
                'additionalProperties': False}
PROBE_MESSAGES = [{'role': 'system', 'content': 'Answer with JSON only.'},
                  {'role': 'user', 'content': 'Return {"ok": true} as JSON.'}]


# --- schema: what is sent, and the local check of every answer ------------------------------------------------
SENT_KEYS = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'description', 'anyOf'}
ANNOTATIONS = {'title', 'description', 'default'}
CHECKED = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'pattern', 'anyOf',
           'maxLength', 'minLength', 'maxItems', 'minItems'}


def sent_schema(schema):
    """The part of the schema every provider accepts: types, properties (all required, no extra keys), items, enum,
    anyOf and descriptions. Length/count limits and patterns are checked locally by validate()."""
    if not isinstance(schema, dict):
        raise ValueError('Schema must be an object.')
    result = {key: value for key, value in schema.items() if key in SENT_KEYS and key not in
              {'properties', 'items', 'anyOf', 'required', 'additionalProperties'}}
    if result.get('enum') == []:
        del result['enum']           # some providers reject an empty enum; the local check still allows nothing
    if 'anyOf' in schema:
        result['anyOf'] = [sent_schema(item) for item in schema['anyOf']]
    if 'items' in schema:
        result['items'] = sent_schema(schema['items'])
    if schema.get('type') == 'object' or 'properties' in schema:
        properties = schema.get('properties', {})
        result['properties'] = {name: sent_schema(item) for name, item in properties.items()}
        result['required'] = list(properties)
        result['additionalProperties'] = False
    return result


TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'null': type(None)}


def _is_type(value, name):
    if name == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if name == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return name in TYPES and isinstance(value, TYPES[name])


def validate(value, schema, where='$'):
    """Raise ValueError unless value matches schema. Unknown validation keywords fail closed."""
    unknown = set(schema) - CHECKED - ANNOTATIONS
    if unknown:
        raise ValueError(f'Unsupported schema keyword at {where}: {sorted(unknown)[0]}')
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                validate(value, option, where)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f'No anyOf option matches at {where}.')
    kind = schema.get('type')
    if kind is not None and not any(_is_type(value, name) for name in ([kind] if isinstance(kind, str) else kind)):
        raise ValueError(f'Wrong type at {where}.')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'Value not allowed at {where}.')
    if isinstance(value, str):
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            raise ValueError(f'Too long at {where}.')
        if 'minLength' in schema and len(value) < schema['minLength']:
            raise ValueError(f'Too short at {where}.')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise ValueError(f'Pattern mismatch at {where}.')
    if isinstance(value, list):
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            raise ValueError(f'Too many items at {where}.')
        if 'minItems' in schema and len(value) < schema['minItems']:
            raise ValueError(f'Too few items at {where}.')
        if 'items' in schema:
            for index, item in enumerate(value):
                validate(item, schema['items'], f'{where}[{index}]')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        missing = [name for name in schema.get('required', []) if name not in value]
        if missing:
            raise ValueError(f'Missing {missing[0]} at {where}.')
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError(f'Unexpected key at {where}.')
        for name, item in value.items():
            if name in properties:
                validate(item, properties[name], f'{where}.{name}')


# --- HTTPS ------------------------------------------------------------------------------------------------------
def open_https(host, timeout):
    """A connection to one fixed host: certificate checked, no proxy from the environment, no redirects."""
    return http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())


def summary(text):
    """One cleaned, redacted line of a provider error message, at most 120 characters."""
    text = ' '.join(''.join(char if char.isprintable() else ' ' for char in redact(str(text))).split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + '…'


def error_message(data):
    """The provider's own error message from a JSON error body, if any."""
    if isinstance(data, dict):
        error = data.get('error')
        if isinstance(error, dict) and isinstance(error.get('message'), str):
            return error['message']
        if isinstance(error, str):
            return error
        if isinstance(data.get('message'), str):
            return data['message']
    if isinstance(data, list) and data:
        return error_message(data[0])
    return ''


class ApiProvider:
    """Shared parts: key lookup, model listing cache, capability probe, streamed answer with local validation.
    Subclasses describe one company's endpoints and stream format."""
    provider_id = ''
    label = ''
    external = True
    uses_connection_ref = True
    host = ''
    company = ''               # shown in messages, e.g. 'OpenAI'
    prompted_json = False      # True when the API has no schema mode (DeepSeek): schema in prompt, one retry

    def __init__(self, guard, *, credentials, connect=open_https, clock=time.monotonic):
        self.guard, self.credentials, self.connect, self.clock = guard, credentials, connect, clock
        self._models, self._models_at = None, float('-inf')
        self._verified = set()
        self._lock = threading.Lock()

    @property
    def connection_ref(self):
        return 'JARVIS/brain/' + self.provider_id

    # --- key ------------------------------------------------------------------------------------------------
    def _key(self):
        try:
            return self.credentials.get(self.connection_ref)
        except CredentialError:
            return None

    def connected(self):
        return self._key() is not None

    def set_key(self, secret):
        """Check the key with the models endpoint, then store it. A rejected key is not stored."""
        if not valid_secret(secret):
            raise BrainError('auth', '키 형식이 올바르지 않습니다. 공백 없이 키 전체를 붙여 넣어 주십시오.')
        self.guard.check_running()
        listing = self._list(secret)                    # raises BrainError('auth', ...) on 401/403
        try:
            self.credentials.set(self.connection_ref, secret)
        except CredentialError as exc:
            raise BrainError('unavailable', str(exc)) from None
        with self._lock:
            self._models, self._models_at, self._verified = listing, self.clock(), set()

    def delete_key(self):
        try:
            removed = self.credentials.delete(self.connection_ref)
        except CredentialError as exc:
            raise BrainError('unavailable', str(exc)) from None
        with self._lock:
            self._models, self._models_at, self._verified = None, float('-inf'), set()
        return removed

    # --- HTTP -----------------------------------------------------------------------------------------------
    def _headers(self, key):
        raise NotImplementedError

    def _fail(self, status, message, *, probing=False):
        detail = summary(message)
        suffix = f' ({detail})' if detail else ''
        if status in (401, 403):
            return BrainError('auth', f'{self.company} 키가 거부되었습니다. 키를 다시 연결해 주십시오.{suffix}')
        if status == 429:
            return BrainError('rate_limited', f'{self.company} 사용 한도에 걸렸습니다. 잠시 뒤 다시 시도해 주십시오.{suffix}')
        if status == 404:
            return BrainError('unavailable', f'{self.company}에서 이 모델을 찾지 못했습니다.{suffix}')
        if probing and status in (400, 422):
            return BrainError('unavailable', f'이 모델은 자비스 답변 형식(JSON 스키마) 또는 스트리밍을 지원하지 않습니다.{suffix}')
        return BrainError('unavailable', f'{self.company} 요청이 실패했습니다(HTTP {status}).{suffix}')

    def _get_json(self, key, path):
        connection = self.connect(self.host, LIST_TIMEOUT)
        try:
            connection.request('GET', path, headers={**self._headers(key), 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            data = response.read(MAX_LIST + 1)
            if len(data) > MAX_LIST:
                raise BrainError('too_large', f'{self.company} 모델 목록이 너무 큽니다.')
            try:
                value = json.loads(data) if data else None
            except ValueError:
                value = None
            if response.status != 200:
                raise self._fail(response.status, error_message(value))
            if not isinstance(value, dict):
                raise BrainError('invalid_response', f'{self.company} 모델 목록 형식이 올바르지 않습니다.')
            return value
        except (OSError, http.client.HTTPException):
            raise BrainError('unavailable', f'{self.company}에 연결하지 못했습니다.') from None
        finally:
            connection.close()

    # --- models ---------------------------------------------------------------------------------------------
    def _list(self, key):
        """[(model_id, label, schema_ok or None, max_tokens or None)] from the models endpoint."""
        raise NotImplementedError

    def _listing(self):
        key = self._key()
        if key is None:
            return None
        with self._lock:
            if self._models is not None and self.clock() - self._models_at < LIST_TTL:
                return self._models
        listing = self._list(key)
        with self._lock:
            self._models, self._models_at = listing, self.clock()
        return listing

    def _info(self, model_id, label=None, schema_ok=None, available=True, reason=None):
        if available and schema_ok is False:
            available, reason = False, '이 모델은 자비스 답변 형식(JSON 스키마)을 지원하지 않습니다.'
        return ModelInfo(self.provider_id, model_id, label or model_id, True, SCHEMA_CAPABILITIES, available, reason)

    def models(self):
        self.guard.check_running()
        listing = self._listing()
        if listing is None:
            return []
        return [self._info(model_id, label, schema_ok) for model_id, label, schema_ok, _ in listing]

    def check(self, model_id):
        """Local checks only (no paid call): id shape, key present, and what the cached listing says."""
        if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
            return self._info(str(model_id)[:80], available=False, reason='모델 ID 형식이 올바르지 않습니다.')
        if not self.connected():
            return self._info(model_id, available=False, reason=f'{self.company} 키가 연결되어 있지 않습니다.')
        try:
            listing = self._listing() or []
        except BrainError:
            listing = []
        for listed_id, label, schema_ok, _ in listing:
            if listed_id == model_id:
                return self._info(model_id, label, schema_ok)
        return self._info(model_id)

    def _max_tokens(self, model_id):
        for listed_id, _, _, limit in (self._models or []):
            if listed_id == model_id and isinstance(limit, int) and limit > 0:
                return min(ANSWER_TOKENS, limit)
        return ANSWER_TOKENS

    def _known_supported(self, model_id) -> bool:
        return False

    def verify(self, model_id):
        """Called after the external confirmation, before the selection is saved: one tiny streamed answer with a
        one-field schema, unless the models endpoint already says structured output works. Cached per model id
        for this process; never run on startup."""
        if model_id in self._verified or self._known_supported(model_id):
            return
        try:
            answer = self._infer(model_id, BrainRequest(PROBE_MESSAGES, PROBE_SCHEMA, self.clock() + 60,
                                                        lambda _: None), probing=True, max_tokens=PROBE_TOKENS)
        except BrainError as exc:
            if exc.code == 'invalid_response':
                raise BrainError('unavailable', '이 모델은 자비스 답변 형식(JSON)을 지키지 못했습니다.') from None
            raise
        if answer.get('ok') is not True:
            raise BrainError('unavailable', '이 모델은 자비스 답변 형식(JSON)을 지키지 못했습니다.')
        self._verified.add(model_id)

    def warm_up(self, model_id):
        """Nothing to load on this PC."""

    # --- answer ---------------------------------------------------------------------------------------------
    def _request(self, model_id, request, max_tokens):
        """(path, body) for one streamed answer."""
        raise NotImplementedError

    def _event(self, data, state):
        """Handle one SSE data object; return new answer text (may be ''). Raise BrainError on provider errors."""
        raise NotImplementedError

    def _finish(self, state):
        """Raise BrainError when the stream ended without a complete answer (refusal, length, safety stop...)."""

    def infer(self, model_id, request: BrainRequest):
        return self._infer(model_id, request)

    def _infer(self, model_id, request, *, probing=False, max_tokens=None):
        if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
            raise BrainError('unavailable', '모델 ID 형식이 올바르지 않습니다.')
        if len(json.dumps(request.messages, ensure_ascii=False).encode('utf-8')) > MAX_INPUT:
            raise BrainError('too_large', f'대화가 너무 길어 {self.company}에 보낼 수 없습니다.')
        key = self._key()
        if key is None:
            raise BrainError('auth', f'{self.company} 키가 연결되어 있지 않습니다. 모델 버튼에서 키를 연결해 주십시오.')
        max_tokens = max_tokens or self._max_tokens(model_id)
        attempts = 2 if self.prompted_json and not probing else 1
        for attempt in range(attempts):
            on_delta = request.on_delta if attempt == 0 else None
            text = self._stream(key, model_id, request, on_delta, max_tokens, probing)
            try:
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError('Answer is not an object.')
                validate(value, request.schema)
                return value
            except ValueError:
                if attempt + 1 == attempts:
                    raise BrainError('invalid_response',
                                     f'{self.company} 응답이 자비스 답변 형식에 맞지 않습니다.') from None
        raise AssertionError('unreachable')

    def _stream(self, key, model_id, request, on_delta, max_tokens, probing):
        path, body = self._request(model_id, request, max_tokens)
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.guard.check_running()
        connection = self.connect(self.host, SOCKET_TIMEOUT)
        lines = queue.Queue()
        try:
            try:
                connection.request('POST', path, payload, {**self._headers(key), 'Content-Type': 'application/json',
                                                           'Accept': 'text/event-stream',
                                                           'Accept-Encoding': 'identity'})
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                raise BrainError('unavailable', f'{self.company}에 연결하지 못했습니다.') from None
            if response.status != 200:
                data = response.read(16384)
                try:
                    message = error_message(json.loads(data))
                except ValueError:
                    message = ''
                raise self._fail(response.status, message, probing=probing)
            threading.Thread(target=self._pump, args=(response, lines), daemon=True).start()
            state, text, shown, total, buffer = {}, '', '', 0, []
            while True:
                self.guard.check_running()
                remaining = request.deadline - self.clock()
                if remaining <= 0:
                    raise BrainError('timeout', f'{self.company} 모델이 제한 시간 안에 답하지 못했습니다.')
                try:
                    line = lines.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if isinstance(line, Exception):
                    raise BrainError('unavailable', f'{self.company} 연결이 끊겼습니다.')
                if line is not None:
                    total += len(line)
                    if total > MAX_OUTPUT:
                        raise BrainError('too_large', f'{self.company} 응답이 너무 깁니다.')
                    line = line.decode('utf-8', errors='replace').rstrip('\r\n')
                if line is None or line == '':                     # end of one SSE event (or of the stream)
                    if buffer:
                        data = '\n'.join(buffer)
                        buffer = []
                        if data.strip() == '[DONE]':
                            state['done'] = True
                        else:
                            try:
                                event = json.loads(data)
                            except ValueError:
                                raise BrainError('invalid_response', f'{self.company} 스트림 형식이 올바르지 않습니다.') from None
                            text += self._event(event, state)
                            if len(text) > MAX_OUTPUT:
                                raise BrainError('too_large', f'{self.company} 응답이 너무 깁니다.')
                            if on_delta is not None:
                                reply = reply_text(text)
                                if reply is not None and len(reply) > len(shown) and reply.startswith(shown):
                                    added, shown = reply[len(shown):], reply
                                    try:
                                        on_delta(added)
                                    except Exception:
                                        pass
                    if line is None:
                        break
                    continue
                if line.startswith('data:'):
                    buffer.append(line[5:].removeprefix(' '))
                # 'event:', 'id:', 'retry:' and ':' comment lines carry nothing we need: the data has the type.
            self._finish(state)
            return text
        finally:
            connection.close()

    @staticmethod
    def _pump(response, lines):
        try:
            for line in iter(lambda: response.readline(MAX_OUTPUT + 1), b''):
                lines.put(line)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            lines.put(exc)
            return
        lines.put(None)


def _merged(messages, roles):
    """System text, and the other turns with consecutive same-role turns joined (roles maps user/assistant)."""
    system = '\n\n'.join(m['content'] for m in messages if m.get('role') == 'system')
    turns = []
    for message in messages:
        role = roles.get(message.get('role'))
        if role is None:
            continue
        if turns and turns[-1][0] == role:
            turns[-1] = (role, turns[-1][1] + '\n\n' + message['content'])
        else:
            turns.append((role, message['content']))
    return system, turns


# --- Anthropic --------------------------------------------------------------------------------------------------
class AnthropicApiProvider(ApiProvider):
    provider_id, label, company, host = 'anthropic-api', 'Claude (API)', 'Anthropic', 'api.anthropic.com'

    def _headers(self, key):
        return {'x-api-key': key, 'anthropic-version': '2023-06-01'}

    def _list(self, key):
        result, after = [], None
        for _ in range(MAX_PAGES):
            query = {'limit': 1000, **({'after_id': after} if after else {})}
            page = self._get_json(key, '/v1/models?' + urlencode(query))
            for item in page.get('data') or []:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    continue
                capabilities = item.get('capabilities')
                schema_ok = None
                if isinstance(capabilities, dict) and isinstance(capabilities.get('structured_outputs'), dict):
                    schema_ok = capabilities['structured_outputs'].get('supported') is True
                label = item.get('display_name') if isinstance(item.get('display_name'), str) else item['id']
                limit = item.get('max_tokens') if isinstance(item.get('max_tokens'), int) else None
                result.append((item['id'], label[:80], schema_ok, limit))
            after = page.get('last_id')
            if page.get('has_more') is not True or not isinstance(after, str):
                break
        return [entry for entry in result if MODEL_ID.fullmatch(entry[0])][:MAX_MODELS]

    def _known_supported(self, model_id) -> bool:
        return any(listed_id == model_id and schema_ok is True for listed_id, _, schema_ok, _ in (self._models or []))

    def _request(self, model_id, request, max_tokens):
        system, turns = _merged(request.messages, {'user': 'user', 'assistant': 'assistant'})
        body = {'model': model_id, 'max_tokens': max_tokens, 'stream': True,
                'messages': [{'role': role, 'content': content} for role, content in turns],
                'output_config': {'format': {'type': 'json_schema', 'schema': sent_schema(request.schema)}}}
        if system:
            body['system'] = system
        return '/v1/messages', body

    def _event(self, data, state):
        kind = data.get('type') if isinstance(data, dict) else None
        if kind == 'error':
            error = data.get('error') or {}
            status = {'authentication_error': 401, 'permission_error': 403, 'rate_limit_error': 429,
                      'not_found_error': 404}.get(error.get('type'), 503)
            raise self._fail(status, error.get('message') or error.get('type') or '')
        if kind == 'content_block_delta':
            delta = data.get('delta') or {}
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                return delta['text']
        elif kind == 'message_delta':
            reason = (data.get('delta') or {}).get('stop_reason')
            if reason:
                state['stop'] = reason
        elif kind == 'message_stop':
            state['done'] = True
        return ''

    def _finish(self, state):
        stop = state.get('stop')
        if stop == 'refusal':
            raise BrainError('unavailable', 'Claude가 이 요청에 답하기를 거절했습니다.')
        if stop == 'max_tokens':
            raise BrainError('too_large', 'Claude 답이 길이 제한에서 잘렸습니다.')
        if not state.get('done'):
            raise BrainError('invalid_response', 'Claude 응답이 끝나지 않았습니다.')


# --- OpenAI-compatible Chat Completions (OpenAI, xAI, DeepSeek) ---------------------------------------------------
NOT_CHAT = re.compile(r'embed|tts|whisper|dall-e|davinci|babbage|moderation|image|audio|realtime|transcribe|sora'
                      r'|search-api|computer-use', re.IGNORECASE)


class ChatCompletionsProvider(ApiProvider):
    system_role = 'system'
    models_path = '/v1/models'
    chat_path = '/v1/chat/completions'
    token_field = 'max_completion_tokens'

    def _headers(self, key):
        return {'Authorization': 'Bearer ' + key}

    def _ids(self, page):
        return [item['id'] for item in page.get('data') or [] if isinstance(item, dict) and isinstance(item.get('id'), str)]

    def _list(self, key):
        ids = sorted({model_id for model_id in self._ids(self._get_json(key, self.models_path))
                      if MODEL_ID.fullmatch(model_id) and not NOT_CHAT.search(model_id)})
        return [(model_id, model_id, None, None) for model_id in ids][:MAX_MODELS]

    def _messages(self, request):
        result = []
        for message in request.messages:
            role = message.get('role')
            if role == 'system':
                result.append({'role': self.system_role, 'content': message['content']})
            elif role in ('user', 'assistant'):
                result.append({'role': role, 'content': message['content']})
        return result

    def _format(self, request):
        return {'type': 'json_schema', 'json_schema': {'name': 'jarvis_answer', 'strict': True,
                                                       'schema': sent_schema(request.schema)}}

    def _request(self, model_id, request, max_tokens):
        return self.chat_path, {'model': model_id, 'messages': self._messages(request), 'stream': True,
                                self.token_field: max_tokens, 'response_format': self._format(request)}

    def _event(self, data, state):
        if not isinstance(data, dict):
            return ''
        if 'error' in data:
            raise self._fail(503, error_message(data))
        text = ''
        for choice in data.get('choices') or []:
            if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            if isinstance(delta.get('refusal'), str) and delta['refusal']:
                state['refusal'] = True
            if isinstance(delta.get('content'), str):
                text += delta['content']
            if choice.get('finish_reason'):
                state['stop'] = choice['finish_reason']
        return text

    def _finish(self, state):
        stop = state.get('stop')
        if state.get('refusal') or stop == 'content_filter':
            raise BrainError('unavailable', f'{self.company} 모델이 이 요청에 답하기를 거절했습니다.')
        if stop == 'length':
            raise BrainError('too_large', f'{self.company} 답이 길이 제한에서 잘렸습니다.')
        if stop not in (None, 'stop'):
            raise BrainError('unavailable', f'{self.company} 모델이 답을 끝내지 못했습니다({summary(stop)}).')
        if not state.get('done') and stop is None:
            raise BrainError('invalid_response', f'{self.company} 응답이 끝나지 않았습니다.')


class OpenAIApiProvider(ChatCompletionsProvider):
    provider_id, label, company, host = 'openai-api', 'GPT (API)', 'OpenAI', 'api.openai.com'
    system_role = 'developer'          # "With o1 models and newer, developer messages replace system messages"


class XaiApiProvider(ChatCompletionsProvider):
    provider_id, label, company, host = 'xai-api', 'Grok (API)', 'xAI', 'api.x.ai'
    models_path = '/v1/language-models'

    def _ids(self, page):
        return [item['id'] for item in page.get('models') or [] if isinstance(item, dict)
                and isinstance(item.get('id'), str) and 'text' in (item.get('output_modalities') or ['text'])]


class DeepSeekApiProvider(ChatCompletionsProvider):
    provider_id, label, company, host = 'deepseek-api', 'DeepSeek (API)', 'DeepSeek', 'api.deepseek.com'
    models_path, chat_path, token_field = '/models', '/chat/completions', 'max_tokens'
    prompted_json = True

    def _messages(self, request):
        rule = ('Reply with exactly one JSON object and nothing else. It must match this JSON Schema, including '
                'maxLength, maxItems, enum and required keys:\n' + json.dumps(request.schema, ensure_ascii=False))
        return [{'role': 'system', 'content': rule}, *super()._messages(request)]

    def _format(self, request):
        return {'type': 'json_object'}


# --- Google Gemini --------------------------------------------------------------------------------------------
class GeminiApiProvider(ApiProvider):
    provider_id, label, company, host = 'gemini-api', 'Gemini (API)', 'Google', 'generativelanguage.googleapis.com'

    def _headers(self, key):
        return {'x-goog-api-key': key}

    def _list(self, key):
        result, token = [], None
        for _ in range(MAX_PAGES):
            query = {'pageSize': 1000, **({'pageToken': token} if token else {})}
            page = self._get_json(key, '/v1beta/models?' + urlencode(query))
            for item in page.get('models') or []:
                if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                    continue
                methods = item.get('supportedGenerationMethods') or []
                if 'generateContent' not in methods:
                    continue
                model_id = item['name'].removeprefix('models/')
                label = item.get('displayName') if isinstance(item.get('displayName'), str) else model_id
                limit = item.get('outputTokenLimit') if isinstance(item.get('outputTokenLimit'), int) else None
                result.append((model_id, label[:80], None, limit))
            token = page.get('nextPageToken')
            if not isinstance(token, str) or not token:
                break
        return [entry for entry in result if MODEL_ID.fullmatch(entry[0])][:MAX_MODELS]

    def _request(self, model_id, request, max_tokens):
        system, turns = _merged(request.messages, {'user': 'user', 'assistant': 'model'})
        body = {'contents': [{'role': role, 'parts': [{'text': content}]} for role, content in turns],
                'generationConfig': {'maxOutputTokens': max_tokens, 'responseMimeType': 'application/json',
                                     'responseJsonSchema': sent_schema(request.schema)}}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
        return f'/v1beta/models/{quote(model_id, safe="")}:streamGenerateContent?alt=sse', body

    def _event(self, data, state):
        if not isinstance(data, dict):
            return ''
        if 'error' in data:
            raise self._fail(503, error_message(data))
        feedback = data.get('promptFeedback') or {}
        if feedback.get('blockReason'):
            state['stop'] = 'BLOCKED:' + str(feedback['blockReason'])
        text = ''
        for candidate in (data.get('candidates') or [])[:1]:
            if not isinstance(candidate, dict):
                continue
            for part in ((candidate.get('content') or {}).get('parts') or []):
                if isinstance(part, dict) and not part.get('thought') and isinstance(part.get('text'), str):
                    text += part['text']
            if candidate.get('finishReason'):
                state['stop'] = candidate['finishReason']
        return text

    def _finish(self, state):
        stop = state.get('stop')
        if stop == 'STOP':
            return
        if stop == 'MAX_TOKENS':
            raise BrainError('too_large', 'Gemini 답이 길이 제한에서 잘렸습니다.')
        if stop is None:
            raise BrainError('invalid_response', 'Gemini 응답이 끝나지 않았습니다.')
        raise BrainError('unavailable', f'Gemini가 답을 멈췄습니다({summary(stop)}).')


API_PROVIDERS = (AnthropicApiProvider, OpenAIApiProvider, GeminiApiProvider, XaiApiProvider, DeepSeekApiProvider)


def api_providers(guard, credentials, **options):
    return [cls(guard, credentials=credentials, **options) for cls in API_PROVIDERS]
