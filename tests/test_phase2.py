import asyncio
import json
from pathlib import Path
import subprocess
import os

import pytest
from mcp.server.fastmcp.exceptions import ToolError
import requests

from jarvis.core.guardrails import Guardrails, GuardrailError
from jarvis.features.knowledge.service import KnowledgeStore, KnowledgeError, vectorize
from jarvis.features.google.service import GoogleBriefing, GoogleIntegrationError, SCOPES
import server


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / 'JarvisWorkspace'
    workspace.mkdir()
    (workspace / 'documents').mkdir()
    return KnowledgeStore(Guardrails(workspace))


def write(store, name, text):
    path = store.guard.workspace / 'documents' / name
    path.write_text(text, encoding='utf-8')
    return path


def test_korean_search_and_persistence(store):
    write(store, 'notes.md', '# 자비스\n벡터 검색과 로컬 문서 인덱싱 설계 기록')
    write(store, 'fruit.txt', 'orange apple banana orchard')
    assert store.index()['indexed_files'] == 2
    result = KnowledgeStore(store.guard).search('벡터 검색')
    assert result['results'][0]['path'] == 'documents/notes.md'
    assert result['results'][0]['untrusted_content'] is True
    assert result['results'][0]['line'] == 1
    assert vectorize('ＡＢＣ') == vectorize('abc')


def test_changes_deletion_and_clear(store):
    path = write(store, 'notes.txt', 'unique original note')
    store.index()
    path.write_text('changed', encoding='utf-8')
    assert store.search('original')['stale_documents'] == ['documents/notes.txt']
    assert store.search('original')['results'] == []
    path.unlink()
    assert store.search('original')['results'] == []
    path = write(store, 'notes.txt', 'new snapshot')
    store.index()
    assert store.clear()['removed_chunks'] == 1
    assert path.read_text() == 'new snapshot'
    with pytest.raises(KnowledgeError, match='No index'):
        store.search('snapshot')


def test_invalid_files_reported_and_hidden_excluded(store):
    write(store, '.secret.txt', 'never index')
    path = write(store, 'broken.txt', 'placeholder')
    path.write_bytes(b'\xff\xfe')
    write(store, 'binary.txt', 'binary\x00data')
    write(store, 'valid.md', 'normal')
    result = store.index()
    assert result['indexed_files'] == 1
    assert len(result['skipped']) == 2
    with pytest.raises(KnowledgeError):
        store.index('.jarvis-index')


def test_atomic_failed_index_and_empty_rebuild(store, monkeypatch):
    path = write(store, 'note.md', 'remember this')
    store.index()
    monkeypatch.setattr(store, 'MAX_FILES', 0)
    with pytest.raises(KnowledgeError, match='previous index retained'):
        store.index()
    assert store.search('remember')['results']
    monkeypatch.setattr(store, 'MAX_FILES', 200)
    path.unlink()
    assert store.index()['chunks'] == 0
    assert store.search('remember')['results'] == []


@pytest.mark.parametrize('query,limit', [('', 5), ('x' * 501, 5), ('!!!', 5), ('a', 0), ('a', 11), ('a', True)])
def test_invalid_search(store, query, limit):
    with pytest.raises(KnowledgeError):
        store.search(query, limit)


def test_scope_and_link_escape(store, tmp_path):
    with pytest.raises(GuardrailError):
        store.index('..')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.md').write_text('secret data')
    link = store.guard.workspace / 'documents' / 'escape'
    if os.name == 'nt':
        process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        assert process.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    result = store.index()
    assert result['indexed_files'] == 0
    assert result['skipped'][0]['reason'] == 'unsafe directory'


