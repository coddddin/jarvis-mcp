"""Chat persistence, inference/tool admission and HTTP authentication boundaries."""
import time
import json
import subprocess
import sys
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from jarvis.agents.chat import ChatBusy, ChatError, ChatStore
from jarvis.agents.store import TaskStore
from jarvis.app.chat import ChatDecision, ChatService
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.core.paths import WORKERS_ROOT


@pytest.fixture
def service(tmp_path):
    return ChatService(Guardrails(tmp_path))


def reply(text='안녕하세요. 저는 자비스입니다.'):
    return ChatDecision(reply=text, tool='none', argument='')


def test_persisted_history_and_duplicate_request_do_not_repeat_inference(service, monkeypatch):
    calls = []
    def generate(messages, schema, deadline):
        calls.append(messages)
        return reply('당신의 이름은 해준입니다.' if len(calls) > 1 else '해준님, 기억할게요.')
    monkeypatch.setattr(service, '_worker', generate)
    session = service.store.create()['id']
    request = uuid4().hex
    first = service.send(session, request, '내 이름은 해준이야.')
    assert first['turns'][0]['status'] == 'completed'
    assert service.send(session, request, '내 이름은 해준이야.') == first
    assert len(calls) == 1
    service.send(session, uuid4().hex, '내 이름이 뭐야?')
    assert any(m['content'] == '내 이름은 해준이야.' for m in calls[1])
    restored = ChatStore(service.guard).get(session)
    assert restored['turns'][1]['assistant'] == '당신의 이름은 해준입니다.'
    assert restored['reserved'] == 2 * 9216
    assert service.store.get(service.store.create()['id'])['turns'] == []
    with pytest.raises(ChatError):
        service.send(session, request, 'Different request')


def test_tool_result_is_visible_data_and_guarded(service, monkeypatch):
    calls = []
    def generate(messages, schema, deadline):
        calls.append(messages[:])
        return ChatDecision(reply='', tool='get_system_status', argument='') if len(calls) == 1 else reply('PC 상태를 확인했습니다.')
    monkeypatch.setattr(service, '_worker', generate)
    result = service.send(service.store.create()['id'], uuid4().hex, '내 PC 상태 확인해줘')
    trace = result['turns'][0]['tools'][0]
    assert trace['name'] == 'get_system_status' and trace['status'] == 'ok'
    assert 'cpu_percent' in trace['detail']
    assert calls[1][-1]['role'] == 'user' and 'UNTRUSTED TOOL RESULT' in calls[1][-1]['content']
    assert service.guard._calls == 1
    assert not {'run_safe_shell_command', 'take_screenshot', 'get_daily_briefing', 'apply_file_organization'} & service.tools.keys()


