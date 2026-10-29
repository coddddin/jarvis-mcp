"""Brain P3: company API providers (Anthropic, OpenAI, Gemini, xAI, DeepSeek) with fake HTTPS and a fake key store.
No test talks to a real API."""
import io
import json
import time

import pytest
from starlette.testclient import TestClient

from jarvis.agents import api_brain
from jarvis.agents.api_brain import (AnthropicApiProvider, DeepSeekApiProvider, GeminiApiProvider, OpenAIApiProvider,
                                     XaiApiProvider, api_providers, sent_schema, validate)
from jarvis.agents.brain import BrainError, BrainRequest, ConfirmRequired
from jarvis.agents.store import TaskStore
from jarvis.app.chat import ChatDecision, ChatService
from jarvis.app.events import create_event_app
from jarvis.core.credentials import CredentialError, valid_name, valid_secret
from jarvis.core.guardrails import Guardrails

KEY = 'sk-test-' + 'k' * 40
ORIGIN = 'http://127.0.0.1:8787'
TOKEN = 'x' * 43
AUTH = {'Authorization': 'Bearer ' + TOKEN}
POST = {**AUTH, 'Origin': ORIGIN, 'Content-Type': 'application/json'}


def chat_schema(tools=('none',)):
    schema = ChatDecision.model_json_schema()
    schema['required'] = list(schema.get('required', [])) + ['basis', 'evidence_ids']
    schema['properties']['tool']['enum'] = list(tools)
    return schema


def answer(reply='안녕하세요', **extra):
    return {'reply': reply, 'tool': 'none', 'argument': '', 'basis': 'general', 'evidence_ids': [], **extra}


class Guard:
    def __init__(self):
        self.stopped = False

    def check_running(self):
        if self.stopped:
            from jarvis.core.guardrails import GuardrailError
            raise GuardrailError('stopped')


class Keys:
    """In-memory stand-in for WindowsCredentials."""

    def __init__(self, entries=None, broken=False):
        self.entries, self.broken = dict(entries or {}), broken

    def get(self, name):
        if self.broken:
            raise CredentialError('broken')
        return self.entries.get(name)

    def set(self, name, secret):
        assert valid_name(name) and valid_secret(secret)
        self.entries[name] = secret

    def delete(self, name):
        return self.entries.pop(name, None) is not None


class Response:
    def __init__(self, status, body):
        self.status = status
        self.stream = io.BytesIO(body if isinstance(body, bytes) else body.encode('utf-8'))

    def read(self, size=-1):
        return self.stream.read(size)

    def readline(self, size=-1):
        return self.stream.readline(size)


class Net:
    """Fake HTTPS: routes (method, path prefix) to queued responses and records every request."""

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []

    def __call__(self, host, timeout):
        net = self

        class Connection:
            def request(self, method, path, body=None, headers=None):
                self.method, self.path = method, path
                net.calls.append({'host': host, 'method': method, 'path': path, 'headers': dict(headers or {}),
                                  'body': json.loads(body) if body else None})

            def getresponse(self):
                for (method, prefix), queue in net.routes.items():
                    if method == self.method and self.path.startswith(prefix) and queue:
                        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
                        return Response(status, body)
                raise AssertionError(f'unexpected {self.method} {self.path}')

            def close(self):
                pass
        return Connection()


def sse(*events, named=False):
    out = []
    for event in events:
        if event == '[DONE]':
            out.append('data: [DONE]\n\n')
            continue
        if named:
            out.append(f"event: {event.get('type', 'x')}\n")
        out.append('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n')
    return ''.join(out)


def make(cls, routes, keys=None, **options):
    net = Net(routes)
    keys = keys if keys is not None else Keys({'JARVIS/brain/' + cls.provider_id: KEY})
    return cls(Guard(), credentials=keys, connect=net, **options), net, keys


