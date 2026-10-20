import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import time
import sys
import zipfile

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jarvis.core.guardrails import Guardrails, GuardrailError
from jarvis.features.snapshots.service import SnapshotStore, SnapshotError
import server


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'workspace'
    (root / 'projects/demo').mkdir(parents=True)
    return SnapshotStore(Guardrails(root))


def put(store, name, content):
    path = store.guard.workspace / 'projects/demo' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_create_preview_restore_and_current_backup(store):
    a = put(store, 'a.py', b'original')
    b = put(store, 'nested/b.txt', b'old note')
    metadata = store.create('projects/demo')
    a.write_bytes(b'new version')
    b.unlink()
    extra = put(store, 'extra.txt', b'keep new file')
    secret = put(store, '.env', b'keep secret')
    preview = store.preview(metadata['id'])
    assert {item['action'] for item in preview['changes']} == {'replace', 'create'}
    restored = store.restore(preview['preview_id'])
    assert restored['restored_files'] == 2
    assert a.read_bytes() == b'original'
    assert b.read_bytes() == b'old note'
    assert extra.read_bytes() == b'keep new file'
    assert secret.read_bytes() == b'keep secret'
    backup, backup_files = store._load(restored['backup_id'])
    assert backup_files['a.py'] == b'new version'
    assert '.env' not in backup_files
    assert store.list()['pending_recoveries'] == []
    with pytest.raises(SnapshotError, match='already used'):
        store.restore(preview['preview_id'])


def test_exclusions_limits_and_safe_scope(store, monkeypatch):
    put(store, 'main.py', b'valid')
    for name in ('.env', 'client_secret.json', 'private.pem', '.venv/cache.txt',
                 'sources/synced.md', 'node_modules/file.js'):
        put(store, name, b'private or generated')
    metadata = store.create('projects/demo')
    assert metadata['file_count'] == 1
    assert len(metadata['excluded']) == 6
    monkeypatch.setattr(store, 'MAX_FILE_BYTES', 2)
    with pytest.raises(SnapshotError, match='size limit'):
        store.create('projects/demo')
    assert len(store.list()['snapshots']) == 1


@pytest.mark.parametrize('project', ['.', 'projects', '../outside', 'documents',
                                    'projects/.hidden', 'projects/demo/child', 'projects/demo.'])
def test_reject_project_scope(store, project):
    with pytest.raises((SnapshotError, GuardrailError)):
        store.create(project)


def test_preview_stale_and_expired(store):
    path = put(store, 'a.txt', b'original')
    snap = store.create('projects/demo')
    path.write_bytes(b'changed')
    preview = store.preview(snap['id'])
    path.write_bytes(b'newer')
    with pytest.raises(SnapshotError, match='changed since preview'):
        store.restore(preview['preview_id'])
    assert path.read_bytes() == b'newer'
    preview = store.preview(snap['id'])
    store.previews[preview['preview_id']]['expires'] = time.monotonic() - 1
    with pytest.raises(SnapshotError, match='expired'):
        store.restore(preview['preview_id'])


def test_checksum_tampering(store):
    put(store, 'a.py', b'original')
    snap = store.create('projects/demo')
    archive = store.guard.workspace / '.jarvis-snapshots' / (snap['id'] + '.zip')
    archive.write_bytes(archive.read_bytes() + b'tampered')
    with pytest.raises(SnapshotError, match='checksum mismatch'):
        store.preview(snap['id'])


def replace_archive(store, snap, names):
    vault = store.guard.workspace / '.jarvis-snapshots'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        manifest = {'version': 1, 'id': snap['id'], 'project': 'projects/demo',
                    'files': [{'path': name, 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}
                              for name in names]}
        z.writestr('manifest.json', json.dumps(manifest))
        for name in names:
            z.writestr('files/' + name, b'x')
    raw = stream.getvalue()
    (vault / (snap['id'] + '.zip')).write_bytes(raw)
    snap['archive_sha256'] = hashlib.sha256(raw).hexdigest()
    (vault / (snap['id'] + '.json')).write_text(json.dumps(snap))


@pytest.mark.parametrize('names', [['../outside.txt'], ['/outside.txt'], ['a\\b'],
                                  ['A.txt', 'a.txt'], ['a', 'a/b'], ['CON.txt'], ['x:ads'], ['.env']])
def test_hostile_archives_rejected_even_if_sidecar_changed(store, names):
    put(store, 'a.py', b'original')
    snap = store.create('projects/demo')
    replace_archive(store, snap, names)
    with pytest.raises(SnapshotError):
        store.preview(snap['id'])


def test_archive_expanded_size_limit(store, monkeypatch):
    put(store, 'a.txt', b'a' * 10000)
    snap = store.create('projects/demo')
    monkeypatch.setattr(store, 'MAX_FILE_BYTES', 100)
    with pytest.raises(SnapshotError, match='member exceeds'):
        store.preview(snap['id'])


