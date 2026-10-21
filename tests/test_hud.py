"""HUD authentication, data minimization and local operator selection."""
import asyncio
import pytest
from starlette.testclient import TestClient

from jarvis.agents.models import TaskRequest
from jarvis.agents.store import TaskStore, TaskError
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails
from jarvis.cli.hud import HUDServer
import uvicorn


@pytest.fixture
def setup(tmp_path):
    store = TaskStore(Guardrails(tmp_path))
    selected = store.create(TaskRequest(summary='Selected local task'))
    hidden = store.create(TaskRequest(summary='Unselected private task'))
    app = create_event_app(store, token='x' * 43, task_ids=[selected.task_id], origins=['http://127.0.0.1:8787'])
    return store, selected, hidden, TestClient(app)


def test_api_requires_header_and_exact_origin(setup):
    _, _, _, client = setup
    assert client.get('/api/tasks').status_code == 401
    assert client.get('/api/tasks?token=' + 'x' * 43).status_code == 401
    headers = {'Authorization': 'Bearer ' + 'x' * 43}
    assert client.get('/api/tasks', headers={**headers, 'Origin': 'https://example.org'}).status_code == 403
    assert client.get('/api/tasks', headers={**headers, 'Origin': 'http://127.0.0.1:8787'}).status_code == 200


def test_api_only_selected_summaries_no_proposals_or_context(setup):
    _, selected, hidden, client = setup
    response = client.get('/api/tasks', headers={'Authorization': 'Bearer ' + 'x' * 43})
    data = response.json()
    assert data['read_only'] and data['local_only']
    assert [task['task_id'] for task in data['tasks']] == [selected.task_id]
    assert hidden.task_id not in response.text
    assert not {'history', 'request', 'proposal', 'feedback', 'memory', 'source'} & data['tasks'][0].keys()
    assert response.headers['cache-control'] == 'no-store'


def test_process_observations_require_auth_and_minimize_data(setup):
    store, selected, _, _ = setup
    class Monitor:
        calls = 0
        def inspect(self, *, limit):
            self.calls += 1
            assert limit == 100
            return {'processes': [{'id': 'test', 'pid': 123, 'kind': 'verification',
                'label': '검증', 'process_state': 'running', 'warnings': ['runtime_exceeded'],
                'observed_at': 1000, 'parent_pid': 789, 'created': 1,
                'command_line': 'private-command'}], 'has_more': True}
    monitor = Monitor()
    client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
        origins=['http://127.0.0.1:8787'], processes=monitor))
    assert client.get('/api/processes').status_code == 401
    headers = {'Authorization': 'Bearer ' + 'x' * 43}
    assert client.get('/api/processes', headers={**headers, 'Origin': 'https://example.org'}).status_code == 403
    assert monitor.calls == 0
    response = client.get('/api/processes', headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data['has_more'] and data['termination_supported'] is False
    assert data['processes'][0]['warnings'] == ['runtime_exceeded']
    assert not {'command_line', 'parent_pid', 'created'} & data['processes'][0].keys()
    assert response.headers['cache-control'] == 'no-store'
    assert client.post('/api/processes', headers=headers).status_code == 405


def test_process_monitor_failure_is_not_healthy_empty_list(setup):
    store, selected, _, client = setup
    headers = {'Authorization': 'Bearer ' + 'x' * 43}
    assert client.get('/api/processes', headers=headers).status_code == 503
    class BrokenMonitor:
        def inspect(self, **kwargs):
            raise RuntimeError('private-failure-detail')
    client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
        origins=['http://127.0.0.1:8787'], processes=BrokenMonitor()))
    response = client.get('/api/processes', headers=headers)
    assert response.status_code == 503
    assert response.json() == {'error': 'status_unavailable'}
    assert 'private-failure-detail' not in response.text


def test_collaboration_snapshot_is_authenticated_read_only_and_origin_bound(setup):
    store, selected, _, _ = setup
    class Board:
        calls = 0
        def activity_snapshot(self):
            self.calls += 1
            return {'schema_version': 1, 'source': 'self_reported', 'read_only': True,
                'agents': [], 'tasks': [], 'timeline': []}
    board = Board()
    client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
        origins=['http://127.0.0.1:8787'], board=board))
    headers = {'Authorization': 'Bearer ' + 'x' * 43}
    # Exact Host is intentionally enforced, including requests without Origin.
    assert client.get('/api/collaboration', headers=headers).status_code == 403
    base = 'http://127.0.0.1:8787/api/collaboration'
    assert client.get(base).status_code == 401
    assert client.get(base + '?token=ignored', headers=headers).status_code == 403
    assert client.get(base, headers={**headers, 'Origin': 'http://evil.example'}).status_code == 403
    assert board.calls == 0
    response = client.get(base, headers=headers)
    assert response.status_code == 200
    assert response.json()['source'] == 'self_reported'
    assert response.headers['cache-control'] == 'no-store'
    assert board.calls == 1
    assert client.post(base, headers=headers).status_code == 405