def request(schema=None, on_delta=None, deadline=None):
    messages = [{'role': 'system', 'content': '규칙'}, {'role': 'user', 'content': '첫 질문'},
                {'role': 'assistant', 'content': '첫 답'}, {'role': 'user', 'content': '둘째'},
                {'role': 'user', 'content': '셋째'}]
    return BrainRequest(messages, schema or chat_schema(), deadline or time.monotonic() + 30, on_delta)


def chunks(text, size=7):
    return [text[i:i + size] for i in range(0, len(text), size)]


# --- schema ---------------------------------------------------------------------------------------------------

def test_sent_schema_keeps_the_common_subset_and_requires_every_field():
    schema = chat_schema(('none', 'search_public_web'))
    schema['properties']['evidence_ids']['items']['enum'] = []
    sent = sent_schema(schema)
    text = json.dumps(sent)
    for word in ('maxLength', 'maxItems', 'minItems', 'pattern', 'title', 'default'):
        assert word not in text
    assert sent['additionalProperties'] is False and sent['required'] == list(schema['properties'])
    assert sent['properties']['tool']['enum'] == ['none', 'search_public_web']
    assert 'enum' not in sent['properties']['evidence_ids']['items']           # empty enum is rejected by some APIs
    assert schema['properties']['reply']['maxLength'] == 6000                  # the original is untouched


def test_validate_checks_what_the_providers_do_not():
    schema = chat_schema(('none',))
    validate(answer(), schema)
    bad = [answer('x' * 6001), answer(tool='search_public_web'), {**answer(), 'extra': 1},
           {k: v for k, v in answer().items() if k != 'basis'}, answer(evidence_ids=['a'] * 9), answer(basis='other'),
           answer(argument=3)]
    for value in bad:
        with pytest.raises(ValueError):
            validate(value, schema)
    schema['properties']['evidence_ids']['items']['enum'] = []
    with pytest.raises(ValueError):
        validate(answer(evidence_ids=['n1']), schema)
    with pytest.raises(ValueError, match='Unsupported'):
        validate('x', {'type': 'string', 'format': 'email'})                  # unknown keywords fail closed


# --- Anthropic ------------------------------------------------------------------------------------------------

def anthropic_models(*items, has_more=False, last_id=None):
    return json.dumps({'data': list(items), 'has_more': has_more, 'last_id': last_id})


def claude_model(model_id, supported=True, max_tokens=64000):
    return {'id': model_id, 'display_name': model_id.upper(), 'max_tokens': max_tokens,
            'capabilities': {'structured_outputs': {'supported': supported}}}


def anthropic_stream(value, stop='end_turn'):
    text = json.dumps(value, ensure_ascii=False)
    events = [{'type': 'message_start', 'message': {}},
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking'}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': '{"x'}},
              {'type': 'ping'}]
    events += [{'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': part}}
               for part in chunks(text)]
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop}}, {'type': 'message_stop'}]
    return sse(*events, named=True)


def test_anthropic_lists_pages_and_marks_models_without_structured_output():
    provider, net, _ = make(AnthropicApiProvider, {
        ('GET', '/v1/models'): [(200, anthropic_models(claude_model('claude-a'), has_more=True, last_id='claude-a')),
                                (200, anthropic_models(claude_model('claude-old', False)))]})
    models = {info.model_id: info for info in provider.models()}
    assert models['claude-a'].available and models['claude-a'].label == 'CLAUDE-A'
    assert not models['claude-old'].available and 'JSON' in models['claude-old'].reason
    assert net.calls[0]['host'] == 'api.anthropic.com'
    assert net.calls[0]['headers']['x-api-key'] == KEY and net.calls[0]['headers']['anthropic-version'] == '2023-06-01'
    assert 'after_id=claude-a' in net.calls[1]['path']
    provider.models()
    assert len(net.calls) == 2                                                  # cached for 10 minutes


