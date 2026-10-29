"""Synthetic Vault fixtures only; no user notes, network, plugins or models."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from jarvis.core.guardrails import Guardrails, GuardrailError
from jarvis.features.knowledge.service import KnowledgeError, KnowledgeStore
from jarvis.features.obsidian.service import ObsidianStore, parse_frontmatter


def note(**changes):
    data = {'id': 'note-1', 'title': '자비스 검색', 'created': '2026-10-22T12:00:00+00:00',
            'updated': '2026-10-22T12:01:00+00:00', 'source': 'manual', 'tags': ['개발'],
            'sensitivity': 'private', 'review_status': 'approved', **changes}
    return '---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in data.items()) + '\n---\n벡터 검색 구현 기록\n[[other]] ![[photo.png]]'


def setup(tmp_path):
    vault = tmp_path / 'JARVIS-Vault'
    vault.mkdir()
    path = vault / 'note.md'
    path.write_text(note(), encoding='utf-8')
    return ObsidianStore(Guardrails(tmp_path)), vault, path


def test_local_search_metadata_and_independent_index(tmp_path):
    store, vault, path = setup(tmp_path)
    (tmp_path / 'documents').mkdir()
    (tmp_path / 'documents/plain.txt').write_text('ordinary document')
    normal = KnowledgeStore(store.guard)
    normal.index()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert store.index()['indexed_files'] == 1
    result = ObsidianStore(store.guard).search('벡터 검색')
    assert result['results'][0]['metadata']['id'] == 'note-1'
    assert result['results'][0]['path'] == 'JARVIS-Vault/note.md'
    assert result['results'][0]['untrusted_content']
    assert not result['external_transmission']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert normal.search('ordinary')['results'][0]['path'] == 'documents/plain.txt'


@pytest.mark.parametrize('changes', [
    {'review_status': 'proposed'}, {'review_status': 'rejected'}, {'sensitivity': 'secret'},
    {'updated': '2026-10-21T12:00:00+00:00'}, {'created': '2026-10-22'}, {'tags': 'invalid'},
])
def test_unapproved_or_invalid_notes_not_disclosed(tmp_path, changes):
    store, vault, path = setup(tmp_path)
    path.write_text(note(**changes), encoding='utf-8')
    assert store.index()['indexed_files'] == 0
    assert store.search('검색')['results'] == []


def test_revocation_hides_cached_excerpt(tmp_path):
    store, vault, path = setup(tmp_path)
    store.index()
    path.write_text(note(review_status='proposed'), encoding='utf-8')
    result = store.search('검색')
    assert result['results'] == []
    assert result['stale_documents'] == ['JARVIS-Vault/note.md']


def test_secrets_links_hidden_folders_and_non_markdown_excluded(tmp_path):
    store, vault, path = setup(tmp_path)
    (vault / 'secret.md').write_text(note() + '\npassword: private-data', encoding='utf-8')
    (vault / 'ignored.txt').write_text(note(), encoding='utf-8')
    (vault / '.obsidian').mkdir()
    (vault / '.obsidian/plugin.md').write_text(note(), encoding='utf-8')
    outside = tmp_path.parent / (tmp_path.name + '-outside.md')
    outside.write_text(note(), encoding='utf-8')
    os.link(outside, vault / 'hard.md')
    assert store.index()['indexed_files'] == 1
    assert [x['path'] for x in store.search('검색')['results']] == ['JARVIS-Vault/note.md']
    with pytest.raises(GuardrailError):
        store.index(str(tmp_path.parent))


def test_linked_vault_entries_are_not_indexed(tmp_path):
    store, vault, path = setup(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (outside / 'other.md').write_text(note(id='note-2'), encoding='utf-8')
    linked = 0
    try:
        (vault / 'linked.md').symlink_to(outside / 'other.md')
        linked += 1
    except OSError:  # Windows without symlink privilege (WinError 1314)
        pass
    if os.name == 'nt':  # a directory junction needs no privilege and must be refused the same way
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(vault / 'joined'), str(outside)],
                              capture_output=True, text=True, timeout=5)
        assert made.returncode == 0, made.stderr
        assert (vault / 'joined').is_junction()
        linked += 1
    if not linked:
        pytest.skip('no symlink or junction can be created on this machine')
    assert store.index()['indexed_files'] == 1
    assert [x['path'] for x in store.search('검색')['results']] == ['JARVIS-Vault/note.md']


def test_crlf_note_is_indexed_and_searchable(tmp_path):
    store, vault, path = setup(tmp_path)
    path.write_bytes(note().replace('\n', '\r\n').encode('utf-8'))
    assert store.index()['indexed_files'] == 1
    [hit] = store.search('검색')['results']
    assert hit['path'] == 'JARVIS-Vault/note.md' and hit['metadata']['id'] == 'note-1'


@pytest.mark.parametrize('body', ['a\rb', 'a\r\r\nb', 'a\x1bb'], ids=['lone-cr', 'cr-before-crlf', 'escape'])
def test_lone_carriage_return_and_control_characters_are_still_refused(tmp_path, body):
    store, vault, path = setup(tmp_path)
    path.write_bytes((note().replace('\n', '\r\n') + '\r\n' + body).encode('utf-8'))
    result = store.index()
    assert result['indexed_files'] == 0
    assert [x['path'] for x in result['skipped']] == ['JARVIS-Vault/note.md']


@pytest.mark.parametrize('text', [
    'no frontmatter', '---\nid: a\nid: b\n---\n',
    '---\nid: !!python/object:evil\n---\n',
    '---\ntags: &anchor [secret]\n---\n',
    '---\n' + 'title: a\n' * 100,
])
def test_frontmatter_fails_closed(text):
    with pytest.raises(KnowledgeError):
        parse_frontmatter(text)


def test_failed_rebuild_retains_previous_index_and_stop_marker(tmp_path):
    store, vault, path = setup(tmp_path)
    store.index()
    store.MAX_ENTRIES = 0
    with pytest.raises(KnowledgeError):
        store.index()
    assert store.search('검색')['results']
    (tmp_path / '.jarvis-stop').touch()
    with pytest.raises(GuardrailError):
        store.search('검색')


def test_tools_registered_guarded_and_old_contracts_preserved(tmp_path):
    from jarvis.app.plugins import collect_tools
    from jarvis.app.runtime import ToolContext
    guard = Guardrails(tmp_path)
    funcs = {f.__name__: f for f in collect_tools(ToolContext.create(guard))}
    assert {'index_obsidian_vault', 'search_obsidian_vault'} <= funcs.keys()
    (tmp_path / '.jarvis-stop').touch()
    for name, kwargs in [('index_obsidian_vault', {}), ('search_obsidian_vault', {'query': '검색'})]:
        with pytest.raises(ToolError):
            funcs[name](**kwargs)


def test_duplicate_note_ids_abort_rebuild_without_replacing_previous_snapshot(tmp_path):
    store, vault, path = setup(tmp_path)
    store.index()
    database = tmp_path / '.jarvis-obsidian/knowledge.sqlite3'
    before = database.read_bytes()
    (vault / 'duplicate.md').write_text(note(), encoding='utf-8')
    with pytest.raises(KnowledgeError, match='Duplicate'):
        store.index()
    assert database.read_bytes() == before
    assert store.search('검색')['results'][0]['path'] == 'JARVIS-Vault/note.md'


def test_mid_build_changes_abort_without_replacing_index(tmp_path, monkeypatch):
    store, vault, path = setup(tmp_path)
    store.index()
    database = tmp_path / '.jarvis-obsidian/knowledge.sqlite3'
    before = database.read_bytes()
    original = store._validate_snapshot
    def changed(rows):
        path.write_text(note(title='Changed during build'), encoding='utf-8')
        return original(rows)
    monkeypatch.setattr(store, '_validate_snapshot', changed)
    with pytest.raises(KnowledgeError, match='changed during indexing'):
        store.index()
    assert database.read_bytes() == before


def test_changed_approved_note_during_search_cannot_mix_old_excerpt_with_new_metadata(tmp_path, monkeypatch):
    store, vault, path = setup(tmp_path)
    store.index()
    original = store._read
    reads = []
    def changed(relative):
        reads.append(relative)
        if len(reads) == 2:
            path.write_text(note(title='Changed while reading'), encoding='utf-8')
        return original(relative)
    monkeypatch.setattr(store, '_read', changed)
    result = store.search('검색')
    assert result['results'] == []
    assert result['stale_documents'] == ['JARVIS-Vault/note.md']


def test_search_source_hash_identifies_exact_note_version(tmp_path):
    store, vault, path = setup(tmp_path)
    store.index()
    assert store.search('검색')['results'][0]['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