def test_guard_stop_and_tool_budget_cannot_be_bypassed(service):
    decision = ChatDecision(reply='', tool='get_system_status', argument='')
    service.guard._calls = service.guard.MAX_CALLS
    with pytest.raises(Exception):
        service._execute(decision, 'PC 상태')
    (service.guard.workspace / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        service.store.create()


def test_rea_requires_current_explicit_path_and_never_runs_arbitrary_tool(service):
    seen = []
    service.tools['analyze_program_static'] = lambda **kwargs: seen.append(kwargs) or {'ok': True}
    decision = ChatDecision(reply='', tool='analyze_program_static', argument='projects/demo/input.js')
    with pytest.raises(ChatError):
        service._execute(decision, '예전 파일 분석해줘')
    assert not seen
    service._execute(decision, 'projects/demo/input.js 분석해줘')
    assert seen == [{'path': 'projects/demo/input.js'}]
    with pytest.raises(ValueError):
        ChatDecision(reply='', tool='run_safe_shell_command', argument='rm anything')


def test_inference_failure_is_persisted_and_never_reported_as_answer(service, monkeypatch):
    session, request = service.store.create()['id'], uuid4().hex
    def fail(*args):
        raise RuntimeError('Private failure data')
    monkeypatch.setattr(service, '_worker', fail)
    with pytest.raises(ChatError, match='로컬 모델'):
        service.send(session, request, '안녕')
    turn = service.store.get(session)['turns'][0]
    assert turn['status'] == 'failed' and turn['assistant'] == ''
    assert service.send(session, request, '안녕')['turns'][0]['status'] == 'failed'


def test_tool_loop_has_finite_rounds_and_final_schema_disables_tools(service, monkeypatch):
    schemas = []
    def looping(messages, schema, deadline):
        schemas.append(schema)
        return ChatDecision(reply='', tool='list_web_sources', argument='')
    monkeypatch.setattr(service, '_worker', looping)
    session = service.store.create()['id']
    with pytest.raises(ChatError, match='round'):
        service.send(session, uuid4().hex, '출처 확인')
    assert len(schemas) == 3 and schemas[-1]['properties']['tool']['enum'] == ['none']
    assert len(service.store.get(session)['turns'][0]['tools']) == 2


def test_unsupported_live_facts_require_matching_successful_retrieval(service, monkeypatch):
    monkeypatch.setattr(service, '_worker', lambda *args: reply('1위: 검색어1, 2위: 검색어2'))
    result = service.send(service.store.create()['id'], uuid4().hex, '네이버 실시간 순위 top5 알려줘')
    assert '검색어1' not in result['turns'][0]['assistant']
    assert '확인하지 못했어요' in result['turns'][0]['assistant']
    sources = [{'name': 'list_web_sources', 'status': 'ok', 'detail': 'list only'}]
    assert '확인하지 못했어요' in service._ground_reply('오늘 뉴스', 'unverified', sources)
    actual = [{'name': 'read_registered_feed_with_agent_reach', 'status': 'ok', 'detail': 'https://www.bbc.co.uk/news'}]
    assert service._ground_reply('오늘 BBC 뉴스', 'verified feed summary', actual) == 'verified feed summary'
    assert '확인하지 못했어요' in service._ground_reply('네이버 실시간 순위', 'unverified', actual)


def test_incomplete_capability_introduction_uses_actual_enabled_tool_list(service):
    response = service._ground_reply('무엇을 할 수 있어?', '다음 기능을 할 수 있습니다:', [])
    assert 'PC 상태 확인' in response and '공개 피드 읽기' in response
    service.tools.pop('analyze_program_static')
    assert '정적 분석' not in service._ground_reply('기능 알려줘', '기능:', [])


def test_secrets_limits_pending_and_request_conflicts(service):
    session = service.store.create()['id']
    for message in ['', 'x' * 2001, 'api_key=sk-private', 'Hello\x1bWorld']:
        with pytest.raises(ChatError):
            service.store.begin(session, uuid4().hex, message, time.time() + 30)
    request = uuid4().hex
    assert service.store.begin(session, request, '안녕', time.time() + 30)
    with pytest.raises(ChatBusy):
        service.store.begin(session, uuid4().hex, '동시 요청', time.time() + 30)
    with service.store.connection() as db:
        db.execute('UPDATE turns SET deadline=0 WHERE id=?', (request,))
    assert service.store.get(session)['turns'][0]['status'] == 'interrupted'
    assert not service.store.begin(session, request, '안녕', time.time() + 30)
    service.store.MAX_RESERVATION = 9216
    service.store.reserve(session)
    with pytest.raises(ChatError, match='budget'):
        service.store.reserve(session)


def test_chat_storage_rejects_linked_database_siblings(service):
    database = service.guard.workspace / service.store.DATABASE
    link = service.guard.workspace / 'copy.db'
    link.hardlink_to(database)
    with pytest.raises(GuardrailError):
        service.store.listing()


def test_global_inference_admission_does_not_queue_unbounded_requests(service):
    service.lock.acquire()
    try:
        with pytest.raises(ChatBusy):
            service.send(uuid4().hex, uuid4().hex, '대기')
    finally:
        service.lock.release()


def test_isolated_chat_worker_loads_support_and_refuses_cloud_without_network():
    result = subprocess.run([sys.executable, '-I', str(WORKERS_ROOT / 'chat_step.py')],
                            input=json.dumps({'model': 'example:cloud'}), text=True,
                            capture_output=True, timeout=5)
    assert result.returncode == 1 and result.stderr == ''
    assert json.loads(result.stdout) == {'ok': False, 'error': 'ValueError'}


@pytest.fixture
def web(service, monkeypatch):
    monkeypatch.setattr(service, '_worker', lambda *args: reply())
    app = create_event_app(TaskStore(service.guard), token='x' * 43, task_ids=[],
                           origins=['http://127.0.0.1:8787'], chat=service)
    with TestClient(app, base_url='http://127.0.0.1:8787') as client:
        yield service, client, {'Authorization': 'Bearer ' + 'x' * 43, 'Origin': 'http://127.0.0.1:8787'}


def test_chat_auth_origin_host_and_bounded_http_body(web):
    _, client, headers = web
    path = '/api/chat/conversations'
    assert client.get(path).status_code == 401
    assert client.post(path, headers={'Authorization': headers['Authorization']}, json={}).status_code == 403
    assert client.post(path, headers={**headers, 'Origin': 'https://evil.example'}, json={}).status_code == 403
    assert client.get(path, headers={**headers, 'Host': 'evil.example'}).status_code == 403
    assert client.get(path + '?token=secret', headers=headers).status_code == 403
    assert client.post(path, headers=headers, content='x').status_code == 415
    assert client.post(path, headers={**headers, 'Content-Type': 'application/json'}, content='x' * 16385).status_code == 413
    assert client.post(path, headers=headers, json=[]).status_code == 400
    assert client.post(path, headers=headers, json={'model': 'remote'}).status_code == 400


def test_authenticated_roundtrip_assets_and_empty_task_hud(web):
    _, client, headers = web
    assert client.get('/assets/chat.js').status_code == 200
    assert client.get('/api/tasks', headers=headers).json()['tasks'] == []
    created = client.post('/api/chat/conversations', headers=headers, json={})
    assert created.status_code == 201
    identity = created.json()['id']
    path = f'/api/chat/conversations/{identity}/messages'
    result = client.post(path, headers=headers, json={'request_id': uuid4().hex, 'message': '안녕, 자비스'})
    assert result.status_code == 200
    assert result.json()['turns'][0]['assistant'] == '안녕하세요. 저는 자비스입니다.'
    assert result.headers['cache-control'] == 'no-store'
    assert client.get('/api/chat/conversations/' + identity, headers=headers).json()['turns']
    assert client.get('/api/chat/conversations/' + 'z' * 32, headers=headers).status_code == 400
    assert client.get('/.jarvis-chat/conversations.sqlite3', headers=headers).status_code == 404


def test_declared_live_answer_requires_successful_current_evidence(service):
    decision = ChatDecision(reply='retrieved fact', tool='none', argument='', basis='live', evidence_ids=['e1'])
    assert service._verified_reply('news please', decision, []) != decision.reply
    trace = {'name': 'read_registered_feed_with_agent_reach', 'status': 'ok', 'detail': 'article', 'evidence_id': 'e1'}
    assert service._verified_reply('news please', decision, [trace]) == decision.reply
    assert service._verified_reply('news please', decision, [{**trace, 'status': 'error'}]) != decision.reply
    assert service._verified_reply('news please', decision, [{**trace, 'name': 'list_web_sources'}]) != decision.reply


def test_placeholder_answer_is_rejected_without_keyword_trigger(service):
    decision = ChatDecision(reply='[search term1]', tool='none', argument='')
    assert service._verified_reply('show me five', decision, []) != decision.reply


def test_tool_trace_has_server_timestamp_and_evidence_identity(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='get_system_status', argument=''),
                      ChatDecision(reply='observed', tool='none', argument='', basis='tool', evidence_ids=['e1'])])
    monkeypatch.setattr(service, '_worker', lambda *args: next(decisions))
    turn = service.send(service.store.create()['id'], uuid4().hex, 'status')['turns'][0]
    assert turn['assistant'] == 'observed'
    assert turn['tools'][0]['evidence_id'] == 'e1'
    assert turn['tools'][0]['retrieved_at'].endswith('+00:00')


