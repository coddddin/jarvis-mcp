"""Brain P1: provider contract, saved selection, per-request model, external data allowlist and the button API."""
import json
import threading
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from jarvis.agents import brain
from jarvis.agents.brain import (BrainError, BrainSettings, Capabilities, ConfirmRequired, ModelInfo, OllamaProvider,
                                 Selection)
from jarvis.agents.store import TaskStore
from jarvis.app.chat import ChatService
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails

ORIGIN = 'http://127.0.0.1:8787'
TOKEN = 'x' * 43
AUTH = {'Authorization': 'Bearer ' + TOKEN}
POST = {**AUTH, 'Origin': ORIGIN, 'Content-Type': 'application/json'}


def decision(reply='', tool='none', argument='', basis='general', evidence=()):
    return {'reply': reply, 'tool': tool, 'argument': argument, 'basis': basis, 'evidence_ids': list(evidence)}


class External:
    """A stand-in for an external provider (P2's claude-cli): records every request it receives."""
    provider_id, label, external = 'claude-cli', 'Claude (구독)', True

    def __init__(self, answers=None, fail=None, gate=None):
        self.answers, self.fail, self.gate = list(answers or []), fail, gate
        self.requests, self.started = [], threading.Event()

    def _info(self, model_id):
        ok = model_id == 'sonnet'
        return ModelInfo(self.provider_id, model_id, 'Claude', True, Capabilities(True, True, False), ok,
                         None if ok else '로그인되어 있지 않습니다.')

    def models(self):
        return [self._info('sonnet')]

    def check(self, model_id):
        return self._info(model_id)

    def infer(self, model_id, request):
        self.requests.append(request)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(3)
        if self.fail is not None:
            raise self.fail
        return self.answers.pop(0) if self.answers else decision('외부 모델 답변입니다.')

    def warm_up(self, model_id):
        pass


def ollama_fetch(installed=('qwen3.5:9b',), shows=None):
    shows = shows or {}

    def fetch(path, body=None):
        if path == '/api/tags':
            return {'models': [{'name': name} for name in installed]}
        return shows.get(body['model'], {'capabilities': ['completion'], 'details': {'format': 'gguf'}})
    return fetch


def make_service(tmp_path, monkeypatch, external=None, select_external=True):
    external = external or External()
    service = ChatService(Guardrails(tmp_path), brains=[external])
    service.local.fetch = ollama_fetch()
    local = []

    def infer(messages, schema, deadline, on_delta=None):
        local.append(messages)
        if 'mode' in schema.get('properties', {}):
            return {'mode': 'chat', 'evidence': 'general', 'web_action': 'none', 'unsupported': False}
        return decision('로컬 모델 답변입니다.')
    monkeypatch.setattr(service, '_infer', infer)
    if select_external:
        service.brain.select('claude-cli', 'sonnet', confirm_external=True)
    return service, external, local


def text_of(messages):
    return json.dumps(messages, ensure_ascii=False)


# --- contract and allowlist ---------------------------------------------------------------------------------------

def test_external_tools_are_an_allowlist_and_unknown_tools_stay_local():
    assert set(brain.EXTERNAL_TOOLS) >= {'search_public_web', 'get_system_status', 'search_obsidian_vault'}
    assert 'collect_web_information' not in brain.EXTERNAL_TOOLS       # saves a report file by default
    assert brain.external_allowed('collect_web_information')            # chat calls it with save_report=False
    for name in ('search_local_documents', 'analyze_program_static', 'get_daily_briefing', 'search_gmail',
                 'list_calendar_events', 'a_tool_added_later', None):
        assert not brain.external_allowed(name)
    assert {family for family, _ in brain.EXTERNAL_SENDS} == {'conversation', 'public_web', 'system_status',
                                                               'approved_notes'}


def test_consent_payload_comes_from_the_same_constants():
    payload = brain.consent_payload(External().check('sonnet'))
    assert payload['sends'] == [label for _, label in brain.EXTERNAL_SENDS]
    assert payload['never_sends'] == list(brain.NEVER_SENT)


