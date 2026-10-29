"""Capability descriptors, capability-only answers and typed tool failures through the real ChatService and guarded
tools. Models are stubbed and the HTTPS worker is replaced, so nothing leaves this PC."""
import json
from uuid import uuid4

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from jarvis.agents.chat import ChatError
from jarvis.app import capabilities
from jarvis.app.chat import ChatDecision, ChatService
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.features.web.service import WebBlocked, WebCollector, WebNoResults, WebTimeout, WebUnavailable
import jarvis.features.web.service as web_service

UNRELATED = ('<rss><channel><item><title>Update</title><link>https://example.com/post</link>'
             '<description>Hello</description></item></channel></rss>')
CHANNEL = ('<rss><channel><item><title>Cheolgu YouTube channel</title><link>https://www.youtube.com/@cheolgu</link>'
           '<description>Public channel page</description></item></channel></rss>')
MIXED = '너가 뭘할수있는데 지금 웹서핑에서 유튜브에 철구 검색해줄래'


@pytest.fixture
def service(tmp_path):
    return ChatService(Guardrails(tmp_path))


def decide(tool='none', argument='', reply='', basis='general', evidence=()):
    return ChatDecision(reply=reply, tool=tool, argument=argument, basis=basis, evidence_ids=list(evidence))


def recorder(service, monkeypatch, *decisions):
    calls, queue = [], list(decisions)

    def worker(messages, schema, deadline, on_delta=None):
        calls.append({'messages': [dict(m) for m in messages], 'schema': schema})
        return queue.pop(0)
    monkeypatch.setattr(service, '_worker', worker)
    return calls


def fetching(monkeypatch, result):
    """Replace only the worker launch; search parsing, relevance filter and guarded_tool stay real."""
    seen = []

    def fetch(self, source, registry):
        seen.append(source.url)
        if isinstance(result, BaseException):
            raise result
        return 'application/rss+xml', result
    monkeypatch.setattr(WebCollector, '_fetch', fetch)
    return seen


def prompt_tools(system):
    return json.JSONDecoder().raw_decode(system.split('허용 도구: ', 1)[1])[0]


def test_metadata_projects_registered_tools_without_health_claims_or_io(service, monkeypatch):
    monkeypatch.setattr(WebCollector, '_fetch', lambda *args: pytest.fail('metadata fetched'))
    monkeypatch.setattr(web_service.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('metadata spawned'))
    service.tools.pop('analyze_program_static')
    meta = service.metadata()
    assert meta['tools'] == list(service.tools)                      # the existing list stays as it was
    ids = [item['id'] for item in meta['capabilities']]
    assert set(ids) == set(service.tools) and 'analyze_program_static' not in ids
    assert all(item['health'] == 'not_checked' and item['registered'] is True and item['request_allowed'] is None
               for item in meta['capabilities'])
    json.dumps(meta['capabilities'])
    assert service.guard._calls == 0 and service.guard._light == 0
    # Names without a code-owned descriptor are never projected; request_allowed follows the supplied set.
    projected = capabilities.descriptors(['search_public_web', 'run_safe_shell_command'], allowed=['get_system_status'])
    assert [(item['id'], item['request_allowed']) for item in projected] == [('search_public_web', False)]


@pytest.mark.parametrize('message', ['너 뭘 할 수 있어?', '뭐 할 수 있어', '너가 뭘할수있는데', '무엇을 할 수 있나요?',
    '할 수 있는 게 뭐야?', '어떤 기능이 있어?', '기능 알려줘', '사용 가능한 도구 알려줘', '네 기능이 뭐야?',
    '자비스, 지금 뭘 할 수 있어?', 'What can you do?', 'what tools do you have', 'list your capabilities'])
def test_capability_only_questions_match(message):
    assert capabilities.is_capability_only(message)


@pytest.mark.parametrize('message', [MIXED, '무슨 일 있어?', '주말에 뭐 할 수 있을까?', '내가 뭘 할 수 있어?',
    '이 앱 기능 알려줘', '그 기능이 뭐야?', '유튜브 영상 볼 수 있어?', '웹에서 기능 검색해줘', 'what can you do with python?',
    '안녕'])
def test_ordinary_or_mixed_requests_are_not_capability_only(message):
    assert not capabilities.is_capability_only(message)


