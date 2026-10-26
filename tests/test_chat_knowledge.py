"""PA1-KNOWLEDGE: chat answers from approved Vault notes with sources. Synthetic Vault only; no models or network."""
import json

import pytest

from jarvis.agents.chat import ChatError
from jarvis.app.chat import ChatDecision, ChatService
from jarvis.core.guardrails import Guardrails


def note(identity, title, body, **changes):
    data = {'id': identity, 'title': title, 'created': '2026-10-22T12:00:00+00:00',
            'updated': '2026-10-22T12:01:00+00:00', 'source': 'manual', 'tags': ['테스트'],
            'sensitivity': 'private', 'review_status': 'approved', **changes}
    return ('---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in data.items())
            + '\n---\n# ' + title + '\n' + body + '\n')


MEETING = ('meeting', '프로젝트 회의록', '10월 회의에서 출시 일정을 11월 첫째 주로 정했다.\n다음 할 일은 테스트 자동화와 문서 정리다.')
RECIPE = ('recipe', '김치찌개 레시피', '돼지고기와 묵은지를 볶고 물을 붓는다. 두부와 대파를 넣고 15분 끓인다.')


@pytest.fixture
def vault(tmp_path):
    folder = tmp_path / 'JARVIS-Vault'
    folder.mkdir()
    for identity, title, body in (MEETING, RECIPE):
        (folder / (identity + '.md')).write_text(note(identity, title, body), encoding='utf-8')
    return folder


@pytest.fixture
def service(tmp_path, vault):
    chat = ChatService(Guardrails(tmp_path))
    chat.notes.index()
    return chat


def answer(text, *ids):
    return ChatDecision(reply=text, tool='none', argument='', basis='tool' if ids else 'general', evidence_ids=list(ids))


def recorder(service, monkeypatch, *replies):
    calls, queue = [], list(replies)

    def worker(messages, schema, deadline, on_delta=None):
        calls.append({'messages': messages, 'schema': schema})
        return queue.pop(0)
    monkeypatch.setattr(service, '_worker', worker)
    return calls


def send(service, conversation, message, request, mode='chat'):
    return service.send(conversation, request * 32, message, mode)['turns'][-1]


def test_note_question_answers_locally_with_line_sources(service, monkeypatch):
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주로 정했어요.', 'n1'))
    conversation = service.store.create()['id']
    turn = send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    assert turn['assistant'] == '출시는 11월 첫째 주로 정했어요.'
    [source] = turn['sources']
    assert source['note_id'] == 'meeting' and source['title'] == '프로젝트 회의록'
    assert source['path'] == 'JARVIS-Vault/meeting.md' and source['cited'] is True
    assert source['line_start'] == 1 and source['line_end'] == 13
    assert len(source['source_sha256']) == 64 and 'excerpt' not in source
    # One model call, no tools offered at all, citations restricted to the retrieved notes.
    [call] = calls
    assert call['schema']['properties']['tool']['enum'] == ['none']
    assert call['schema']['properties']['evidence_ids']['items']['enum'] == ['n1']
    assert '출시 일정을 11월 첫째 주로' in call['messages'][-1]['content']
    assert call['messages'][-1]['content'].startswith('UNTRUSTED NOTE DATA')
    assert turn['tools'][0]['name'] == 'search_obsidian_vault' and turn['tools'][0]['status'] == 'ok'
    # The stored record carries the same sources for later reads.
    assert service.store.get(conversation)['turns'][-1]['sources'] == turn['sources']


def test_uncited_notes_are_marked_not_cited(service, monkeypatch):
    recorder(service, monkeypatch, answer('노트에서 찾지 못했어요.'))
    turn = send(service, service.store.create()['id'], '메모해 둔 김치찌개 레시피 알려줘', 'a')
    assert [(s['note_id'], s['cited']) for s in turn['sources']] == [('recipe', False)]