def test_brain_error_codes_are_fixed():
    assert BrainError('rate_limited', '사용 한도에 도달했습니다.').code == 'rate_limited'
    with pytest.raises(ValueError):
        BrainError('anything', 'x')


@pytest.mark.parametrize('show, available', [
    ({'capabilities': ['completion', 'tools'], 'details': {'format': 'gguf'}}, True),
    ({'capabilities': ['embedding'], 'details': {'format': 'gguf'}}, False),
    ({'capabilities': ['completion'], 'details': {'format': 'gguf'}, 'remote_host': 'https://example.com'}, False),
    ({'details': {'format': 'gguf'}}, False),
])
def test_ollama_models_must_be_local_chat_models(tmp_path, show, available):
    provider = OllamaProvider(Guardrails(tmp_path), fetch=ollama_fetch(('m:1',), {'m:1': show}))
    assert provider.check('m:1').available is available
    assert provider.check('m:2').available is False                     # not installed: never downloaded
    assert provider.check('x:cloud').available is False


def test_ollama_down_lists_nothing_and_refuses_selection(tmp_path):
    def down(path, body=None):
        raise ConnectionRefusedError()
    provider = OllamaProvider(Guardrails(tmp_path), fetch=down)
    assert provider.models() == []
    assert provider.check('qwen3.5:9b').available is False


# --- selection ---------------------------------------------------------------------------------------------------

def test_selection_is_saved_without_secrets_and_a_bad_file_means_the_default(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    service.local.fetch = ollama_fetch(('qwen3.5:9b', 'gemma3:4b'))
    service.brain.select('ollama', 'gemma3:4b')
    path = tmp_path / '.jarvis-settings' / 'brain.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved == {'version': 1, 'external_confirmed': [],
                     'selection': {'provider_id': 'ollama', 'model_id': 'gemma3:4b', 'connection_ref': None}}
    assert service.brain.current().selection == Selection('ollama', 'gemma3:4b')
    path.write_text('{"version": 1, "selection": {"provider_id": "ollama"}}', encoding='utf-8')
    assert service.brain.current().selection == service.brain.default
    path.write_text('not json', encoding='utf-8')
    assert service.brain.current().selection == service.brain.default


def test_a_failed_switch_keeps_the_old_selection(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch)
    with pytest.raises(BrainError, match='로그인'):
        service.brain.select('claude-cli', 'opus')
    with pytest.raises(BrainError, match='설치되지 않은'):
        service.brain.select('ollama', 'missing:1b')
    with pytest.raises(BrainError):
        service.brain.select('nobody', 'x')
    assert service.brain.current().selection == Selection('claude-cli', 'sonnet')


