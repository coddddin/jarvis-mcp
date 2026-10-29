"""Assistant view routes: same CSP and asset rules as the HUD; no data without the chat token."""
import re

from starlette.testclient import TestClient

from jarvis.agents.models import TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.app.events import create_event_app
from jarvis.core.guardrails import Guardrails

ASSETS = ('/assets/assistant/assistant.css', '/assets/assistant/assistant.js', '/assets/assistant/orb.js',
          '/assets/assistant/connection.js', '/assets/assistant/presence.js', '/assets/assistant/knowledge.js',
          '/assets/assistant/voice.js', '/assets/wiki/wiki.css', '/assets/wiki/wiki.js', '/assets/brain.js')


def client(tmp_path):
    store = TaskStore(Guardrails(tmp_path))
    task = store.create(TaskRequest(summary='Local task'))
    app = create_event_app(store, token='x' * 43, task_ids=[task.task_id], origins=['http://127.0.0.1:8787'])
    return TestClient(app, base_url='http://127.0.0.1:8787')


def test_assistant_page_and_assets_use_the_hud_security_headers(tmp_path):
    http = client(tmp_path)
    page = http.get('/assistant')
    assert page.status_code == 200 and 'text/html' in page.headers['content-type']
    for path in ASSETS:
        response = http.get(path)
        assert response.status_code == 200, path
    for response in (page, *(http.get(path) for path in ASSETS)):
        assert "script-src 'self'" in response.headers['content-security-policy']
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        assert response.headers['cache-control'] == 'no-store'


def test_assistant_page_loads_only_served_local_assets(tmp_path):
    html = client(tmp_path).get('/assistant').text
    referenced = re.findall(r'(?:src|href)="([^"#]+)"', html)
    assert set(path for path in referenced if path.startswith('/assets/')) == set(ASSETS)
    assert all(path == '/' or path.startswith('/assets/') for path in referenced)
    assert '<script>' not in html and 'style=' not in html  # CSP forbids inline script and style


def test_unknown_assistant_paths_and_chat_without_token_are_refused(tmp_path):
    http = client(tmp_path)
    assert http.get('/assets/assistant/../../config/agent_runtime.json').status_code == 404
    assert http.get('/assets/assistant/missing.js').status_code == 404
    slash = http.get('/assistant/', follow_redirects=False)  # Starlette's trailing-slash redirect only
    assert slash.status_code == 307 and slash.headers['location'].endswith('/assistant')
    assert http.get('/api/chat/conversations').status_code == 401