def test_anthropic_streams_the_reply_and_validates_the_answer():
    provider, net, _ = make(AnthropicApiProvider, {
        ('GET', '/v1/models'): [(200, anthropic_models(claude_model('claude-a', max_tokens=4096)))],
        ('POST', '/v1/messages'): [(200, anthropic_stream(answer('스트리밍 "답"입니다')))]})
    provider.models()
    drafts = []
    result = provider.infer('claude-a', request(on_delta=drafts.append))
    assert result == answer('스트리밍 "답"입니다') and ''.join(drafts) == '스트리밍 "답"입니다'
    body = net.calls[-1]['body']
    assert body['stream'] is True and body['max_tokens'] == 4096 and body['system'] == '규칙'
    assert body['output_config']['format']['type'] == 'json_schema'
    assert 'maxLength' not in json.dumps(body['output_config'])
    assert [m['role'] for m in body['messages']] == ['user', 'assistant', 'user']
    assert body['messages'][-1]['content'] == '둘째\n\n셋째'


@pytest.mark.parametrize('stream, code', ids=lambda value: value if len(str(value)) < 20 else 'stream', argvalues=[
    (anthropic_stream(answer(), stop='refusal'), 'unavailable'),
    (anthropic_stream(answer(), stop='max_tokens'), 'too_large'),
    (anthropic_stream(answer('x' * 6001)), 'invalid_response'),
    (sse({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}), 'unavailable'),
    (sse({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'slow down'}}), 'rate_limited'),
    ('data: {not json\n\n', 'invalid_response'),
    (sse({'type': 'message_start'}), 'invalid_response'),                      # stream ended without message_stop
])
def test_anthropic_stream_failures_are_clear_errors(stream, code):
    provider, _, _ = make(AnthropicApiProvider, {('POST', '/v1/messages'): [(200, stream)]})
    with pytest.raises(BrainError) as caught:
        provider.infer('claude-a', request())
    assert caught.value.code == code


def test_http_errors_never_echo_the_key():
    body = json.dumps({'error': {'type': 'authentication_error', 'message': f'invalid x-api-key {KEY}'}})
    provider, _, _ = make(AnthropicApiProvider, {('POST', '/v1/messages'): [(401, body)]})
    with pytest.raises(BrainError) as caught:
        provider.infer('claude-a', request())
    assert caught.value.code == 'auth' and KEY not in str(caught.value) and 'sk-test' not in str(caught.value)


def test_no_key_means_no_request():
    provider, net, _ = make(AnthropicApiProvider, {}, keys=Keys())
    assert provider.models() == [] and not provider.connected()
    info = provider.check('claude-a')
    assert not info.available and '키' in info.reason
    with pytest.raises(BrainError) as caught:
        provider.infer('claude-a', request())
    assert caught.value.code == 'auth' and net.calls == []
    broken, _, _ = make(AnthropicApiProvider, {}, keys=Keys(broken=True))
    assert not broken.connected()


def test_model_ids_are_checked_before_anything_is_sent():
    provider, net, _ = make(OpenAIApiProvider, {('GET', '/v1/models'): [(200, json.dumps({'data': []}))]})
    for bad in ('', ' gpt', 'a' * 121, 'gpt\n4', '../x', 'gpt?x=1', None):
        assert not provider.check(bad).available
    with pytest.raises(BrainError):
        provider.infer('gpt?x=1', request())
    assert all(call['method'] == 'GET' for call in net.calls)


def test_deadline_and_stop_marker_end_the_answer():
    provider, _, _ = make(AnthropicApiProvider, {('POST', '/v1/messages'): [(200, anthropic_stream(answer()))]})
    with pytest.raises(BrainError) as caught:
        provider.infer('claude-a', request(deadline=time.monotonic() - 1))
    assert caught.value.code == 'timeout'
    provider.guard.stopped = True
    from jarvis.core.guardrails import GuardrailError
    with pytest.raises(GuardrailError):
        provider.infer('claude-a', request())