def test_hardlinked_document_and_database_blocked(store, tmp_path):
    source = tmp_path / 'outside.txt'
    source.write_text('private outside document', encoding='utf-8')
    os.link(source, store.guard.workspace / 'documents' / 'hardlink.txt')
    result = store.index()
    assert result['indexed_files'] == 0
    assert result['skipped'][0]['reason'] == 'KnowledgeError'
    # The protected DB path must not let a hard link turn indexing into an outside write.
    store2_root = tmp_path / 'second'
    store2_root.mkdir()
    (store2_root / '.jarvis-index').mkdir()
    os.link(source, store2_root / '.jarvis-index' / 'knowledge.sqlite3')
    store2 = KnowledgeStore(Guardrails(store2_root))
    with pytest.raises(KnowledgeError, match='hard links'):
        store2.search('private')
    assert source.read_text() == 'private outside document'


def test_google_not_connected_and_invalid_token(tmp_path, monkeypatch):
    token = tmp_path / 'token.json'
    monkeypatch.setenv('JARVIS_GOOGLE_TOKEN_FILE', str(token))
    google = GoogleBriefing()
    assert google.status()['credentials_present'] is False
    with pytest.raises(GoogleIntegrationError, match='not connected'):
        google.briefing('2026-10-17')
    for data in ('[]', '{}', json.dumps({'scopes': list(SCOPES), 'token_uri': 'https://evil.invalid'})):
        token.write_text(data)
        with pytest.raises(GoogleIntegrationError):
            google._session()


@pytest.mark.parametrize('day,limit', [('2026-02-30', 5), ('today', 5), ('2026-10-17', 0), ('2026-10-17', 11)])
def test_invalid_briefing(day, limit):
    with pytest.raises(GoogleIntegrationError):
        GoogleBriefing().briefing(day, limit)


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, fail_calendar=False):
        self.calls = []
        self.fail_calendar = fail_calendar

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert kwargs['timeout'] == 10
        assert kwargs['allow_redirects'] is False
        if '/calendar/' in url:
            if self.fail_calendar:
                raise requests.ConnectionError('private debug detail')
            return FakeResponse({'items': [{'summary': 'meeting', 'start': {'dateTime': '2026-10-17T10:00:00+09:00'}}]})
        if url.endswith('/messages'):
            assert kwargs['params']['labelIds'] == ['INBOX', 'UNREAD']
            assert 'q' not in kwargs['params']
            return FakeResponse({'messages': [{'id': 'abc123'}], 'nextPageToken': 'next'})
        assert kwargs['params']['format'] == 'metadata'
        return FakeResponse({'payload': {'headers': [{'name': 'Subject', 'value': '제목'},
                                                    {'name': 'From', 'value': 'sender@example.com'}]}})


def test_google_readonly_briefing(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(GoogleBriefing, '_session', lambda self: session)
    result = GoogleBriefing().briefing('2026-10-17')
    assert result['calendar']['status'] == 'ok'
    assert result['gmail']['messages'][0]['subject'] == '제목'
    assert result['gmail']['has_more'] is True
    params = session.calls[0][1]['params']
    assert params['timeMin'] == '2026-10-17T00:00:00+09:00'
    assert params['timeMax'] == '2026-10-18T00:00:00+09:00'
    assert not result['partial']


def test_google_partial_failure_visible(monkeypatch):
    monkeypatch.setattr(GoogleBriefing, '_session', lambda self: FakeSession(fail_calendar=True))
    result = GoogleBriefing().briefing('2026-10-17')
    assert result['partial']
    assert result['calendar']['status'] == 'error'
    assert result['gmail']['status'] == 'ok'
    assert 'private debug detail' not in json.dumps(result)


def test_new_tools_obey_stop_and_budget(store):
    mcp = server.create_server(store.guard)
    (store.guard.workspace / '.jarvis-stop').touch()
    for name, args in [('index_local_documents', {}), ('search_local_documents', {'query': 'x'}),
                       ('clear_local_index', {}), ('get_google_connection_status', {}), ('get_daily_briefing', {})]:
        with pytest.raises(ToolError, match='stopped'):
            asyncio.run(mcp.call_tool(name, args))