def test_registered_collection_never_saves_and_failures_are_not_evidence(service):
    seen = []
    service.tools['collect_web_information'] = lambda **kwargs: seen.append(kwargs) or {'status': 'ok', 'sources': []}
    decision = ChatDecision(reply='', tool='collect_web_information', argument='seoul-weather')
    service._execute(decision, 'weather')
    assert seen == [{'source_ids': ['seoul-weather'], 'max_items': 3, 'save_report': False}]
    service.tools['collect_web_information'] = lambda **kwargs: {'status': 'error'}
    with pytest.raises(ChatError, match='retrieval'):
        service._execute(decision, 'weather')


def test_failed_tool_cannot_be_followed_by_unsupported_success(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='search_local_documents', argument='missing'),
                      reply('I found it successfully')])
    monkeypatch.setattr(service, '_worker', lambda *args: next(decisions))
    def fail(**kwargs):
        raise RuntimeError('private diagnostic')
    service.tools['search_local_documents'] = fail
    turn = service.send(service.store.create()['id'], uuid4().hex, 'find my document')['turns'][0]
    assert turn['assistant'] != 'I found it successfully'
    assert turn['tools'][0]['status'] == 'error'
    assert 'private diagnostic' not in json.dumps(turn)


def test_actual_inference_schema_requires_evidence_fields(service, monkeypatch):
    schemas = []
    def infer(messages, schema, deadline):
        schemas.append(schema)
        return reply()
    monkeypatch.setattr(service, '_worker', infer)
    service.send(service.store.create()['id'], uuid4().hex, 'hello')
    assert {'basis', 'evidence_ids'} <= set(schemas[0]['required'])