def test_unrelated_question_returns_no_note_and_no_model_call(service, monkeypatch):
    calls = recorder(service, monkeypatch)
    turn = send(service, service.store.create()['id'], '내 노트에서 양자역학 정리 찾아줘', 'a')
    assert turn['assistant'] == ChatService.NOTE_NONE_REPLY
    assert turn['sources'] == [] and calls == []


@pytest.mark.parametrize('changes', [{'review_status': 'rejected'}, {'sensitivity': 'secret'}, None])
def test_withdrawn_or_changed_note_is_excluded_from_search_and_history(service, vault, monkeypatch, changes):
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주예요.', 'n1'), answer('일반 답변'))
    conversation = service.store.create()['id']
    assert send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')['sources']
    identity, title, body = MEETING
    path = vault / 'meeting.md'
    path.write_text(note(identity, title, body + '\n수정됨') if changes is None else note(identity, title, body, **changes),
                    encoding='utf-8')
    again = send(service, conversation, '내 노트에서 프로젝트 회의 내용 다시 찾아줘', 'b')
    assert again['assistant'] == ChatService.NOTE_NONE_REPLY and again['sources'] == []
    # A later ordinary turn no longer sends the answer built on the old note to the model.
    send(service, conversation, '그럼 다음 주에 뭐 하면 좋을까?', 'c')
    context = json.dumps(calls[-1]['messages'], ensure_ascii=False)
    assert '11월 첫째 주' not in context


def test_unchanged_note_answer_stays_in_history_for_follow_up(service, monkeypatch):
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주예요.', 'n1'), answer('일반 답변'))
    conversation = service.store.create()['id']
    send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    send(service, conversation, '그럼 다음 주에 뭐 하면 좋을까?', 'b')
    assert '11월 첫째 주' in json.dumps(calls[-1]['messages'], ensure_ascii=False)
    assert service.store.get(conversation)['turns'][-1]['sources'] == []


