"""Offline backup/restore rehearsal of synthetic notes and a live SQLite index."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from jarvis.core.guardrails import Guardrails
from jarvis.features.obsidian.service import ObsidianStore
from scripts.backup.stage_data import stage


def test_existing_workspace_backup_includes_vault_and_restored_index_is_searchable(tmp_path):
    root = tmp_path / 'project'
    workspace = root / 'JarvisWorkspace'
    vault = workspace / 'JARVIS-Vault/10-Knowledge'
    vault.mkdir(parents=True)
    (root / '.jarvis-auth').mkdir()
    fields = {'id': 'backup-demo', 'title': '복구 검증', 'created': '2026-10-22T12:00:00+00:00',
              'updated': '2026-10-22T12:00:00+00:00', 'source': 'synthetic-test',
              'tags': ['backup'], 'sensitivity': 'private', 'review_status': 'approved'}
    text = '---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in fields.items())
    path = vault / 'recovery.md'
    path.write_text(text + '\n---\n자비스 백업 복원 검색 검증', encoding='utf-8')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    store = ObsidianStore(Guardrails(workspace))
    store.index()
    source = workspace / '.jarvis-obsidian/knowledge.sqlite3'
    # Keep a live WAL connection open. Raw file copying could miss the committed update.
    with sqlite3.connect(source) as live:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('rehearsal', 'committed-in-wal'))
        live.commit()
        counts = stage(root, tmp_path / 'staging')
        assert counts['sqlite'] == 1
        staged = tmp_path / 'staging/JarvisWorkspace/.jarvis-obsidian/knowledge.sqlite3'
        with sqlite3.connect(staged) as copied:
            assert copied.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert copied.execute("SELECT value FROM metadata WHERE key='rehearsal'").fetchone()[0] == 'committed-in-wal'
    restored = tmp_path / 'restored-workspace'
    shutil.copytree(tmp_path / 'staging/JarvisWorkspace', restored)
    result = ObsidianStore(Guardrails(restored)).search('백업 복원')
    assert result['results'][0]['metadata']['id'] == 'backup-demo'
    assert result['results'][0]['source_sha256'] == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert hashlib.sha256((restored / 'JARVIS-Vault/10-Knowledge/recovery.md').read_bytes()).hexdigest() == before