def test_real_worker_rejects_omitted_evidence_metadata(service, monkeypatch):
    monkeypatch.setattr(service, '_infer', lambda *args: {'reply': 'invented', 'tool': 'none', 'argument': ''})
    with pytest.raises(ChatError, match='metadata'):
        service._worker([], {}, 0)


@pytest.mark.parametrize('message', ['네이버 인기 키워드 알려줘', '검색 순위 보여줘',
                                     '서울 날씨는 어때?', 'Look up trending topics',
                                     'Search for Python releases'])
def test_request_policy_rejects_general_misclassification(service, message):
    decision = reply('unobserved facts')
    assert service._verified_reply(message, decision, []) != decision.reply
    trace = {'name': 'get_system_status', 'status': 'ok', 'evidence_id': 'e1'}
    decision.evidence_ids = ['e1']
    assert service._verified_reply(message, decision, [trace]) != decision.reply


def test_source_listing_then_retrieval_preserves_current_evidence(service, monkeypatch):
    decisions = iter([
        ChatDecision(reply='', tool='list_web_sources', argument=''),
        ChatDecision(reply='', tool='collect_web_information', argument='seoul-weather'),
        ChatDecision(reply='observed weather', tool='none', argument='', basis='general', evidence_ids=['e2']),
    ])
    monkeypatch.setattr(service, '_worker', lambda *args: next(decisions))
    service.tools['collect_web_information'] = lambda **kwargs: {'status': 'ok', 'sources': [{'temperature': 20}]}
    result = service.send(service.store.create()['id'], uuid4().hex, '서울 날씨 알려줘')['turns'][0]
    assert result['assistant'] == 'observed weather'
    assert [trace['evidence_id'] for trace in result['tools']] == ['e1', 'e2']


@pytest.mark.parametrize('message,expected', [('안녕', 'chat'), ('서울 날씨 확인해줘', 'chat'),
    ('합계 함수를 작성해줘', 'team-local'), ('설계하고 구현 제안을 검토해줘', 'team-local')])
def test_automatic_dispatch_is_local_and_replay_safe(service, monkeypatch, message, expected):
    calls = []
    classifications = []
    def classify(*args):
        classifications.append(True)
        return {'mode': expected, 'evidence': 'general', 'web_action': 'none', 'unsupported': False}
    monkeypatch.setattr(service, '_infer', classify)
    monkeypatch.setattr(service, '_worker', lambda *args: reply())
    def run(identity, request_id, text, external):
        calls.append(external)
        assert service.store.get(identity)['turns'][-1]['mode'] == 'team-local'
        return 'proposal only', []
    monkeypatch.setattr(service.team, 'run', run)
    identity, request_id = service.store.create()['id'], uuid4().hex
    first = service.send(identity, request_id, message, mode='auto')
    assert first['turns'][0]['mode'] == expected
    assert service.send(identity, request_id, message, mode='auto') == first
    assert calls == ([False] if expected == 'team-local' else [])
    assert len(classifications) == 1
    assert first['turns'][0]['requested_mode'] == 'auto'