def test_first_external_switch_needs_confirmation_once(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    with pytest.raises(ConfirmRequired):
        service.brain.select('claude-cli', 'sonnet')
    assert service.brain.current().external is False
    service.brain.select('claude-cli', 'sonnet', confirm_external=True)
    service.brain.select('ollama', 'qwen3.5:9b')
    assert service.brain.select('claude-cli', 'sonnet').external is True     # confirmed before, no second prompt


def test_an_unconfirmed_external_selection_in_the_file_is_ignored(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    BrainSettings(service.guard).save(Selection('claude-cli', 'sonnet'), [])
    assert service.brain.current().external is False


# --- one request, one model --------------------------------------------------------------------------------------

def test_external_answer_is_recorded_and_classification_stays_local(tmp_path, monkeypatch):
    service, external, local = make_service(tmp_path, monkeypatch)
    identity = service.store.create()['id']
    turn = service.send(identity, uuid4().hex, '파이썬의 장점이 뭐야?', mode='auto')['turns'][-1]
    assert turn['assistant'] == '외부 모델 답변입니다.'
    assert turn['model'] == {'provider_id': 'claude-cli', 'model_id': 'sonnet', 'label': 'Claude (구독) · sonnet',
                             'external': True}
    assert len(local) == 1 and 'Classify this request' in local[0][0]['content']     # the plan only
    assert len(external.requests) == 1


def test_a_request_in_flight_keeps_its_model(tmp_path, monkeypatch):
    gate = threading.Event()
    service, external, local = make_service(tmp_path, monkeypatch, External(gate=gate))
    identity = service.store.create()['id']
    result = {}
    worker = threading.Thread(target=lambda: result.update(service.send(identity, uuid4().hex, '안녕')))
    worker.start()
    assert external.started.wait(3)
    service.brain.select('ollama', 'qwen3.5:9b')             # the user switches while the answer is running
    gate.set()
    worker.join(5)
    assert result['turns'][-1]['model']['provider_id'] == 'claude-cli'
    later = service.send(identity, uuid4().hex, '다음 질문')['turns'][-1]
    assert later['model']['provider_id'] == 'ollama' and later['assistant'] == '로컬 모델 답변입니다.'


def test_an_external_failure_fails_the_turn_without_any_fallback(tmp_path, monkeypatch):
    service, external, local = make_service(
        tmp_path, monkeypatch, External(fail=BrainError('rate_limited', '구독 사용 한도에 도달했습니다.')))
    identity = service.store.create()['id']
    with pytest.raises(BrainError, match='사용 한도'):
        service.send(identity, uuid4().hex, '안녕')
    turn = service.store.get(identity)['turns'][-1]
    assert turn['status'] == 'failed' and turn['model']['provider_id'] == 'claude-cli'
    assert local == []                                       # no other model was tried


# --- what an external model may see ------------------------------------------------------------------------------

@pytest.mark.parametrize('message, source', [
    ('오늘 받은 메일 요약해줘', '메일'),
    ('내일 일정 알려줘', '일정'),
    ('오늘 브리핑 읽어줘', '브리핑'),
    ('내 문서에서 계약 기간 찾아줘', '로컬 문서'),
    ('projects/app/main.js 정적 분석해줘', '프로그램 분석'),
    ('미승인 노트 내용 알려줘', '승인되지 않은 노트'),
])
def test_each_local_only_source_is_answered_by_the_local_model(tmp_path, monkeypatch, message, source):
    service, external, local = make_service(tmp_path, monkeypatch)
    identity = service.store.create()['id']
    turn = service.send(identity, uuid4().hex, message)['turns'][-1]
    assert external.requests == [] and turn['assistant'] == '로컬 모델 답변입니다.'
    assert turn['model']['provider_id'] == 'ollama'
    assert turn['model']['local_reason'] == brain.LOCAL_REASON.format(source=source)


def test_a_local_evidence_plan_goes_local_but_pc_status_may_go_out(tmp_path, monkeypatch):
    service, external, local = make_service(tmp_path, monkeypatch, External(answers=[
        decision(tool='get_system_status'), decision('지금은 한가합니다.', basis='tool', evidence=['e1'])]))
    plans = iter([{'mode': 'chat', 'evidence': 'local', 'web_action': 'none', 'unsupported': False}] * 2)

    def infer(messages, schema, deadline, on_delta=None):
        local.append(messages)
        if 'mode' in schema['properties']:
            return next(plans)
        if 'none' not in schema['properties']['tool']['enum']:   # a local plan forces a local lookup first
            return decision(tool='get_system_status')
        return decision('로컬 모델 답변입니다.', basis='tool', evidence=['e1'])
    monkeypatch.setattr(service, '_infer', infer)
    service.tools['get_system_status'] = lambda: {'cpu_percent': 3}
    identity = service.store.create()['id']
    turn = service.send(identity, uuid4().hex, '그거 확인해줘', mode='auto')['turns'][-1]
    assert turn['model']['local_reason'] and external.requests == []
    turn = service.send(identity, uuid4().hex, '내 컴퓨터 얼마나 바빠?', mode='auto')['turns'][-1]
    assert turn['model']['provider_id'] == 'claude-cli' and turn['assistant'] == '지금은 한가합니다.'
    assert external.requests[0].schema['properties']['tool']['enum'] == ['get_system_status']


def test_an_external_model_is_offered_only_allowlisted_tools(tmp_path, monkeypatch):
    service, external, _ = make_service(tmp_path, monkeypatch)
    identity = service.store.create()['id']
    service.send(identity, uuid4().hex, '안녕')
    offered = set(external.requests[0].schema['properties']['tool']['enum']) - {'none'}
    assert offered and all(brain.external_allowed(name) for name in offered)
    assert 'search_local_documents' not in offered and 'analyze_program_static' not in offered
    assert 'search_local_documents' not in external.requests[0].messages[0]['content']


def test_local_only_turns_never_reach_an_external_model_through_history(tmp_path, monkeypatch):
    service, external, local = make_service(tmp_path, monkeypatch, select_external=False)
    steps = iter([decision(tool='search_local_documents', argument='연봉'),
                  decision('계약서상 연봉은 5000만 원입니다.', basis='tool', evidence=['e1']),
                  decision('일반 대화 답변입니다.'), decision('내일은 치과 예약이 있습니다.')])
    monkeypatch.setattr(service, '_infer', lambda *args, **kwargs: next(steps))
    service.tools['search_local_documents'] = lambda **kwargs: {'results': [{'excerpt': '연봉 5000만 원'}]}
    identity = service.store.create()['id']
    service.send(identity, uuid4().hex, '파일에서 연봉 찾아줘')          # answered locally with a local document
    service.send(identity, uuid4().hex, '좋은 하루 보내')                # an ordinary local turn
    service.brain.select('claude-cli', 'sonnet', confirm_external=True)
    service.send(identity, uuid4().hex, '내일 내 일정 알려줘')            # external selected, answered locally
    monkeypatch.setattr(service, '_infer', lambda *args, **kwargs: decision('로컬 모델 답변입니다.'))
    service.send(identity, uuid4().hex, '아까 그 금액 다시 알려줘')
    sent = text_of(external.requests[-1].messages)
    for secret in ('5000', '연봉', '파일에서', '일정', '치과'):
        assert secret not in sent
    assert ChatService.OMITTED_ASSISTANT in sent and sent.count(ChatService.OMITTED_USER) == 2
    assert '좋은 하루 보내' in sent and '일반 대화 답변입니다.' in sent     # allowed conversation still goes


def test_approved_note_answers_may_use_the_external_model(tmp_path, monkeypatch):
    service, external, _ = make_service(tmp_path, monkeypatch, External(answers=[
        decision('노트에 따르면 출시는 금요일입니다.', basis='tool', evidence=['n1'])]))
    hit = {'results': [{'score': 0.5, 'line': 1, 'excerpt': '출시는 금요일', 'path': 'a.md', 'source_sha256': '0' * 64,
                        'metadata': {'id': 'a', 'title': '출시'}}], 'stale_documents': []}
    monkeypatch.setattr(service.notes, 'search', lambda query, limit: hit)
    identity = service.store.create()['id']
    turn = service.send(identity, uuid4().hex, '내 노트에서 출시일 찾아줘')['turns'][-1]
    assert turn['model']['provider_id'] == 'claude-cli' and turn['sources'][0]['cited'] is True


# --- HTTP API for the button -------------------------------------------------------------------------------------

def client(service):
    app = create_event_app(TaskStore(service.guard), token=TOKEN, task_ids=[], origins=[ORIGIN], chat=service)
    return TestClient(app, base_url=ORIGIN)


def test_button_api_lists_selects_and_asks_before_the_first_external_switch(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    http = client(service)
    listing = http.get('/api/brain', headers=AUTH).json()
    assert listing['selection']['model_id'] == 'qwen3.5:9b' and listing['scope'] == ['chat', 'voice']
    assert [item['provider_id'] for item in listing['providers']] == ['ollama', 'claude-cli']
    body = {'provider_id': 'claude-cli', 'model_id': 'sonnet'}
    asked = http.post('/api/brain/selection', headers=POST, json=body)
    assert asked.status_code == 409 and asked.json()['error'] == 'confirm_required'
    assert asked.json()['never_sends'] == list(brain.NEVER_SENT)
    assert http.get('/api/brain', headers=AUTH).json()['selection']['external'] is False
    done = http.post('/api/brain/selection', headers=POST, json={**body, 'confirm_external': True})
    assert done.status_code == 200 and done.json()['selection']['external'] is True
    refused = http.post('/api/brain/selection', headers=POST, json={'provider_id': 'ollama', 'model_id': 'none:1'})
    assert refused.status_code == 400 and refused.json()['error'] == 'model_unavailable' and refused.json()['reason']
    assert http.get('/api/brain', headers=AUTH).json()['selection']['provider_id'] == 'claude-cli'
    assert http.get('/api/chat/conversations', headers=AUTH).json()['brain']['external'] is True


@pytest.mark.parametrize('headers, body, status', [
    ({'Origin': ORIGIN, 'Content-Type': 'application/json'}, {'provider_id': 'ollama', 'model_id': 'qwen3.5:9b'}, 401),
    ({**AUTH, 'Content-Type': 'application/json'}, {'provider_id': 'ollama', 'model_id': 'qwen3.5:9b'}, 403),
    ({**AUTH, 'Origin': 'http://127.0.0.1:9999', 'Content-Type': 'application/json'},
     {'provider_id': 'ollama', 'model_id': 'qwen3.5:9b'}, 403),
    (POST, {'provider_id': 'ollama', 'model_id': 'qwen3.5:9b', 'api_key': 'sk-x'}, 400),
    (POST, {'provider_id': 'ollama'}, 400),
])
def test_button_api_gate(tmp_path, monkeypatch, headers, body, status):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    assert client(service).post('/api/brain/selection', headers=headers, json=body).status_code == status


def test_button_api_without_chat_is_unavailable(tmp_path):
    store = TaskStore(Guardrails(tmp_path))
    from jarvis.agents.models import TaskRequest
    task = store.create(TaskRequest(summary='task'))
    http = TestClient(create_event_app(store, token=TOKEN, task_ids=[task.task_id], origins=[ORIGIN]), base_url=ORIGIN)
    assert http.get('/api/brain', headers=AUTH).status_code == 503


# --- follow-up: no secrets in brain.json, no leftover temporary file ---------------------------------------------

SECRET = 'sk-ant-api03-' + 'x' * 40


def test_a_connection_ref_is_refused_unless_the_provider_uses_one(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    path = tmp_path / '.jarvis-settings' / 'brain.json'
    with pytest.raises(BrainError, match='비밀값'):
        service.brain.select('ollama', 'qwen3.5:9b', SECRET)
    with pytest.raises(BrainError, match='비밀값'):
        service.brain.select('claude-cli', 'sonnet', SECRET, confirm_external=True)
    assert not path.exists()                                            # refused before anything is written
    service.brain.select('ollama', 'qwen3.5:9b')
    before = path.read_text(encoding='utf-8')
    refused = client(service).post('/api/brain/selection', headers=POST,
                                   json={'provider_id': 'ollama', 'model_id': 'qwen3.5:9b', 'connection_ref': SECRET})
    assert refused.status_code == 400 and SECRET not in refused.text
    assert path.read_text(encoding='utf-8') == before and SECRET not in before


def test_a_hand_edited_connection_ref_is_ignored(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    service.local.fetch = ollama_fetch(('qwen3.5:9b', 'gemma3:4b'))
    BrainSettings(service.guard).save(Selection('ollama', 'gemma3:4b', 'something'), [])
    assert service.brain.current().selection == service.brain.default


def test_a_failed_save_leaves_no_temporary_file_and_keeps_the_old_selection(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path, monkeypatch, select_external=False)
    service.local.fetch = ollama_fetch(('qwen3.5:9b', 'gemma3:4b'))
    service.brain.select('ollama', 'qwen3.5:9b')

    def broken(source, target):
        raise PermissionError('file in use')
    monkeypatch.setattr(brain.os, 'replace', broken)
    with pytest.raises(PermissionError):
        service.brain.select('ollama', 'gemma3:4b')
    assert sorted(item.name for item in (tmp_path / '.jarvis-settings').iterdir()) == ['brain.json']
    assert service.brain.current().selection == Selection('ollama', 'qwen3.5:9b')