def test_hardlinks_and_junctions_refused(store, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = outside / 'private.txt'
    original.write_bytes(b'do not read')
    linked = store.guard.workspace / 'projects/demo/hard.txt'
    os.link(original, linked)
    with pytest.raises(SnapshotError, match='non-hardlinked'):
        store.create('projects/demo')
    linked.unlink()
    junction = store.guard.workspace / 'projects/demo/external'
    if os.name == 'nt':
        import subprocess
        assert subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                              capture_output=True).returncode == 0
    else:
        junction.symlink_to(outside, target_is_directory=True)
    with pytest.raises(GuardrailError):
        store.create('projects/demo')


def test_mid_apply_failure_rolls_back(store, monkeypatch):
    a = put(store, 'a.txt', b'old-a')
    b = put(store, 'b.txt', b'old-b')
    snap = store.create('projects/demo')
    a.write_bytes(b'current-a')
    b.write_bytes(b'current-b')
    preview = store.preview(snap['id'])
    original_replace = store._replace
    def fail_second(source, destination):
        if Path(source).name == 'new-1':
            raise OSError('simulated failure')
        original_replace(source, destination)
    monkeypatch.setattr(store, '_replace', fail_second)
    with pytest.raises(SnapshotError, match='rollback completed'):
        store.restore(preview['preview_id'])
    assert a.read_bytes() == b'current-a'
    assert b.read_bytes() == b'current-b'
    assert store.list()['pending_recoveries'] == []


def test_failed_rollback_reported_and_blocks_next_restore(store, monkeypatch):
    a = put(store, 'a.txt', b'old-a')
    b = put(store, 'b.txt', b'old-b')
    snap = store.create('projects/demo')
    a.write_bytes(b'current-a')
    b.write_bytes(b'current-b')
    preview = store.preview(snap['id'])
    original_replace = store._replace
    def fail(source, destination):
        if Path(source).name in {'new-1', 'old-0'}:
            raise OSError('simulated I/O failure')
        original_replace(source, destination)
    monkeypatch.setattr(store, '_replace', fail)
    with pytest.raises(SnapshotError, match='rollback incomplete'):
        store.restore(preview['preview_id'])
    assert store.list()['pending_recoveries']
    with pytest.raises(SnapshotError, match='Unfinished recovery'):
        store.preview(snap['id'])


def test_staging_failure_does_not_change_project(store, monkeypatch):
    path = put(store, 'a.txt', b'original')
    snap = store.create('projects/demo')
    path.write_bytes(b'current')
    preview = store.preview(snap['id'])
    monkeypatch.setattr(store, '_stage', lambda *a: (_ for _ in ()).throw(OSError('no space')))
    with pytest.raises(SnapshotError, match='no project files changed'):
        store.restore(preview['preview_id'])
    assert path.read_bytes() == b'current'
    assert store.list()['pending_recoveries'] == []


def test_empty_noop_and_restart_invalidates_preview(store):
    snap = store.create('projects/demo')
    preview = store.preview(snap['id'])
    assert store.restore(preview['preview_id'])['restored_files'] == 0
    preview = store.preview(snap['id'])
    with pytest.raises(SnapshotError, match='expired'):
        SnapshotStore(store.guard).restore(preview['preview_id'])


def test_snapshot_tools_share_stop_and_budget(store):
    mcp = server.create_server(store.guard)
    (store.guard.workspace / '.jarvis-stop').touch()
    for name, args in [('create_project_snapshot', {'project': 'projects/demo'}),
                       ('list_project_snapshots', {}), ('preview_project_restore', {'snapshot_id': 'a' * 32}),
                       ('restore_project_snapshot', {'preview_id': 'a' * 32})]:
        with pytest.raises(ToolError, match='stopped'):
            asyncio.run(mcp.call_tool(name, args))


def test_mid_restore_stop_rolls_back(store, monkeypatch):
    a = put(store, 'a.txt', b'old-a')
    b = put(store, 'b.txt', b'old-b')
    snap = store.create('projects/demo')
    a.write_bytes(b'current-a')
    b.write_bytes(b'current-b')
    preview = store.preview(snap['id'])
    original_replace = store._replace
    def interrupt(source, destination):
        original_replace(source, destination)
        if Path(source).name == 'new-0':
            (store.guard.workspace / '.jarvis-stop').touch()
    monkeypatch.setattr(store, '_replace', interrupt)
    with pytest.raises(SnapshotError, match='rollback completed'):
        store.restore(preview['preview_id'])
    assert a.read_bytes() == b'current-a'
    assert b.read_bytes() == b'current-b'


def test_real_stdio_snapshot_restore(store):
    path = put(store, 'main.py', b'original source')
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__).resolve())],
                                       env={**os.environ, 'JARVIS_WORKSPACE': str(store.guard.workspace)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, response
                    return json.loads(response.content[0].text)
                snapshot = await call('create_project_snapshot', {'project': 'projects/demo'})
                path.write_bytes(b'changed source')
                preview = await call('preview_project_restore', {'snapshot_id': snapshot['id']})
                restored = await call('restore_project_snapshot', {'preview_id': preview['preview_id']})
                assert restored['restored_files'] == 1
                assert path.read_bytes() == b'original source'
                inventory = await call('list_project_snapshots', {})
                assert len(inventory['snapshots']) == 2
                assert inventory['pending_recoveries'] == []
    asyncio.run(exercise())