def test_request_plan_prevents_general_answer_from_avoiding_required_evidence(service, monkeypatch):
    monkeypatch.setattr(service, '_infer', lambda *args: {'mode': 'chat', 'evidence': 'external', 'web_action': 'none', 'unsupported': False})
    monkeypatch.setattr(service, '_worker', lambda *args: reply('invented current fact'))
    turn = service.send(service.store.create()['id'], uuid4().hex, '요즘 뜨는 것 다섯 개 알려줘', mode='auto')['turns'][0]
    assert turn['assistant'] != 'invented current fact'
    assert turn['request_plan']['evidence'] == 'external'


def test_invalid_request_plan_fails_closed_without_repeated_classification(service, monkeypatch):
    calls = []
    def bad(*args):
        calls.append(True)
        return {'mode': 'team', 'evidence': 'general', 'web_action': 'none', 'unsupported': False}
    monkeypatch.setattr(service, '_infer', bad)
    identity, request_id = service.store.create()['id'], uuid4().hex
    with pytest.raises(ChatError):
        service.send(identity, request_id, 'hello', mode='auto')
    result = service.send(identity, request_id, 'hello', mode='auto')
    assert result['turns'][0]['status'] == 'failed' and len(calls) == 1


def test_auto_resolution_is_single_use_and_cannot_select_external_team(service):
    identity, request_id = service.store.create()['id'], uuid4().hex
    service.store.begin(identity, request_id, 'hello', time.time()+660, mode='auto')
    with pytest.raises(ChatError, match='local'):
        service.store.resolve_auto(identity, request_id, 'team', {})
    service.store.resolve_auto(identity, request_id, 'chat', {'evidence': 'general'})
    with pytest.raises(ChatError, match='resolved'):
        service.store.resolve_auto(identity, request_id, 'team-local', {})


def test_local_observation_requirement_cannot_use_public_search_evidence(service):
    decision = ChatDecision(reply='invented PC status', tool='none', argument='', evidence_ids=['e1'])
    trace = {'name': 'search_public_web', 'status': 'ok', 'evidence_id': 'e1'}
    assert service._verified_reply('my PC', decision, [trace], required_basis='local') != decision.reply


def test_unsupported_plan_cannot_dispatch_tools_or_external_team(service, monkeypatch):
    monkeypatch.setattr(service, '_infer', lambda *args: {'mode': 'team-local', 'evidence': 'local',
        'web_action': 'none', 'unsupported': True})
    monkeypatch.setattr(service, '_worker', lambda *args: pytest.fail('Unsupported request reached worker'))
    turn = service.send(service.store.create()['id'], uuid4().hex, '폴더를 지워줘', mode='auto')['turns'][0]
    assert turn['mode'] == 'chat' and turn['tools'] == [] and '실행할 수 없어' in turn['assistant']


def test_second_instance_does_not_reclassify_inflight_auto_request(service, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    calls = []
    def classify(*args):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return {'mode': 'chat', 'evidence': 'general', 'web_action': 'none', 'unsupported': False}
    monkeypatch.setattr(service, '_infer', classify)
    monkeypatch.setattr(service, '_worker', lambda *args: reply())
    other = ChatService(service.guard)
    monkeypatch.setattr(other, '_infer', lambda *args: pytest.fail('Duplicate routing inference'))
    identity, request_id = service.store.create()['id'], uuid4().hex
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.send, identity, request_id, '안녕', 'auto')
        try:
            assert entered.wait(5)
            pending = other.send(identity, request_id, '안녕', 'auto')
            assert pending['turns'][0]['mode'] == 'auto'
            assert pending['turns'][0]['status'] == 'pending'
        finally:
            release.set()
        assert future.result()['turns'][0]['status'] == 'completed'
    assert len(calls) == 1


def test_search_answer_carries_server_sources_and_cannot_claim_live_proof(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='search_public_web', argument='Python documentation'),
        ChatDecision(reply='Invented https://evil.example/fake', tool='none', argument='', basis='external', evidence_ids=['e1'])])
    monkeypatch.setattr(service, '_worker', lambda *args: next(decisions))
    service.tools['search_public_web'] = lambda **kwargs: {'items': [
        {'title': 'Python docs', 'url': 'https://docs.python.org/'}]}
    turn = service.send(service.store.create()['id'], uuid4().hex, 'Python 문서 검색해줘')['turns'][0]
    assert 'https://docs.python.org/' in turn['assistant'] and 'UTC' in turn['assistant']
    assert turn['tools'][0]['sources'][0]['title'] == 'Python docs'
    assert 'evil.example' not in turn['assistant']
    decision = ChatDecision(reply='live claim', tool='none', argument='', basis='live', evidence_ids=['e1'])
    assert service._verified_reply('current facts', decision, turn['tools']) != decision.reply