@pytest.mark.parametrize('mode', ['chat', 'auto'])
def test_capability_question_is_answered_from_registered_tools_without_model(service, monkeypatch, mode):
    monkeypatch.setattr(service, '_worker', lambda *args: pytest.fail('model called'))
    monkeypatch.setattr(service, '_infer', lambda *args: pytest.fail('classifier called'))
    monkeypatch.setattr(WebCollector, '_fetch', lambda *args: pytest.fail('tool called'))
    service.tools.pop('analyze_program_static')
    identity, request = service.store.create()['id'], uuid4().hex
    first = service.send(identity, request, '너 뭘 할 수 있어?', mode)
    turn = first['turns'][-1]
    assert turn['status'] == 'completed' and turn['mode'] == 'chat' and turn['tools'] == []
    assert '공개 웹 검색' in turn['assistant'] and 'PC 상태 확인' in turn['assistant']
    assert '정적 분석' not in turn['assistant']                       # not registered, not listed
    assert '동영상 재생·시청' in turn['assistant'].split('할 수 없는 일:')[1]
    assert service.send(identity, request, '너 뭘 할 수 있어?', mode) == first   # replay never re-answers
    assert service.guard._calls == 0 and first['reserved'] == 0


def test_mixed_capability_and_search_request_runs_the_search(service, monkeypatch):
    seen = fetching(monkeypatch, CHANNEL)
    recorder(service, monkeypatch, decide('search_public_web', 'Cheolgu YouTube'),
             decide(reply='찾았습니다.', basis='external', evidence=['e1']))
    turn = service.send(service.store.create()['id'], uuid4().hex, MIXED)['turns'][-1]
    assert seen == ['https://www.bing.com/search?format=rss&q=Cheolgu+YouTube']
    assert turn['tools'][0]['status'] == 'ok' and turn['tools'][0]['sources'][0]['url'] == 'https://www.youtube.com/@cheolgu'
    assert 'https://www.youtube.com/@cheolgu' in turn['assistant'] and '지금 연결된 기능' not in turn['assistant']
    assert service.guard._calls == 1


def test_schema_enum_and_prompt_describe_the_same_turn_tools(service, monkeypatch):
    service.tools.pop('collect_web_information')
    calls = recorder(service, monkeypatch, decide(reply='네.'))
    service.send(service.store.create()['id'], uuid4().hex, '오늘 기분 어때?')
    [call] = calls
    offered = set(call['schema']['properties']['tool']['enum']) - {'none'}
    system = call['messages'][0]['content']
    assert offered == set(prompt_tools(system)) == set(service.tools)
    assert '공개 웹 검색' in system and capabilities.BY_ID['collect_web_information'].label not in system


def test_note_tainted_turn_drops_outside_tools_from_both_schema_and_prompt(tmp_path, monkeypatch):
    folder = tmp_path / 'JARVIS-Vault'
    folder.mkdir()
    meta = {'id': 'meeting', 'title': '프로젝트 회의록', 'created': '2026-10-22T12:00:00+00:00',
            'updated': '2026-10-22T12:01:00+00:00', 'source': 'manual', 'tags': ['테스트'],
            'sensitivity': 'private', 'review_status': 'approved'}
    (folder / 'meeting.md').write_text('---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in meta.items())
        + '\n---\n# 프로젝트 회의록\n10월 회의에서 출시 일정을 11월 첫째 주로 정했다.\n', encoding='utf-8')
    service = ChatService(Guardrails(tmp_path))
    service.notes.index()
    calls = recorder(service, monkeypatch, decide(reply='11월 첫째 주입니다.', basis='tool', evidence=['n1']), decide(reply='네.'))
    identity = service.store.create()['id']
    service.send(identity, uuid4().hex, '내 노트에서 프로젝트 회의 내용 찾아줘')
    service.send(identity, uuid4().hex, '고마워')
    offered = set(calls[-1]['schema']['properties']['tool']['enum']) - {'none'}
    system = calls[-1]['messages'][0]['content']
    assert offered == set(prompt_tools(system)) and not offered & capabilities.SENDS_OUT
    assert '공개 웹 검색' not in system.split('할 수 없는 일')[0]


def test_no_results_keeps_typed_cause_and_overrides_a_model_claim_of_absence(service, monkeypatch):
    fetching(monkeypatch, UNRELATED)
    with pytest.raises(ToolError) as caught:
        service.tools['search_public_web'](query='Python documentation')
    assert isinstance(caught.value.__cause__, WebNoResults)
    assert capabilities.failure_outcome(caught.value) == 'no_results'
    recorder(service, monkeypatch, decide('search_public_web', 'Python documentation'),
             decide(reply='이 채팅에는 웹 검색 기능이 없습니다.'))
    turn = service.send(service.store.create()['id'], uuid4().hex, '웹에서 Python documentation 검색해줘')['turns'][-1]
    trace = turn['tools'][0]
    assert (trace['status'], trace['error_type'], trace['outcome']) == ('error', 'tool_failed', 'no_results')
    assert '기능이 아닙니다' not in trace['detail'] and '기능이 없다는 뜻이 아닙니다' in trace['detail']
    assert '검색 기능이 없다는 뜻은 아닙니다' in turn['assistant'] and '기능이 없습니다' not in turn['assistant']