def test_new_conversation_starts_without_note_context(service, monkeypatch):
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주예요.', 'n1'), answer('안녕하세요.'))
    send(service, service.store.create()['id'], '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    fresh = service.store.create()['id']
    turn = send(service, fresh, '안녕?', 'b')
    assert turn['sources'] == []
    assert '11월' not in json.dumps(calls[-1]['messages'], ensure_ascii=False)


@pytest.mark.parametrize('mode', ['chat', 'auto'])
def test_request_to_send_notes_to_outside_employee_is_refused(service, monkeypatch, mode):
    monkeypatch.setattr(service, '_infer', lambda *args, **kwargs: pytest.fail('no model call expected'))
    monkeypatch.setattr(service.notes, 'search', lambda *args, **kwargs: pytest.fail('no Vault search expected'))
    monkeypatch.setattr(service.dispatcher, 'run', lambda *args, **kwargs: pytest.fail('no external dispatch expected'))
    turn = send(service, service.store.create()['id'], '무료 API로 내 노트 회의록 요약해줘', 'a', mode)
    assert turn['assistant'] == ChatService.NOTE_EXTERNAL_REPLY and turn['sources'] == []
    if mode == 'auto':
        assert turn['mode'] == 'chat' and turn['request_plan']['unsupported'] is True


def test_auto_note_question_is_resolved_to_local_chat_without_classifier(service, monkeypatch):
    monkeypatch.setattr(service, '_infer', lambda *args, **kwargs: pytest.fail('no classifier call expected'))
    recorder(service, monkeypatch, answer('출시는 11월 첫째 주예요.', 'n1'))
    turn = send(service, service.store.create()['id'], '내 노트에서 프로젝트 회의 내용 찾아줘', 'a', 'auto')
    assert turn['mode'] == 'chat' and turn['request_plan']['evidence'] == 'local'
    assert turn['sources'][0]['note_id'] == 'meeting'


def test_team_mode_never_searches_the_vault(service, monkeypatch):
    monkeypatch.setattr(service.notes, 'search', lambda *args, **kwargs: pytest.fail('no Vault search expected'))
    monkeypatch.setattr(service.team, 'run', lambda *args, **kwargs: ('팀 답변', []))
    turn = send(service, service.store.create()['id'], '내 노트에서 회의 내용 정리해줘', 'a', 'team-local')
    assert turn['assistant'] == '팀 답변' and turn['sources'] == []


def test_missing_index_is_reported_without_model_call(tmp_path, vault, monkeypatch):
    chat = ChatService(Guardrails(tmp_path))
    calls = recorder(chat, monkeypatch)
    turn = send(chat, chat.store.create()['id'], '내 노트에서 회의 내용 찾아줘', 'a')
    assert turn['assistant'] == ChatService.NOTE_NO_INDEX_REPLY
    assert turn['tools'][0]['status'] == 'error' and calls == []


@pytest.mark.parametrize('message', ['내 노트에서 회의 찾아줘', '메모해 둔 레시피 알려줘', '정리해 둔 자료 있어?',
                                     '옵시디언에 적어 둔 거', 'search my notes for the plan', '내 vault에서 찾아줘',
                                     '그 노트에 다음 할 일은 뭐라고 적혀 있었어?', '내 회의 노트 보여줘', '제 메모에 뭐 있어?',
                                     '제 자료에서 견적 찾아줘', '내 노트들 중에 여행 계획', '이 메모 다시 보여줘'])
def test_note_questions_are_detected(message):
    assert ChatService._is_note_request(message)


@pytest.mark.parametrize('message', [
    '지금 메모리 사용량 알려줘', '노트북 추천해줘', '안녕?', '웹에서 날씨 검색해줘', '롤 패치노트 알려줘',
    '파이썬 3.14 릴리스 노트 요약해줘', '애플 키노트 언제야?', 'M8 볼트 규격', '220볼트 콘센트', '우사인 볼트 기록',
    '갤럭시 노트 20 가격', 'HashiCorp Vault 설정법', '메모장 단축키', '메모해줘: 내일 9시 회의', '내 노트북이 느려',
    '안내 자료 만들어줘', '옵시디언 플러그인 추천해줘', '그 자료 웹에서 찾아줘', '이 자료 출처가 어디야?',
    '이 볼트 몇 mm야?', '제 자료구조 숙제 도와줘', '내 자료형 변환 코드 봐줘'])
def test_other_questions_are_not_note_questions(message):
    assert not ChatService._is_note_request(message)


def test_http_response_carries_sources_and_asset_is_served(service, monkeypatch):
    from uuid import uuid4
    from starlette.testclient import TestClient
    from jarvis.agents.store import TaskStore
    from jarvis.app.events import create_event_app
    recorder(service, monkeypatch, answer('출시는 11월 첫째 주예요.', 'n1'))
    app = create_event_app(TaskStore(service.guard), token='x' * 43, task_ids=[],
                           origins=['http://127.0.0.1:8787'], chat=service)
    headers = {'Authorization': 'Bearer ' + 'x' * 43, 'Origin': 'http://127.0.0.1:8787'}
    with TestClient(app, base_url='http://127.0.0.1:8787') as client:
        assert client.get('/assets/sources.js').status_code == 200
        identity = client.post('/api/chat/conversations', headers=headers, json={}).json()['id']
        result = client.post(f'/api/chat/conversations/{identity}/messages', headers=headers,
                             json={'request_id': uuid4().hex, 'message': '내 노트에서 프로젝트 회의 내용 찾아줘', 'mode': 'auto'})
        assert result.status_code == 200
        [source] = result.json()['turns'][-1]['sources']
        assert source['title'] == '프로젝트 회의록' and source['line_start'] == 1


@pytest.mark.parametrize('message, query', [
    ('내 노트에서 프로젝트 회의 내용 찾아줘', '프로젝트 회의'),
    ('노트에 적어 둔 출시 일정이 언제야?', '출시 일정이'),
    ('독서 노트에 다음에 읽을 책 있어?', '독서 다음에 읽을 책'),
    ('제 메모에서 출시 일정 알려줘', '출시 일정'),
    ('내 노트에서 찾아줘', '내 노트에서 찾아줘'),  # nothing left: the original is searched
])
def test_filler_words_are_removed_before_scoring(message, query):
    assert ChatService._note_query(message) == query


EXTERNAL = set(ChatService.EXTERNAL_TOOLS)


def test_web_turn_after_note_answer_never_sees_note_content(service, monkeypatch):
    """Review P1: a later web search must not be able to carry note text out as a query."""
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주로 정했어요.', 'n1'), answer('검색 없이 답할게요.'))
    conversation = service.store.create()['id']
    send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    with pytest.raises(ChatError):  # the stub refuses to search; only the context handed to the model matters
        send(service, conversation, '웹에서 그 일정이랑 비슷한 사례 검색해줘', 'b')
    web = calls[-1]
    assert 'search_public_web' in web['schema']['properties']['tool']['enum']
    assert '11월 첫째 주' not in json.dumps(web['messages'], ensure_ascii=False)


@pytest.mark.parametrize('message', ['방금 거 한 줄로 요약해줘', '그럼 다음 주에 뭐 하면 좋을까?'])
def test_follow_up_keeps_note_context_without_outside_tools(service, monkeypatch, message):
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주로 정했어요.', 'n1'), answer('요약: 11월 첫째 주 출시'))
    conversation = service.store.create()['id']
    send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    send(service, conversation, message, 'b')
    follow = calls[-1]
    assert '11월 첫째 주' in json.dumps(follow['messages'], ensure_ascii=False)
    assert not EXTERNAL & set(follow['schema']['properties']['tool']['enum'])


def test_turns_after_a_note_answer_stay_offline_until_a_web_request(service, monkeypatch):
    """The summary turn repeats note content, so the turn after it is still offline."""
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주로 정했어요.', 'n1'),
                     answer('요약: 11월 첫째 주 출시'), answer('좋아요.'))
    conversation = service.store.create()['id']
    send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    send(service, conversation, '방금 거 한 줄로 요약해줘', 'b')
    send(service, conversation, '고마워', 'c')
    assert not EXTERNAL & set(calls[-1]['schema']['properties']['tool']['enum'])