def test_worker_normalizes_no_argument_none_without_accepting_other_values(service, monkeypatch):
    monkeypatch.setattr(service, '_infer', lambda *args: {'reply': '', 'tool': 'list_web_sources',
        'argument': 'none', 'basis': 'general', 'evidence_ids': []})
    assert service._worker([], {}, 0).argument == ''


def test_explicit_web_request_cannot_choose_unrelated_tool(service, monkeypatch):
    monkeypatch.setattr(service, '_worker', lambda *args: ChatDecision(reply='', tool='get_system_status', argument=''))
    with pytest.raises(ChatError, match='outside'):
        service.send(service.store.create()['id'], uuid4().hex, '웹에서 Python 검색해줘')
    assert service.guard._calls == 0


def test_korean_search_query_translation_uses_local_infer_and_reserves_budget(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='search_public_web', argument='파이썬 공식 문서'),
        ChatDecision(reply='summary', tool='none', argument='', basis='external', evidence_ids=['e1'])])
    monkeypatch.setattr(service, '_worker', lambda *args: next(decisions))
    inferred = []
    def infer(messages, schema, deadline):
        inferred.append(messages)
        return {'query': 'Python official documentation'}
    monkeypatch.setattr(service, '_infer', infer)
    queries = []
    def search(**kwargs):
        queries.append(kwargs)
        return {'items': [{'title': 'Python', 'url': 'https://python.org/', 'summary': 'Actual excerpt'}]}
    service.tools['search_public_web'] = search
    result = service.send(service.store.create()['id'], uuid4().hex, '파이썬 문서를 웹에서 검색해줘')
    assert queries == [{'query': 'Python official documentation', 'limit': 3}]
    assert len(inferred) == 1 and inferred[0][-1]['content'] == '파이썬 공식 문서'
    assert result['reserved'] == 3 * 9216
    assert 'Actual excerpt' in result['turns'][0]['assistant']


def test_local_document_request_does_not_authorize_public_search(service):
    called = []
    service.tools['search_public_web'] = lambda **kwargs: called.append(kwargs)
    with pytest.raises(ChatError, match='does not authorize'):
        service._execute(ChatDecision(reply='', tool='search_public_web', argument='private topic'), '내 문서 검색해줘')
    assert not called


def test_page_reader_only_accepts_exact_current_search_results(service):
    called = []
    service.tools['read_public_web_page'] = lambda **kwargs: called.append(kwargs) or {'status': 'ok'}
    decision = ChatDecision(reply='', tool='read_public_web_page', argument='https://example.com/article')
    with pytest.raises(ChatError, match='exactly match'):
        service._execute(decision, 'read page')
    trace = {'name': 'search_public_web', 'status': 'ok', 'sources': [{'url': decision.argument}]}
    service._execute(decision, 'read page', [trace])
    assert called == [{'url': decision.argument}]
    with pytest.raises(ChatError):
        service._execute(decision, 'read page', [{**trace, 'status': 'error'}])


def test_search_then_page_returns_actual_excerpt_not_model_claims(service, monkeypatch):
    decisions = iter([ChatDecision(reply='', tool='search_public_web', argument='Python documentation'),
        ChatDecision(reply='', tool='read_public_web_page', argument='https://python.org/'),
        ChatDecision(reply='invented facts', tool='none', argument='', basis='external', evidence_ids=['e2'])])
    monkeypatch.setattr(service, '_worker', lambda *args: next(decisions))
    service.tools['search_public_web'] = lambda **kwargs: {'items': [{'title': 'Python', 'url': 'https://python.org/'}]}
    service.tools['read_public_web_page'] = lambda **kwargs: {'title': 'Actual page', 'url': kwargs['url'], 'excerpt': 'Observed page text'}
    turn = service.send(service.store.create()['id'], uuid4().hex, '웹에서 Python 검색하고 원문 읽어줘')['turns'][0]
    assert [t['name'] for t in turn['tools']] == ['search_public_web', 'read_public_web_page']
    assert 'Observed page text' in turn['assistant'] and 'invented facts' not in turn['assistant']
    assert '전체 문서' in turn['assistant']