def test_output_cap(monkeypatch):
    monkeypatch.setattr(api_brain, 'MAX_OUTPUT', 200)
    provider, _, _ = make(AnthropicApiProvider, {('POST', '/v1/messages'): [(200, anthropic_stream(answer('x' * 500)))]})
    with pytest.raises(BrainError) as caught:
        provider.infer('claude-a', request())
    assert caught.value.code == 'too_large'


# --- OpenAI / xAI / DeepSeek -------------------------------------------------------------------------------------

def openai_stream(value, finish='stop', refusal=None):
    text = json.dumps(value, ensure_ascii=False)
    events = [{'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]}]
    events += [{'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': part}}]}
               for part in chunks(text)]
    if refusal:
        events.append({'choices': [{'index': 0, 'delta': {'refusal': refusal}}]})
    events.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]})
    return sse(*events, '[DONE]')


def test_openai_lists_chat_models_and_sends_strict_json_schema():
    listing = json.dumps({'object': 'list', 'data': [{'id': name} for name in
                                                     ('gpt-5.5', 'text-embedding-3-large', 'whisper-1', 'o4', 'tts-1')]})
    provider, net, _ = make(OpenAIApiProvider, {('GET', '/v1/models'): [(200, listing)],
                                                ('POST', '/v1/chat/completions'): [(200, openai_stream(answer()))]})
    assert [info.model_id for info in provider.models()] == ['gpt-5.5', 'o4']
    drafts = []
    assert provider.infer('gpt-5.5', request(on_delta=drafts.append)) == answer()
    call = net.calls[-1]
    assert call['host'] == 'api.openai.com' and call['headers']['Authorization'] == 'Bearer ' + KEY
    body = call['body']
    assert body['response_format']['type'] == 'json_schema' and body['response_format']['json_schema']['strict'] is True
    assert body['messages'][0]['role'] == 'developer' and body['max_completion_tokens'] == api_brain.ANSWER_TOKENS
    assert ''.join(drafts) == '안녕하세요'


@pytest.mark.parametrize('stream, code', ids=lambda value: value if len(str(value)) < 20 else 'stream', argvalues=[
    (openai_stream(answer(), refusal='I cannot'), 'unavailable'),
    (openai_stream(answer(), finish='length'), 'too_large'),
    (openai_stream(answer(), finish='content_filter'), 'unavailable'),
    (sse({'error': {'message': 'server error'}}), 'unavailable'),
    (sse({'choices': [{'index': 0, 'delta': {'content': '{"reply":'}}]}), 'invalid_response'),
])
def test_openai_stream_failures(stream, code):
    provider, _, _ = make(OpenAIApiProvider, {('POST', '/v1/chat/completions'): [(200, stream)]})
    with pytest.raises(BrainError) as caught:
        provider.infer('gpt-5.5', request())
    assert caught.value.code == code


def test_xai_uses_language_models_and_system_role():
    listing = json.dumps({'models': [{'id': 'grok-4.7', 'output_modalities': ['text']},
                                     {'id': 'grok-imagine', 'output_modalities': ['image']}]})
    provider, net, _ = make(XaiApiProvider, {('GET', '/v1/language-models'): [(200, listing)],
                                             ('POST', '/v1/chat/completions'): [(200, openai_stream(answer()))]})
    assert [info.model_id for info in provider.models()] == ['grok-4.7']
    provider.infer('grok-4.7', request())
    assert net.calls[-1]['host'] == 'api.x.ai' and net.calls[-1]['body']['messages'][0]['role'] == 'system'


