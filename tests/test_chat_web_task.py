"""Chat wiring of the web task agent (M4a): routing, progress drafts, stop, setup message, stored traces."""
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

import jarvis.app.chat as chat_module
from jarvis.agents.brain import external_allowed
from jarvis.agents.store import TaskStore
from jarvis.app.chat import ChatService
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails
from jarvis.features.web_agent import service as web_agent


@pytest.fixture
def service(tmp_path):
    return ChatService(Guardrails(tmp_path))


def configured(monkeypatch, present=True):
    monkeypatch.setattr(web_agent, 'configured_server', lambda config: object() if present else None)


def fake_result(**extra):
    return {'status': 'completed', 'reason': '', 'answer': '최고 21도\n\n출처:\n- https://www.weather.go.kr/w/index.do',
            'sources': ['https://www.weather.go.kr/w/index.do'], 'files': [], 'calls': 2, 'searches': ['제주 날씨'],
            'traces': [{'tool': 'search', 'query': '제주 날씨', 'status': 'ok'},
                       {'tool': 'open', 'url': 'https://www.weather.go.kr/w/index.do', 'status': 'ok'}],
            'family': 'public_web', **extra}


def send(service, message):
    identity = service.store.create()['id']
    return identity, service.send(identity, uuid4().hex, message)['turns'][-1]


def test_a_web_request_runs_the_agent_with_progress_lines_in_the_draft(service, monkeypatch):
    configured(monkeypatch)
    drafts = []
    monkeypatch.setattr(service.store, 'draft', lambda identity, request, text: drafts.append(text))

    def run_task(guard, request, decide, *, on_progress, should_stop):
        on_progress({'phase': 'start', 'tool': 'search', 'query': '제주 날씨'})
        on_progress({'phase': 'start', 'tool': 'open', 'url': 'https://www.weather.go.kr/w/index.do'})
        on_progress({'phase': 'done', 'tool': 'open', 'trace': {'status': 'failed', 'reason': 'The page did not open.'}})
        assert not should_stop()
        return fake_result()
    monkeypatch.setattr(web_agent, 'run_task', run_task)
    _, turn = send(service, '제주 날씨 찾아줘')
    assert turn['status'] == 'completed' and turn['assistant'].startswith('최고 21도')
    assert drafts[-1] == '검색 중: 제주 날씨\n페이지 여는 중: www.weather.go.kr\n실패: The page did not open.'
    assert [t['name'] for t in turn['tools']] == ['search_public_web', 'read_public_web_page']
    assert turn['sources'] == []                       # web sources never look like note sources


def test_a_web_task_turn_may_reach_an_external_model_but_a_download_turn_may_not(service, monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(web_agent, 'run_task', lambda *a, **k: fake_result())
    _, turn = send(service, '제주 날씨 찾아줘')
    assert ChatService._external_safe(turn)
    files = [{'name': 'r.csv', 'bytes': 4, 'status': 'saved', 'path': 'C:/w/downloads/r.csv'}]
    monkeypatch.setattr(web_agent, 'run_task', lambda *a, **k: fake_result(
        traces=[{'tool': 'download', 'status': 'ok', 'from': 'https://x.go.kr/r.csv', 'files': files}], files=files))
    _, turn = send(service, 'csv 파일 다운받아줘')
    assert not external_allowed('web_download') and not ChatService._external_safe(turn)


def test_without_a_browser_setup_a_download_gets_the_setup_message(service, monkeypatch):
    configured(monkeypatch, present=False)
    monkeypatch.setattr(service, '_worker', lambda *a: pytest.fail('no model call for the setup message'))
    _, turn = send(service, '공공데이터 csv 파일 하나 내려받아줘')
    assert turn['status'] == 'completed' and 'setup_web_agent.py' in turn['assistant']


def test_without_a_browser_setup_a_search_keeps_the_existing_chat_path(service, monkeypatch):
    configured(monkeypatch, present=False)
    monkeypatch.setattr(web_agent, 'run_task', lambda *a, **k: pytest.fail('agent must not run'))
    monkeypatch.setattr(service, '_worker', lambda *a: chat_module.ChatDecision(reply='안녕하세요.', tool='none',
                                                                               argument=''))
    _, turn = send(service, '안녕 자비스')
    assert turn['assistant'] == '안녕하세요.'


def test_local_requests_never_become_web_tasks(service):
    assert not service._wants_web_task('내 메일에서 영수증 찾아줘', None)
    assert not service._wants_web_task('로컬 문서에서 계약서 찾아줘', None)
    assert not service._wants_web_task('CPU 상태 알아봐줘', None)
    assert service._wants_web_task('GitHub 트렌딩에서 탑 5 찾고 분석 보고해줘', None)
    assert service._wants_web_task('KTX 요금 검색해서 알려줘', None)


def test_setup_problems_found_at_start_are_answered_not_failed(service, monkeypatch):
    configured(monkeypatch)

    def run_task(*a, **k):
        raise web_agent.WebSetupRequired('Playwright MCP 도구가 바뀌어 다시 승인해야 합니다.')
    monkeypatch.setattr(web_agent, 'run_task', run_task)
    _, turn = send(service, '제주 날씨 찾아줘')
    assert turn['status'] == 'completed' and '다시 승인' in turn['assistant']


def test_the_stop_route_ends_a_running_web_task(service, monkeypatch):
    configured(monkeypatch)
    app = create_event_app(TaskStore(service.guard), token='x' * 43, task_ids=[],
                           origins=['http://127.0.0.1:8787'], chat=service)
    headers = {'Authorization': 'Bearer ' + 'x' * 43, 'Origin': 'http://127.0.0.1:8787'}
    with TestClient(app, base_url='http://127.0.0.1:8787') as client:
        assert client.post('/api/chat/stop', headers=headers, json={}).json() == {'stopping': False}
        assert client.post('/api/chat/stop', headers={'Origin': headers['Origin']}, json={}).status_code == 401
        assert client.post('/api/chat/stop', headers=headers, json={'all': True}).status_code == 400

        def run_task(guard, request, decide, *, on_progress, should_stop):
            assert client.post('/api/chat/stop', headers=headers, json={}).json() == {'stopping': True}
            assert should_stop()
            return fake_result(status='stopped', reason='사용자가 작업을 멈췄습니다.', answer='사용자가 작업을 멈췄습니다.')
        monkeypatch.setattr(web_agent, 'run_task', run_task)
        _, turn = send(service, '제주 날씨 찾아줘')
    assert turn['status'] == 'failed' and '멈췄' in turn['assistant']
    assert not service._web_stop.is_set()


def test_a_web_task_turn_gets_the_longer_deadline(service, monkeypatch):
    configured(monkeypatch)
    seen = {}

    def run_task(*a, **k):
        identity = next(iter(service.store.listing()))['id']
        turn = service.store.get(identity)['turns'][-1]
        seen['span'] = turn['deadline'] - turn['created']
        return fake_result()
    monkeypatch.setattr(web_agent, 'run_task', run_task)
    send(service, '제주 날씨 찾아줘')
    assert seen['span'] == pytest.approx(660, abs=1)