@pytest.mark.parametrize('error,outcome,shown', [
    (WebUnavailable('worker C:/Users/secret-path missing'), 'unavailable', '실행 경로를 쓸 수 없어'),
    (WebTimeout('deadline token=SECRET123'), 'timeout', '자동으로 다시 시도하지 않았습니다'),
    (WebBlocked('blocked password=SECRET123'), 'blocked', '안전 규칙이나 입력 조건'),
    (RuntimeError('timeout unavailable password=SECRET123'), 'failed', '원인은 확인되지 않았습니다'),
])
def test_failures_are_told_apart_by_type_without_leaking_raw_text(service, monkeypatch, error, outcome, shown):
    fetching(monkeypatch, error)
    recorder(service, monkeypatch, decide('search_public_web', 'Python documentation'),
             decide(reply='Raw: ' + str(error)))
    turn = service.send(service.store.create()['id'], uuid4().hex, '웹에서 Python documentation 검색해줘')['turns'][-1]
    assert turn['tools'][0]['outcome'] == outcome and shown in turn['assistant']
    stored = json.dumps(service.store.get(service.store.listing()[0]['id']), ensure_ascii=False)
    assert 'SECRET123' not in stored and 'secret-path' not in stored


def test_chat_admission_refusal_is_blocked_before_any_tool_call(service, monkeypatch):
    fetching(monkeypatch, CHANNEL)
    recorder(service, monkeypatch, decide('search_public_web', 'private topic'), decide(reply='찾았습니다.'))
    turn = service.send(service.store.create()['id'], uuid4().hex, '내 문서 검색해줘')['turns'][-1]
    assert turn['tools'][0]['outcome'] == 'blocked' and service.guard._calls == 0


def test_cause_chain_traversal_is_bounded_and_survives_cycles():
    first, second = ValueError('a'), ValueError('b')
    first.__cause__, second.__cause__ = second, first
    assert capabilities.failure_outcome(first) == 'failed' and capabilities.guardrail_cause(first) is None
    deep = WebTimeout('deep')
    for _ in range(capabilities.CAUSE_LIMIT):
        wrapper = RuntimeError('wrap')
        wrapper.__cause__ = deep
        deep = wrapper
    assert capabilities.failure_outcome(deep) == 'failed'      # beyond the bound: not guessed


def test_wrapped_budget_refusal_still_ends_the_turn(service, monkeypatch):
    calls = recorder(service, monkeypatch, decide('get_system_status'), decide(reply='상태를 확인했습니다.'))
    service.guard._calls = service.guard.MAX_CALLS
    identity = service.store.create()['id']
    with pytest.raises(ChatError):
        service.send(identity, uuid4().hex, '내 PC 상태 확인해줘')
    assert len(calls) == 1 and service.store.get(identity)['turns'][-1]['status'] == 'failed'
    with pytest.raises(ToolError) as caught:
        service.tools['get_system_status']()
    assert isinstance(capabilities.guardrail_cause(caught.value), GuardrailError)


def test_search_failure_then_capability_question_still_lists_search(service, monkeypatch):
    fetching(monkeypatch, UNRELATED)
    calls = recorder(service, monkeypatch, decide('search_public_web', 'Cheolgu YouTube'),
                     decide(reply='검색 기능이 없습니다.'), decide(reply='동영상은 직접 볼 수 없습니다.'))
    identity = service.store.create()['id']
    failed = service.send(identity, uuid4().hex, '웹에서 유튜브 철구 검색해줘')['turns'][-1]
    assert failed['tools'][0]['outcome'] == 'no_results'
    listed = service.send(identity, uuid4().hex, '너 뭘 할 수 있어?')['turns'][-1]
    assert len(calls) == 2                                          # the capability answer used no model
    supported, unsupported = listed['assistant'].split('할 수 없는 일:')
    assert '공개 웹 검색' in supported and '동영상' not in supported and '동영상 재생·시청' in unsupported
    # A legitimate statement that videos cannot be watched is not rewritten.
    video = service.send(identity, uuid4().hex, '유튜브 영상 봐줄 수 있어?')['turns'][-1]
    assert video['assistant'] == '동영상은 직접 볼 수 없습니다.'


def test_search_routing_hint_cannot_restore_an_unregistered_tool(service, monkeypatch):
    service.tools.pop('search_public_web')
    calls = recorder(service, monkeypatch, decide(reply='현재 검색 도구는 연결되지 않았습니다.'))
    service.send(service.store.create()['id'], uuid4().hex, '웹에서 Python 검색해줘')
    assert calls[0]['schema']['properties']['tool']['enum'] == ['none']
    assert 'search_public_web' not in prompt_tools(calls[0]['messages'][0]['content'])