def test_deepseek_puts_the_schema_in_the_prompt_and_retries_once():
    bad, good = openai_stream({'reply': 'x'}), openai_stream(answer('두 번째'))
    provider, net, _ = make(DeepSeekApiProvider, {('POST', '/chat/completions'): [(200, bad), (200, good)]})
    drafts = []
    assert provider.infer('deepseek-flash', request(on_delta=drafts.append)) == answer('두 번째')
    posts = [call for call in net.calls if call['method'] == 'POST']
    assert len(posts) == 2 and drafts == ['x']                                  # the retry streams no draft
    body = posts[0]['body']
    assert net.calls[0]['host'] == 'api.deepseek.com' and body['response_format'] == {'type': 'json_object'}
    assert 'JSON' in body['messages'][0]['content'] and '"maxLength": 6000' in body['messages'][0]['content']
    assert body['max_tokens'] == api_brain.ANSWER_TOKENS


def test_deepseek_gives_up_after_one_retry():
    bad = openai_stream({'reply': 'x'})
    provider, net, _ = make(DeepSeekApiProvider, {('POST', '/chat/completions'): [(200, bad)]})
    with pytest.raises(BrainError) as caught:
        provider.infer('deepseek-flash', request())
    assert caught.value.code == 'invalid_response' and len(net.calls) == 2


# --- Gemini ---------------------------------------------------------------------------------------------------

