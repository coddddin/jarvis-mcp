"""Wiki withdrawal veto. Synthetic fixtures only; no user notes, network, models or subprocesses."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jarvis.app.wiki_routes import wiki_routes
from jarvis.core.guardrails import GuardrailError, Guardrails
from jarvis.features.obsidian import service as obsidian_service
from jarvis.features.obsidian.service import ObsidianStore
from jarvis.features.wiki.service import WikiError, WikiStore, managed_note_withdrawn
from test_wiki import QUOTE, draft, make, note_path, vault_note
from test_wiki_routes import HEADERS, ORIGIN, SOURCE, Model, client

QUERY = 'zephyrbeacon Friday'


def db_path(tmp_path):
    return tmp_path / '.jarvis-wiki' / 'wiki.sqlite3'


def rows(tmp_path, sql):
    with closing(sqlite3.connect(db_path(tmp_path))) as db:
        return db.execute(sql).fetchall()


def published(tmp_path):
    store, source = make(tmp_path)
    candidate = store.create_candidate(source['id'], draft())
    saved = store.publish(candidate['id'], candidate['digest'])
    return store, source, candidate, saved


def ordinary_note(tmp_path):
    (tmp_path / 'JARVIS-Vault' / 'garden.md').write_text(vault_note(), encoding='utf-8')


def found_paths(guard, query=QUERY):
    return [hit['path'] for hit in ObsidianStore(guard).search(query)['results']]


def test_withdraw_hides_note_in_fresh_stores_and_reindex_and_preserves_every_record(tmp_path):
    store, source, candidate, saved = published(tmp_path)
    ordinary_note(tmp_path)
    obsidian = ObsidianStore(store.guard)
    assert obsidian.index()['indexed_files'] == 2
    # Lexical retrieval may also return an ordinary note; veto must target only this publication.
    assert saved['path'] in found_paths(store.guard)
    assert any(item['path'] == saved['path'] for item in obsidian.prepare_context(QUERY)['sources'])
    note_bytes = note_path(tmp_path, source).read_bytes()
    garden_bytes = (tmp_path / 'JARVIS-Vault' / 'garden.md').read_bytes()
    before = {table: rows(tmp_path, f'SELECT * FROM {table}') for table in ('sources', 'candidates', 'exports')}

    lookup = WikiStore(Guardrails(tmp_path)).publication(source['id'])
    assert lookup == {'source_id': source['id'], 'note_id': 'wiki-' + source['id'], 'candidate_id': candidate['id'],
                      'digest': candidate['digest'], 'file_sha256': saved['file_sha256'], 'status': 'approved',
                      'withdrawn': False}
    result = WikiStore(Guardrails(tmp_path)).withdraw(candidate['id'], candidate['digest'], lookup['file_sha256'])
    assert result == {'candidate_id': candidate['id'], 'status': 'withdrawn', 'source_id': source['id'],
                      'note_id': 'wiki-' + source['id'], 'digest': candidate['digest'],
                      'file_sha256': saved['file_sha256'], 'withdrawn': True, 'indexed': False}

    # A fresh instance over the old index hides the cached excerpt before any reindex.
    fresh = ObsidianStore(Guardrails(tmp_path))
    searched = fresh.search(QUERY)
    assert saved['path'] not in [item['path'] for item in searched['results']]
    assert saved['path'] in searched['stale_documents']
    context = fresh.prepare_context(QUERY)
    assert all(item['path'] != saved['path'] for item in context['sources'])
    assert 'zephyrbeacon' not in context['context']
    reindexed = ObsidianStore(Guardrails(tmp_path)).index()
    assert reindexed['indexed_files'] == 1 and saved['path'] in {item['path'] for item in reindexed['skipped']}
    assert saved['path'] not in found_paths(Guardrails(tmp_path))
    assert found_paths(Guardrails(tmp_path), 'Tomato watering') == ['JARVIS-Vault/garden.md']

    assert note_path(tmp_path, source).read_bytes() == note_bytes
    assert (tmp_path / 'JARVIS-Vault' / 'garden.md').read_bytes() == garden_bytes
    assert {table: rows(tmp_path, f'SELECT * FROM {table}') for table in before} == before
    assert rows(tmp_path, 'SELECT status FROM candidates') == [('approved',)]  # immutable row kept
    assert store.get_source(source['id'])['text'] == source['text']
    assert store.get_candidate(candidate['id'])['status'] == 'withdrawn'
    assert store.publication(source['id'])['withdrawn'] is True


def test_same_withdrawal_is_idempotent_and_nothing_resurrects_it(tmp_path):
    store, source, candidate, saved = published(tmp_path)
    first = store.withdraw(candidate['id'], candidate['digest'], saved['file_sha256'])
    assert WikiStore(Guardrails(tmp_path)).withdraw(candidate['id'], candidate['digest'], saved['file_sha256']) == first
    with pytest.raises(WikiError, match='withdrawn'):
        WikiStore(Guardrails(tmp_path)).publish(candidate['id'], candidate['digest'])
    assert store.create_candidate(source['id'], draft())['status'] == 'withdrawn'
    other = store.create_candidate(source['id'], draft(summary='A second review.'))
    assert other['status'] == 'proposed'
    with pytest.raises(WikiError, match='withdrawn'):
        store.publish(other['id'], other['digest'])
    with pytest.raises(WikiError, match='Only the published candidate'):
        store.withdraw(other['id'], other['digest'], saved['file_sha256'])
    with pytest.raises(WikiError, match='Expected file hash'):
        store.withdraw(candidate['id'], candidate['digest'], '0' * 64)
    assert rows(tmp_path, 'SELECT candidate_id FROM withdrawals') == [(candidate['id'],)]
    with closing(sqlite3.connect(db_path(tmp_path))) as db, pytest.raises(sqlite3.DatabaseError):
        db.execute('DELETE FROM withdrawals')
    assert ObsidianStore(store.guard).index()['indexed_files'] == 0


def test_invalid_or_unverified_withdrawals_are_refused_without_a_veto(tmp_path):
    store, source = make(tmp_path)
    candidate = store.create_candidate(source['id'], draft())
    with pytest.raises(WikiError, match='No completed'):
        store.withdraw(candidate['id'], candidate['digest'], '0' * 64)
    saved = store.publish(candidate['id'], candidate['digest'])
    expected = saved['file_sha256']
    for args in [(candidate['id'].upper(), candidate['digest'], expected), (True, candidate['digest'], expected),
                 (candidate['id'][:-1], candidate['digest'], expected), (candidate['id'], None, expected),
                 (candidate['id'], candidate['digest'], expected.upper()), (candidate['id'], candidate['digest'], 1)]:
        with pytest.raises(WikiError):
            store.withdraw(*args)
    with pytest.raises(WikiError, match='not found'):
        store.withdraw('e' * 32, candidate['digest'], expected)
    with pytest.raises(WikiError, match='does not match'):
        store.withdraw(candidate['id'], '0' * 64, expected)
    with pytest.raises(WikiError, match='Expected file hash'):
        store.withdraw(candidate['id'], candidate['digest'], hashlib.sha256(b'other').hexdigest())
    with pytest.raises(WikiError, match='not found'):
        store.publication('e' * 32)
    path = note_path(tmp_path, source)
    original = path.read_bytes()
    path.write_bytes(original + b'\nuser edit\n')
    with pytest.raises(WikiError, match='changed or removed'):
        store.withdraw(candidate['id'], candidate['digest'], expected)
    assert path.read_bytes() == original + b'\nuser edit\n'
    path.unlink()
    with pytest.raises(WikiError, match='changed or removed'):
        store.withdraw(candidate['id'], candidate['digest'], expected)
    outside = tmp_path.parent / (tmp_path.name + '-outside-note.md')
    outside.write_bytes(original)
    os.link(outside, path)
    with pytest.raises(WikiError, match='regular unlinked'):
        store.withdraw(candidate['id'], candidate['digest'], expected)
    assert outside.read_bytes() == original
    assert rows(tmp_path, 'SELECT * FROM withdrawals') == []


def test_pending_export_cannot_be_withdrawn(tmp_path, monkeypatch):
    store, source = make(tmp_path)
    candidate = store.create_candidate(source['id'], draft())
    monkeypatch.setattr(store, '_write_atomic', lambda target, data: (_ for _ in ()).throw(OSError('x')))
    with pytest.raises(WikiError):
        store.publish(candidate['id'], candidate['digest'])
    content_sha = rows(tmp_path, 'SELECT content_sha256 FROM exports')[0][0]
    with pytest.raises(WikiError, match='No completed'):
        store.withdraw(candidate['id'], candidate['digest'], content_sha)
    with pytest.raises(WikiError, match='No completed'):
        store.publication(source['id'])


def test_publish_final_transaction_rechecks_veto(tmp_path, monkeypatch):
    # Defensive path: a veto that appears while the file is written must stop the approval record.
    store, source = make(tmp_path)
    candidate = store.create_candidate(source['id'], draft())
    original = store._write_atomic

    def write_then_veto(target, data):
        original(target, data)
        with closing(sqlite3.connect(db_path(tmp_path))) as db, db:
            db.execute('INSERT INTO withdrawals VALUES (?, ?, ?, ?, ?)',
                       (source['id'], candidate['id'], candidate['digest'], hashlib.sha256(data).hexdigest(), 'now'))
    monkeypatch.setattr(store, '_write_atomic', write_then_veto)
    with pytest.raises(WikiError, match='withdrawn'):
        store.publish(candidate['id'], candidate['digest'])
    assert rows(tmp_path, 'SELECT state FROM exports') == [('pending',)]
    assert rows(tmp_path, 'SELECT status FROM candidates') == [('proposed',)]


def test_cross_instance_withdrawals_serialize_and_do_not_resurrect(tmp_path):
    store, source, candidate, saved = published(tmp_path)
    stores = [WikiStore(Guardrails(tmp_path)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: s.withdraw(candidate['id'], candidate['digest'], saved['file_sha256']), stores))
    assert results[0] == results[1] and results[0]['status'] == 'withdrawn'
    assert len(rows(tmp_path, 'SELECT * FROM withdrawals')) == 1
    with pytest.raises(WikiError, match='withdrawn'):
        store.publish(candidate['id'], candidate['digest'])
    assert stores[1].get_candidate(candidate['id'])['status'] == 'withdrawn'


def test_old_schema_without_withdrawals_table_means_none_withdrawn_and_is_not_altered(tmp_path):
    store, source, candidate, saved = published(tmp_path)
    with closing(sqlite3.connect(db_path(tmp_path))) as db, db:
        db.execute('DROP TABLE withdrawals')  # also drops its triggers, like a database from before this slice
    guard = Guardrails(tmp_path)
    assert managed_note_withdrawn(guard, source['id']) is False
    assert ObsidianStore(guard).index()['indexed_files'] == 1
    assert saved['path'] in found_paths(guard)
    assert rows(tmp_path, "SELECT name FROM sqlite_master WHERE name='withdrawals'") == []
    # The next write transaction adds the table additively and withdrawal works.
    assert store.withdraw(candidate['id'], candidate['digest'], saved['file_sha256'])['withdrawn'] is True
    assert found_paths(guard) == []


def test_canonical_note_without_verifiable_wiki_db_fails_closed_and_creates_nothing(tmp_path):
    hexid = 'c' * 32
    folder = tmp_path / 'JARVIS-Vault' / 'JarvisWiki'
    folder.mkdir(parents=True)
    managed = folder / (hexid + '.md')
    managed.write_text(vault_note(id='wiki-' + hexid, source='jarvis-wiki:' + hexid)
                       .replace('Tomato watering', 'Orchid misting'), encoding='utf-8')
    # Same folder, ordinary metadata: keeps the metadata-only policy.
    (folder / ('d' * 32 + '.md')).write_text(vault_note(id='note-2').replace('Tomato watering', 'Orchid pruning'),
                                            encoding='utf-8')
    ordinary_note(tmp_path)
    guard = Guardrails(tmp_path)
    paths = {'JARVIS-Vault/garden.md', f'JARVIS-Vault/JarvisWiki/{"d" * 32}.md'}
    indexed = ObsidianStore(guard).index()
    assert indexed['indexed_files'] == 2 and f'JARVIS-Vault/JarvisWiki/{hexid}.md' in {s['path'] for s in indexed['skipped']}
    assert set(found_paths(guard, 'Orchid plan')) | set(found_paths(guard, 'Tomato')) == paths
    assert not (tmp_path / '.jarvis-wiki').exists()  # missing DB: refused, never created

    store = WikiStore(guard)  # valid DB that does not know this managed note
    store.register_source('1' * 32, 'Other', 'other text')
    assert ObsidianStore(guard).index()['indexed_files'] == 2
    db_bytes = db_path(tmp_path).read_bytes()
    db_path(tmp_path).write_bytes(b'not a sqlite database' * 50)
    assert ObsidianStore(guard).index()['indexed_files'] == 2
    assert db_path(tmp_path).read_bytes() == b'not a sqlite database' * 50
    db_path(tmp_path).write_bytes(db_bytes)
    with pytest.raises(WikiError):
        managed_note_withdrawn(guard, hexid)


def test_linked_corrupt_or_missing_db_hides_a_real_published_note(tmp_path):
    store, source, candidate, saved = published(tmp_path)
    ordinary_note(tmp_path)
    guard = Guardrails(tmp_path)
    assert ObsidianStore(guard).index()['indexed_files'] == 2
    outside = tmp_path.parent / (tmp_path.name + '-outside.sqlite3')
    os.link(db_path(tmp_path), outside)
    assert saved['path'] not in found_paths(guard)
    assert found_paths(guard, 'Tomato watering') == ['JARVIS-Vault/garden.md']
    outside.unlink()
    assert saved['path'] in found_paths(guard)
    db_path(tmp_path).write_bytes(b'\x00corrupt' * 64)
    assert saved['path'] not in found_paths(guard)
    assert db_path(tmp_path).read_bytes() == b'\x00corrupt' * 64
    shutil.rmtree(tmp_path / '.jarvis-wiki')
    assert all(item['path'] != saved['path'] for item in ObsidianStore(guard).prepare_context(QUERY)['sources'])
    assert not (tmp_path / '.jarvis-wiki').exists()
    assert QUOTE.encode() in note_path(tmp_path, source).read_bytes()


def test_stop_marker_is_an_error_not_a_withdrawal_or_stale_success(tmp_path, monkeypatch):
    store, source, candidate, saved = published(tmp_path)
    ObsidianStore(store.guard).index()
    (tmp_path / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        store.withdraw(candidate['id'], candidate['digest'], saved['file_sha256'])
    (tmp_path / '.jarvis-stop').unlink()
    original = store._read_export

    def read_then_stop(target):
        data = original(target)
        (tmp_path / '.jarvis-stop').touch()
        return data
    monkeypatch.setattr(store, '_read_export', read_then_stop)
    with pytest.raises(GuardrailError):
        store.withdraw(candidate['id'], candidate['digest'], saved['file_sha256'])
    monkeypatch.undo()
    (tmp_path / '.jarvis-stop').unlink()
    assert rows(tmp_path, 'SELECT * FROM withdrawals') == []
    veto = obsidian_service.managed_note_withdrawn

    def stop_then_veto(guard, source_id):
        (tmp_path / '.jarvis-stop').touch()
        return veto(guard, source_id)
    monkeypatch.setattr(obsidian_service, 'managed_note_withdrawn', stop_then_veto)
    for call in (lambda: ObsidianStore(store.guard).search(QUERY),
                 lambda: ObsidianStore(store.guard).prepare_context(QUERY)):
        with pytest.raises(GuardrailError):
            call()
        (tmp_path / '.jarvis-stop').unlink()


# ---- routes -------------------------------------------------------------------------------

def bare(tmp_path):
    """The wiki routes alone with no model at all: lookup, withdraw and search must still work."""
    guard = Guardrails(tmp_path)
    routes = wiki_routes(WikiStore(guard), guard=guard, infer=None, valid=lambda token: token == 'x' * 43,
                         origins=[ORIGIN], headers={})
    return TestClient(Starlette(routes=routes), base_url=ORIGIN)


def test_routes_lookup_withdraw_and_search_without_inference(tmp_path):
    store, source, candidate, saved = published(tmp_path)
    ObsidianStore(store.guard).index()
    c = bare(tmp_path)
    url = f'/api/wiki/sources/{source["id"]}/publication'
    assert c.get(url, headers={'Authorization': HEADERS['Authorization']}).json()['status'] == 'approved'
    body = {'candidate_id': candidate['id'], 'digest': candidate['digest'], 'expected_file_sha256': saved['file_sha256']}
    assert c.post('/api/wiki/prepare', json=SOURCE, headers=HEADERS).status_code == 503
    for changes, status in [({'Authorization': ''}, 401), ({'Origin': 'https://evil.example'}, 403),
                            ({'Content-Type': 'text/plain'}, 415)]:
        assert c.post('/api/wiki/withdraw', content=json.dumps(body), headers={**HEADERS, **changes}).status_code == status
    assert c.get(url, headers={**HEADERS, 'Authorization': ''}).status_code == 401
    assert c.get(url, headers={**HEADERS, 'Origin': 'https://evil.example'}).status_code == 403
    assert c.get(url + '?x=1', headers=HEADERS).status_code == 403
    for bad in [{**body, 'path': saved['path']}, {k: v for k, v in body.items() if k != 'digest'},
                {**body, 'expected_file_sha256': '0' * 64}]:
        refused = c.post('/api/wiki/withdraw', json=bad, headers=HEADERS)
        assert refused.status_code == 400 and refused.json() == {'error': 'wiki_request_refused'}
    assert c.get('/api/wiki/sources/' + 'e' * 32 + '/publication', headers=HEADERS).json() == {
        'error': 'wiki_request_refused'}
    assert rows(tmp_path, 'SELECT * FROM withdrawals') == []
    done = c.post('/api/wiki/withdraw', json=body, headers=HEADERS)
    assert done.status_code == 200 and done.json()['status'] == 'withdrawn'
    assert done.json()['indexed'] is False and done.json()['index_refresh'] == 'not_run'
    assert c.post('/api/wiki/withdraw', json=body, headers=HEADERS).json() == done.json()
    assert c.get(url, headers=HEADERS).json()['withdrawn'] is True
    assert c.post('/api/wiki/search', json={'query': QUERY}, headers=HEADERS).json()['results'] == []
    again = c.post('/api/wiki/approve', json={'candidate_id': candidate['id'], 'digest': candidate['digest']},
                   headers=HEADERS)
    assert again.status_code == 400 and 'zephyrbeacon' not in again.text


def test_full_app_withdrawal_calls_the_model_only_for_preparation(tmp_path):
    model = Model()
    c = client(tmp_path, model)
    draft_result = c.post('/api/wiki/prepare', json=SOURCE, headers=HEADERS).json()
    saved = c.post('/api/wiki/approve', json={'candidate_id': draft_result['id'], 'digest': draft_result['digest']},
                   headers=HEADERS).json()
    assert saved['indexed'] is True
    assert c.post('/api/wiki/search', json={'query': '초록별'}, headers=HEADERS).json()['results']
    lookup = c.get(f'/api/wiki/sources/{saved["source_id"]}/publication', headers=HEADERS).json()
    assert set(lookup) == {'source_id', 'note_id', 'candidate_id', 'digest', 'file_sha256', 'status', 'withdrawn'}
    done = c.post('/api/wiki/withdraw', json={'candidate_id': lookup['candidate_id'], 'digest': lookup['digest'],
                                              'expected_file_sha256': lookup['file_sha256']}, headers=HEADERS)
    assert done.status_code == 200 and done.json()['withdrawn'] is True
    assert c.post('/api/wiki/search', json={'query': '초록별'}, headers=HEADERS).json()['results'] == []
    assert c.post('/api/wiki/prepare', json=SOURCE, headers=HEADERS).json()['status'] == 'withdrawn'
    assert model.calls == 1
