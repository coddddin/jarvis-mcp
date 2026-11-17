"""Vector RAG core (2e3093a2): heading chunks, collections, embedding reuse, hybrid search, lexical fallback.
A fake embedder and a fake worker process only; no models or network."""
import importlib.util
import io
import json
import sqlite3

import numpy as np
import pytest

from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import WORKERS_ROOT
from jarvis.features.embeddings import service as embeddings
from jarvis.features.embeddings.service import EmbedError, Embedder, chunk_markdown, rrf
from jarvis.features.knowledge.service import KnowledgeError, KnowledgeStore

# Concepts the fake model "understands": synonyms share an axis, so meaning matches without shared words.
AXES = [('고양이', 'cat', '반려묘'), ('출시', 'release', '배포'), ('예산', 'budget', '비용')]


class FakeEmbedder:
    model = 'fake-embed:1'

    def __init__(self, fail=False, fail_query=False):
        self.calls, self.fail, self.fail_query = [], fail, fail_query

    def embed(self, texts, *, query=False):
        self.calls.append((list(texts), query))
        if self.fail or (query and self.fail_query):
            raise EmbedError('unavailable', 'down')
        rows = [[1.0 + sum(text.casefold().count(word) for word in axis) * 10 for axis in AXES] for text in texts]
        matrix = np.asarray(rows, dtype=np.float32)
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


class FolderSource:
    """A source outside the workspace guard, like a0's ProjectDocsRoot."""

    def __init__(self, root, collection='project_docs', walk_error=None):
        self.root, self.collection, self.walk_error, self.skipped = root, collection, walk_error, []

    def walk(self):
        if self.walk_error:
            raise self.walk_error
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*.md'))

    def read(self, rel):
        if 'refused' in rel:
            raise ValueError('refused by source')
        return (self.root / rel).read_bytes()


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    (root / 'pets.md').write_text('# 집\n우리 반려묘는 밥을 많이 먹는다.\n', encoding='utf-8')
    (root / 'plan.md').write_text('# 계획\n다음 배포는 금요일이다.\n\n## 비용\n서버 비용 정리.\n', encoding='utf-8')
    return root


def make_store(tmp_path, embedder=None):
    workspace = tmp_path / 'ws'
    workspace.mkdir(exist_ok=True)
    return KnowledgeStore(Guardrails(workspace), embedder=embedder)


def test_markdown_chunks_keep_heading_path_and_verbatim_text():
    text = ('---\nid: x\n# yaml comment\n---\n# 가이드\n소개\n\n## 설치\n```\n# 코드 주석\n```\n설치 방법\n'
            '### 상세\n' + ''.join(f'문단{i} ' + '내용 ' * 150 + '\n\n' for i in range(4)) + '# 끝\n마침\n')
    chunks = chunk_markdown(text)
    assert [c.heading for c in chunks[:2]] == ['가이드', '가이드 > 설치']
    assert chunks[0].line == 1 and chunks[0].text.startswith('---')      # frontmatter joins the first section
    assert '# 코드 주석' in chunks[1].text                                 # fenced, not a heading
    deep = [c for c in chunks if c.heading == '가이드 > 설치 > 상세']
    assert len(deep) >= 2 and all(len(c.text) <= 1200 for c in chunks)
    for chunk in chunks:
        assert chunk.text in text and chunk.line == text.count('\n', 0, text.index(chunk.text)) + 1
    assert chunks[-1].heading == '끝'


def test_rrf_rewards_agreement():
    scores = rrf([['a', 'b', 'c'], ['b', 'a']])
    assert scores['a'] == scores['b'] > scores['c']


def test_hybrid_finds_meaning_and_reuses_vectors(tmp_path, docs):
    fake = FakeEmbedder()
    store = make_store(tmp_path, fake)
    source = FolderSource(docs)
    first = store.index_source(source)
    assert first['collection'] == 'project_docs' and first['embedding']['status'] == 'ok'
    assert first['embedded'] == first['chunks'] == 3 and first['reused'] == 0
    result = store.search('cat food', sources={'project_docs': source})
    assert result['retrieval'] == {'mode': 'hybrid', 'model': 'fake-embed:1'}
    top = result['results'][0]
    assert top['path'] == 'pets.md' and top['collection'] == 'project_docs' and top['method'] == 'hybrid'
    assert top['heading'] == '집' and top['lexical_score'] == 0 and top['embedding_score'] > 0.9
    assert store.search('cat food', mode='lexical', sources={'project_docs': source})['results'] == []
    # Unchanged rebuild embeds nothing; editing one section embeds only that chunk.
    assert store.index_source(source)['embedded'] == 0
    (docs / 'plan.md').write_text('# 계획\n다음 release는 월요일이다.\n\n## 비용\n서버 비용 정리.\n', encoding='utf-8')
    again = store.index_source(source)
    assert again['embedded'] == 1 and again['reused'] == 2
    assert len(fake.calls[-1][0]) == 1 and '월요일' in fake.calls[-1][0][0]