def gemini_stream(value, finish='STOP', block=None):
    text = json.dumps(value, ensure_ascii=False)
    events = [{'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'thinking...', 'thought': True}]}}]}]
    parts = chunks(text)
    for index, part in enumerate(parts):
        candidate = {'content': {'role': 'model', 'parts': [{'text': part}]}}
        if index == len(parts) - 1 and finish:
            candidate['finishReason'] = finish
        events.append({'candidates': [candidate]})
    if block:
        events = [{'promptFeedback': {'blockReason': block}}]
    return sse(*events).replace('\n\n', '\r\n\r\n')


def test_gemini_lists_generate_models_and_streams_json_schema_answers():
    listing = json.dumps({'models': [
        {'name': 'models/gemini-3.8-flash', 'displayName': 'Gemini 3.8 Flash', 'outputTokenLimit': 8192,
         'supportedGenerationMethods': ['generateContent', 'countTokens']},
        {'name': 'models/text-embedding-005', 'supportedGenerationMethods': ['embedContent']}]})
    provider, net, _ = make(GeminiApiProvider, {
        ('GET', '/v1beta/models'): [(200, listing)],
        ('POST', '/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse'): [(200, gemini_stream(answer()))]})
    assert [(i.model_id, i.label) for i in provider.models()] == [('gemini-3.8-flash', 'Gemini 3.8 Flash')]
    drafts = []
    assert provider.infer('gemini-3.8-flash', request(on_delta=drafts.append)) == answer()
    call = net.calls[-1]
    assert call['headers']['x-goog-api-key'] == KEY and 'key=' not in call['path']
    body = call['body']
    assert body['generationConfig']['responseMimeType'] == 'application/json'
    assert body['generationConfig']['maxOutputTokens'] == 8192 and 'responseJsonSchema' in body['generationConfig']
    assert body['systemInstruction'] == {'parts': [{'text': '규칙'}]}
    assert [c['role'] for c in body['contents']] == ['user', 'model', 'user']
    assert ''.join(drafts) == '안녕하세요'


@pytest.mark.parametrize('stream, code', ids=lambda value: value if len(str(value)) < 20 else 'stream', argvalues=[
    (gemini_stream(answer(), finish='SAFETY'), 'unavailable'),
    (gemini_stream(answer(), finish='MAX_TOKENS'), 'too_large'),
    (gemini_stream(answer(), finish=None), 'invalid_response'),
    (gemini_stream(answer(), block='SAFETY'), 'unavailable'),
])
def test_gemini_stops_are_errors(stream, code):
    provider, _, _ = make(GeminiApiProvider, {('POST', '/v1beta/models/g:'): [(200, stream)]})
    with pytest.raises(BrainError) as caught:
        provider.infer('g', request())
    assert caught.value.code == code


# --- key and capability probe -------------------------------------------------------------------------------------

def test_set_key_stores_only_a_key_the_company_accepts():
    provider, _, keys = make(OpenAIApiProvider, {('GET', '/v1/models'): [(401, '{"error":{"message":"bad key"}}')]},
                             keys=Keys())
    with pytest.raises(BrainError) as caught:
        provider.set_key(KEY)
    assert caught.value.code == 'auth' and keys.entries == {}
    for bad in ('short', 'has space ' + 'x' * 20, '키' * 20, 'x' * 600):
        with pytest.raises(BrainError):
            provider.set_key(bad)
    good, _, keys = make(OpenAIApiProvider, {('GET', '/v1/models'): [(200, json.dumps({'data': [{'id': 'gpt-x'}]}))]},
                         keys=Keys())
    good.set_key(KEY)
    assert keys.entries == {'JARVIS/brain/openai-api': KEY} and good.connected()
    assert good.delete_key() is True and keys.entries == {} and not good.connected()


def test_probe_runs_once_after_confirmation_and_is_cached():
    provider, net, _ = make(OpenAIApiProvider, {('POST', '/v1/chat/completions'): [(200, openai_stream({'ok': True}))]})
    provider.verify('gpt-x')
    provider.verify('gpt-x')
    posts = [call for call in net.calls if call['method'] == 'POST']
    assert len(posts) == 1 and posts[0]['body']['max_completion_tokens'] == api_brain.PROBE_TOKENS
    assert '첫 질문' not in json.dumps(posts[0]['body'], ensure_ascii=False)     # no conversation in the probe


def test_probe_rejection_is_a_clear_refusal():
    provider, _, _ = make(OpenAIApiProvider, {
        ('POST', '/v1/chat/completions'): [(400, '{"error":{"message":"response_format json_schema not supported"}}')]})
    with pytest.raises(BrainError) as caught:
        provider.verify('old-model')
    assert caught.value.code == 'unavailable' and 'JSON 스키마' in str(caught.value)
    wrong, _, _ = make(OpenAIApiProvider, {('POST', '/v1/chat/completions'): [(200, openai_stream({'ok': False}))]})
    with pytest.raises(BrainError):
        wrong.verify('m')


def test_anthropic_skips_the_probe_when_the_listing_says_supported():
    provider, net, _ = make(AnthropicApiProvider, {('GET', '/v1/models'): [(200, anthropic_models(claude_model('c')))]})
    provider.models()
    provider.verify('c')
    assert all(call['method'] == 'GET' for call in net.calls)


# --- registry and HTTP routes ----------------------------------------------------------------------------------

def make_service(tmp_path, routes, keys=None):
    net = Net(routes)
    keys = keys if keys is not None else Keys()
    providers = api_providers(Guardrails(tmp_path), keys, connect=net)
    service = ChatService(Guardrails(tmp_path), brains=providers)
    service.local.fetch = lambda path, body=None: ({'models': [{'name': 'qwen3.5:9b'}]} if path == '/api/tags'
                                                   else {'capabilities': ['completion'], 'details': {'format': 'gguf'}})
    return service, net, keys


def test_selection_probes_only_after_confirmation_and_saves_the_connection_name(tmp_path):
    keys = Keys({'JARVIS/brain/openai-api': KEY})
    service, net, _ = make_service(tmp_path, {
        ('GET', '/v1/models'): [(200, json.dumps({'data': [{'id': 'gpt-x'}]}))],
        ('POST', '/v1/chat/completions'): [(200, openai_stream({'ok': True}))]}, keys)
    with pytest.raises(ConfirmRequired):
        service.brain.select('openai-api', 'gpt-x')
    assert not [call for call in net.calls if call['method'] == 'POST']
    with pytest.raises(BrainError):
        service.brain.select('openai-api', 'gpt-x', KEY, confirm_external=True)   # a key pasted as connection_ref
    choice = service.brain.select('openai-api', 'gpt-x', confirm_external=True)
    assert choice.selection.connection_ref == 'JARVIS/brain/openai-api'
    saved = (tmp_path / '.jarvis-settings' / 'brain.json').read_text(encoding='utf-8')
    assert KEY not in saved and 'JARVIS/brain/openai-api' in saved
    assert service.brain.current().selection.model_id == 'gpt-x'


def test_failed_probe_keeps_the_old_selection(tmp_path):
    keys = Keys({'JARVIS/brain/openai-api': KEY})
    service, _, _ = make_service(tmp_path, {('GET', '/v1/models'): [(200, json.dumps({'data': []}))],
                                            ('POST', '/v1/chat/completions'): [(400, '{}')]}, keys)
    with pytest.raises(BrainError):
        service.brain.select('openai-api', 'gpt-x', confirm_external=True)
    assert service.brain.current().selection.provider_id == 'ollama'


def client(service):
    app = create_event_app(TaskStore(service.guard), token=TOKEN, task_ids=[], origins=[ORIGIN], chat=service)
    return TestClient(app, base_url=ORIGIN)


def test_key_routes_store_check_and_delete_without_echoing_the_key(tmp_path):
    service, _, keys = make_service(tmp_path, {('GET', '/v1/models'): [(200, json.dumps({'data': [{'id': 'gpt-x'}]}))]})
    http = client(service)
    listing = http.get('/api/brain', headers=AUTH).json()
    openai = next(item for item in listing['providers'] if item['provider_id'] == 'openai-api')
    assert openai['needs_key'] and not openai['connected'] and openai['custom_model'] and openai['models'] == []
    response = http.post('/api/brain/key', headers=POST, json={'provider_id': 'openai-api', 'key': KEY})
    assert response.status_code == 200 and response.json() == {'provider_id': 'openai-api', 'connected': True}
    assert keys.entries == {'JARVIS/brain/openai-api': KEY}
    text = http.get('/api/brain', headers=AUTH).text
    assert KEY not in text and '"connected":true' in text.replace(' ', '') and 'gpt-x' in text
    assert http.post('/api/brain/key/delete', headers=POST, json={'provider_id': 'openai-api'}).json() == {
        'provider_id': 'openai-api', 'connected': False}
    assert keys.entries == {}


def test_key_route_refusals(tmp_path):
    body = json.dumps({'error': {'message': f'Incorrect API key provided: {KEY}'}})
    service, _, keys = make_service(tmp_path, {('GET', '/v1/models'): [(401, body)]})
    http = client(service)
    response = http.post('/api/brain/key', headers=POST, json={'provider_id': 'openai-api', 'key': KEY})
    assert response.status_code == 400 and response.json()['error'] == 'key_rejected'
    assert KEY not in response.text and keys.entries == {}
    for payload in ({'provider_id': 'ollama', 'key': KEY}, {'provider_id': 'claude-cli', 'key': KEY}):
        assert http.post('/api/brain/key', headers=POST, json=payload).status_code == 400
    for payload in ({'provider_id': 'openai-api'}, {'provider_id': 'openai-api', 'key': KEY, 'x': 1},
                    {'provider_id': 'openai-api', 'key': 5}):
        assert http.post('/api/brain/key', headers=POST, json=payload).json()['error'] == 'invalid_key_request'
    assert http.post('/api/brain/key', headers={**AUTH, 'Content-Type': 'application/json'},
                     json={'provider_id': 'openai-api', 'key': KEY}).status_code == 403      # no Origin
    assert http.post('/api/brain/key', headers={'Origin': ORIGIN, 'Content-Type': 'application/json'},
                     json={'provider_id': 'openai-api', 'key': KEY}).status_code == 401
    assert http.post('/api/brain/key', headers=POST, content=b'{"provider_id":"' + b'x' * 3000 + b'"}').status_code == 413