def test_collaboration_unavailable_does_not_report_idle_employees(setup):
    store, selected, _, _ = setup
    class BrokenBoard:
        def activity_snapshot(self):
            raise RuntimeError('private-board-error')
    for board in (None, BrokenBoard()):
        client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
            origins=['http://127.0.0.1:8787'], board=board))
        response = client.get('http://127.0.0.1:8787/api/collaboration',
            headers={'Authorization': 'Bearer ' + 'x' * 43})
        assert response.status_code == 503
        assert response.json() == {'error': 'collaboration_unavailable'}
    for asset in ('collaboration.js', 'collaboration.css'):
        assert client.get('/assets/' + asset).status_code == 200


def test_forgotten_omitted_but_stop_not_hidden_as_empty_success(setup):
    store, selected, _, client = setup
    store.interrupt(selected.task_id, 'Cancel', cancelled=True)
    store.forget(selected.task_id)
    headers = {'Authorization': 'Bearer ' + 'x' * 43}
    assert client.get('/api/tasks', headers=headers).json()['tasks'] == []
    (store.guard.workspace / '.jarvis-stop').touch()
    assert client.get('/api/tasks', headers=headers).status_code == 503


def test_curated_assets_no_filesystem_or_mutation_endpoints(setup):
    _, _, _, client = setup
    for path in ('/', '/assets/hud.css', '/assets/hud.js', '/assets/task-stream.js'):
        response = client.get(path)
        assert response.status_code == 200
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert client.get('/.jarvis-auth/agent-events-token.txt').status_code == 404
    assert client.get('/assets/%2e%2e/config/agent_runtime.json').status_code == 404
    assert client.post('/api/tasks', json={'summary': 'Run code'}).status_code == 405


def test_recent_operator_selection_is_bounded_and_validated(setup):
    store, _, hidden, _ = setup
    assert store.recent(limit=1)[0].task_id == hidden.task_id
    for limit in (0, 33, True):
        with pytest.raises(TaskError):
            store.recent(limit=limit)
    with store._connection() as db:
        db.execute('UPDATE tasks SET record=? WHERE id=?', ('{}', hidden.task_id))
    with pytest.raises(TaskError):
        store.recent()


def test_browser_opens_only_after_successful_bind(monkeypatch):
    seen = []
    async def startup(server, sockets=None):
        assert seen == []
        server.started = True
    monkeypatch.setattr(uvicorn.Server, 'startup', startup)
    monkeypatch.setattr('jarvis.cli.hud.webbrowser.open', seen.append)
    server = HUDServer(uvicorn.Config('unused'), open_url='http://127.0.0.1:8787')
    asyncio.run(server.startup())
    assert seen == ['http://127.0.0.1:8787']


def test_activity_events_require_run_token_and_fixed_bounded_identity(setup):
    store, selected, _, _ = setup
    class Activity:
        calls = []
        def ingest(self, token, event):
            self.calls.append((token, event))
            if token != 'r' * 43:
                raise ValueError('private-token-detail')
            return {'status': 'accepted', 'run_id': event['run_id'], 'seq': event['seq'], 'private': 'not exposed'}
        def inspect(self, **kwargs):
            return {'runs': [], 'read_only': True}
    activity = Activity()
    client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
        origins=['http://127.0.0.1:8787'], activity=activity))
    run_id = 'a' * 32
    url = 'http://127.0.0.1:8787/api/activity/runs/' + run_id + '/events'
    event = {'run_id': run_id, 'epoch': 1, 'seq': 1, 'kind': 'start', 'phase': 'working', 'reason_code': 'none', 'result_ref': None}
    headers = {'Authorization': 'Bearer ' + 'r' * 43}
    assert client.post(url, json=event).status_code == 401
    assert client.post(url, json=event, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(url + '?token=bad', json=event, headers=headers).status_code == 403
    assert client.post(url, json={**event, 'run_id': 'b' * 32}, headers=headers).status_code == 400
    assert client.post(url, json={**event, 'extra': 'a' * 1025}, headers=headers).status_code == 413
    assert activity.calls == []
    response = client.post(url, json=event, headers={'Authorization': 'Bearer ' + 'x' * 43})
    assert response.status_code == 400
    assert response.json() == {'error': 'event_rejected'}
    response = client.post(url, json=event, headers=headers)
    assert response.json() == {'status': 'accepted', 'run_id': run_id, 'seq': 1}
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('http://127.0.0.1:8787/api/activity', headers=headers).status_code == 401
    assert client.get('http://127.0.0.1:8787/api/activity', headers={'Authorization': 'Bearer ' + 'x' * 43}).status_code == 200


def test_activity_collector_failure_is_not_empty_success(setup):
    store, selected, _, _ = setup
    class Broken:
        def inspect(self, **kwargs):
            raise RuntimeError('private-error')
    for activity in (None, Broken()):
        client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
            origins=['http://127.0.0.1:8787'], activity=activity))
        response = client.get('http://127.0.0.1:8787/api/activity', headers={'Authorization': 'Bearer ' + 'x' * 43})
        assert response.status_code == 503
        assert response.json() == {'error': 'activity_unavailable'}