def test_collections_replace_independently_and_failed_walk_keeps_rows(tmp_path, docs):
    store = make_store(tmp_path, FakeEmbedder())
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'budget.md').write_text('# 예산\n올해 예산 보고\n', encoding='utf-8')
    (other / 'refused.md').write_text('# 비밀\n', encoding='utf-8')
    a, b = FolderSource(docs), FolderSource(other, 'documents')
    store.index_source(a)
    result = store.index_source(b)
    assert result['skipped'] == [{'path': 'refused.md', 'reason': 'ValueError'}]
    sources = {'project_docs': a, 'documents': b}
    both = store.search('예산 비용', 10, sources=sources)
    assert {hit['collection'] for hit in both['results']} == {'project_docs', 'documents'}
    only = store.search('예산 비용', 10, sources=sources, collections=['documents'])
    assert {hit['collection'] for hit in only['results']} == {'documents'}
    with pytest.raises(KnowledgeError, match='cap'):
        store.index_source(FolderSource(docs, walk_error=KnowledgeError('cap passed')))
    assert store.search('반려묘', sources=sources)['results'][0]['path'] == 'pets.md'
    with pytest.raises(KnowledgeError, match='No index'):
        store.search('예산', collections=['vault'])


def test_search_marks_changed_or_refused_source_files_stale(tmp_path, docs):
    store = make_store(tmp_path, FakeEmbedder())
    source = FolderSource(docs)
    store.index_source(source)
    (docs / 'pets.md').write_text('# 집\n바뀐 내용\n', encoding='utf-8')
    result = store.search('반려묘', sources={'project_docs': source})
    assert result['stale_documents'] == ['pets.md']
    assert all(hit['path'] != 'pets.md' for hit in result['results'])


def test_embedding_failures_fall_back_to_lexical_with_reason(tmp_path, docs):
    store = make_store(tmp_path, FakeEmbedder(fail=True))
    source = FolderSource(docs)
    result = store.index_source(source)
    assert result['embedding'] == {'status': 'unavailable', 'model': 'fake-embed:1', 'dim': None,
                                   'reason': 'unavailable'}
    assert result['chunks'] == 3                                      # lexical rows still committed
    found = store.search('반려묘', sources={'project_docs': source})
    assert found['retrieval']['mode'] == 'lexical' and found['retrieval']['fallback_reason'] == 'index_incomplete'
    assert found['results'][0]['path'] == 'pets.md' and found['results'][0]['embedding_score'] is None
    with pytest.raises(KnowledgeError, match='unavailable'):
        store.search('반려묘', sources={'project_docs': source}, mode='hybrid')
    store._embedder = FakeEmbedder(fail_query=True)
    store.index_source(source)
    found = store.search('반려묘', sources={'project_docs': source})
    assert found['retrieval']['fallback_reason'] == 'unavailable' and found['results']


def test_lexical_only_store_and_old_index_contract(tmp_path):
    store = make_store(tmp_path)
    (store.guard.workspace / 'documents').mkdir()
    (store.guard.workspace / 'documents' / 'a.md').write_text('# 메모\n로컬 검색 메모', encoding='utf-8')
    result = store.index()
    assert result['embedding'] == {'status': 'off', 'model': None, 'dim': None} and result['collection'] == 'documents'
    found = store.search('검색 메모')
    assert found['retrieval'] == {'mode': 'lexical', 'model': None, 'fallback_reason': 'off'}
    assert found['results'][0]['path'] == 'documents/a.md' and found['results'][0]['heading'] == '메모'


def test_v1_index_is_dropped_and_needs_rebuild(tmp_path):
    store = make_store(tmp_path)
    folder = store.guard.workspace / '.jarvis-index'
    folder.mkdir()
    with sqlite3.connect(folder / 'knowledge.sqlite3') as db:
        db.execute('CREATE TABLE chunks (path TEXT, digest TEXT, line INTEGER, text TEXT, vector TEXT)')
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO metadata VALUES ('updated', 'x')")
    db.close()
    with pytest.raises(KnowledgeError, match='No index'):
        store.search('anything')