def test_withdrawn_note_also_drops_follow_up_turns_that_repeat_it(service, vault, monkeypatch):
    """Review P2-1: a summary of a withdrawn note must not reach the model either."""
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주로 정했어요.', 'n1'),
                     answer('요약: 11월 첫째 주 출시'), answer('일반 답변'))
    conversation = service.store.create()['id']
    send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    send(service, conversation, '방금 거 한 줄로 요약해줘', 'b')
    identity, title, body = MEETING
    (vault / 'meeting.md').write_text(note(identity, title, body, review_status='rejected'), encoding='utf-8')
    send(service, conversation, '그럼 다음 주에 뭐 하면 좋을까?', 'c')
    context = json.dumps(calls[-1]['messages'], ensure_ascii=False)
    assert '11월 첫째 주' not in context and '요약:' not in context


def test_conversation_without_note_answers_keeps_outside_tools(service, monkeypatch):
    calls = recorder(service, monkeypatch, answer('안녕하세요.'), answer('네.'))
    conversation = service.store.create()['id']
    send(service, conversation, '안녕?', 'a')
    send(service, conversation, '오늘 기분 어때?', 'b')
    assert EXTERNAL & set(calls[-1]['schema']['properties']['tool']['enum'])


def test_offline_turn_prompt_lists_only_the_allowed_tools(service, monkeypatch):
    calls = recorder(service, monkeypatch, answer('출시는 11월 첫째 주로 정했어요.', 'n1'), answer('네.'))
    conversation = service.store.create()['id']
    send(service, conversation, '내 노트에서 프로젝트 회의 내용 찾아줘', 'a')
    send(service, conversation, '고마워', 'b')
    system = calls[-1]['messages'][0]['content']
    assert 'search_public_web' not in system.split('허용 도구:')[1]
    assert '웹 검색·페이지·피드 도구를 쓸 수 없다' in system