def test_real_activity_store_http_reports_are_not_verification(setup):
    from jarvis.agents.activity import ActivityStore
    store, selected, _, _ = setup
    activity = ActivityStore(store.guard)
    registration = activity.register(owner='local:http-test', task_id='c' * 32)
    client = TestClient(create_event_app(store, token='x' * 43, task_ids=[selected.task_id],
        origins=['http://127.0.0.1:8787'], activity=activity))
    url = 'http://127.0.0.1:8787/api/activity/runs/' + registration['run_id'] + '/events'
    headers = {'Authorization': 'Bearer ' + registration['token']}
    event = {'run_id': registration['run_id'], 'epoch': registration['epoch'], 'seq': 1,
        'kind': 'start', 'phase': 'working', 'reason_code': 'none', 'result_ref': None}
    assert client.post(url, json=event, headers=headers).json()['status'] == 'accepted'
    assert client.post(url, json=event, headers=headers).json()['status'] == 'duplicate'
    assert client.post(url, json={**event, 'seq': 2, 'kind': 'verification_passed'}, headers=headers).status_code == 400
    assert client.post(url, json={**event, 'seq': 2, 'epoch': registration['epoch'] + 1}, headers=headers).status_code == 400
    assert client.post(url, json={**event, 'seq': 3, 'kind': 'completed', 'phase': 'finished'}, headers=headers).status_code == 200
    response = client.get('http://127.0.0.1:8787/api/activity', headers={'Authorization': 'Bearer ' + 'x' * 43})
    data = response.json()
    run = data['runs'][0]
    assert data['control_supported'] is False
    assert run['report']['source'] == 'self_reported'
    assert run['report']['event']['kind'] == 'completed'
    assert run['process']['source'] == 'observed'
    assert run['process']['status'] == 'running'
    assert run['heartbeat']['status'] == 'recent'
    assert run['sequence_gap'] is True
    assert run['verified'] == []
    assert registration['token'] not in response.text


def test_session_status_is_authenticated_read_only_and_never_receives_event_posts(setup):
    from jarvis.agents.session_status import SessionStatusStore
    store, selected, _, _ = setup
    sessions = SessionStatusStore(store.guard)
    sessions.report({'employee': 'codex', 'session_id': 'test-session', 'event_id': 'a'*32,
        'turn_id': 'test-turn', 'event': 'permission_requested', 'source': 'self_reported'})
    client = TestClient(create_event_app(store, token='x'*43, task_ids=[selected.task_id],
        origins=['http://127.0.0.1:8787'], sessions=sessions))
    url = 'http://127.0.0.1:8787/api/sessions'
    headers = {'Authorization': 'Bearer '+'x'*43}
    assert client.get(url).status_code == 401
    assert client.get(url+'?token=bad', headers=headers).status_code == 403
    assert client.get(url, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/sessions', headers=headers).status_code == 403
    response = client.get(url, headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['identity_verified'] is False
    assert response.json()['sessions'][0]['state'] == 'unknown'
    assert client.post(url, json={'event': 'tool_started'}, headers=headers).status_code == 405


def test_missing_session_adapter_is_unavailable_not_idle(setup):
    client = setup[-1]
    response = client.get('http://127.0.0.1:8787/api/sessions', headers={'Authorization': 'Bearer '+'x'*43})
    assert response.status_code == 503
    assert response.json() == {'error': 'sessions_unavailable'}