def test_invalid_collection_name_refused(tmp_path, docs):
    with pytest.raises(KnowledgeError, match='Collection'):
        make_store(tmp_path).index_source(FolderSource(docs, 'Bad-Name'))


class FakeProcess:
    def __init__(self, reply, code=0):
        self.reply, self.returncode, self.sent = reply, code, []

    def communicate(self, data=None, timeout=None):
        if data is not None:
            self.sent.append(json.loads(data))
        return self.reply, b''

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def test_embedder_batches_prefixes_queries_and_maps_errors(tmp_path):
    guard = Guardrails(tmp_path)
    processes = []

    def spawn():
        process = FakeProcess(b'')
        def reply(data=None, timeout=None):
            if data is not None:
                process.sent.append(json.loads(data))
                count = len(process.sent[-1]['input'])
                process.reply = (json.dumps({'ok': True, 'embeddings': [[3.0, 4.0]] * count, 'dim': 2}) + '\n').encode()
            return process.reply, b''
        process.communicate = reply
        processes.append(process)
        return process

    embedder = Embedder(guard, 'qwen3-embedding:0.6b', spawn=spawn, batch=2)
    matrix = embedder.embed(['a', 'b', 'c'])
    assert matrix.shape == (3, 2) and np.allclose(matrix[0], [0.6, 0.8])
    assert [len(p.sent[0]['input']) for p in processes] == [2, 1] and processes[0].sent[0]['keep_alive'] == '2m'
    embedder.embed(['질문'], query=True)
    assert processes[-1].sent[0]['input'][0].startswith('Instruct: ') and processes[-1].sent[0]['input'][0].endswith('질문')
    failing = Embedder(guard, 'bge-m3:latest',
                       spawn=lambda: FakeProcess(b'{"ok": false, "error": "OllamaError", "kind": "http_404"}\n', 1))
    with pytest.raises(EmbedError) as caught:
        failing.embed(['a'])
    assert caught.value.kind == 'http_404' and 'Ollama' in str(caught.value)
    with pytest.raises(EmbedError) as caught:
        Embedder(guard, 'bge-m3:latest', spawn=lambda: FakeProcess(b'garbage')).embed(['a'])
    assert caught.value.kind == 'invalid'
    with pytest.raises(EmbedError):
        Embedder(guard, 'qwen3.5:9b')


def load_worker():
    spec = importlib.util.spec_from_file_location('embed_step_under_test', WORKERS_ROOT / 'embed_step.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_checks_model_and_bounds(monkeypatch):
    worker = load_worker()
    calls, original = [], worker.request

    def request(path, body=None, timeout=120):
        calls.append((path, body))
        if path == '/api/tags':
            return {'models': [{'name': 'bge-m3:latest'}, {'name': 'qwen3.5:9b'}]}
        if path == '/api/show':
            caps = ['embedding'] if body['model'] == 'bge-m3:latest' else ['completion']
            return {'details': {'format': 'gguf'}, 'capabilities': caps}
        return {'embeddings': [[0.1, 0.2]] * len(body['input'])}
    monkeypatch.setattr(worker, 'request', request)
    result = worker.embed({'model': 'bge-m3:latest', 'input': ['x' * 5000, 'y']})
    assert result == {'ok': True, 'embeddings': [[0.1, 0.2]] * 2, 'dim': 2}
    sent = calls[-1][1]
    assert calls[-1][0] == '/api/embed' and len(sent['input'][0]) == 2000 and sent['keep_alive'] == '2m'
    for payload in [{'model': 'qwen3.5:9b', 'input': ['x']}, {'model': 'bge-m3:latest', 'input': []},
                    {'model': 'bge-m3:latest', 'input': ['x'] * 33}, {'model': 'bge-m3:latest', 'input': ['x'],
                                                                       'keep_alive': '-1'},
                    {'model': 'gpt:cloud', 'input': ['x']}]:
        with pytest.raises(ValueError):
            worker.embed(payload)
    with pytest.raises(ValueError, match='Endpoint'):
        original('/api/pull', {})


def test_worker_main_reports_kind_without_ollama_text(monkeypatch, capsys):
    worker = load_worker()

    def request(path, body=None, timeout=120):
        raise worker.OllamaError('http_500')
    monkeypatch.setattr(worker, 'request', request)
    monkeypatch.setattr(worker.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'{"model": "bge-m3:latest", "input": ["x"]}')))
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'error': 'OllamaError', 'kind': 'http_500'}


def test_wrong_size_stored_vector_refused():
    with pytest.raises(ValueError):
        embeddings.from_blobs([b'\x00' * 8], 3)
