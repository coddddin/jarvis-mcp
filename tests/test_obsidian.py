"""Synthetic Vault fixtures only; no user notes, network, plugins or models."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

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
    (vault / 'secret.md').write_text(note() + '\npassword: private-data')
    (vault / 'ignored.txt').write_text(note())
    (vault / '.obsidian').mkdir()
    (vault / '.obsidian/plugin.md').write_text(note())
    outside = tmp_path.parent / (tmp_path.name + '-outside.md')
    outside.write_text(note())
    (vault / 'linked.md').symlink_to(outside)
    os.link(outside, vault / 'hard.md')
    assert store.index()['indexed_files'] == 1
    assert [x['path'] for x in store.search('검색')['results']] == ['JARVIS-Vault/note.md']
    with pytest.raises(GuardrailError):
        store.index(str(tmp_path.parent))


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
