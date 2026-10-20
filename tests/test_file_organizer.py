import asyncio
import json
import os
from pathlib import Path
import sys
import time

import pytest
from pydantic import ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from jarvis.core.guardrails import Guardrails, GuardrailError
from jarvis.features.snapshots.service import SnapshotStore
from jarvis.features.files.service import FileOrganizer, OrganizationError, ProjectRequest, Move
import jarvis.features.files.service as file_organizer
import server


@pytest.fixture
def organizer(tmp_path):
    workspace = tmp_path / 'workspace'
    (workspace / 'projects/demo').mkdir(parents=True)
    return FileOrganizer(SnapshotStore(Guardrails(workspace)))


def put(organizer, name, data=b'data'):
    path = organizer.guard.workspace / 'projects/demo' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_preview_apply_backup_and_undo(organizer):
    pdf = put(organizer, 'note.pdf', b'pdf')
    image = put(organizer, 'photo.png', b'image')
    code = put(organizer, 'main.py', b'print(1)')
    requirements = put(organizer, 'requirements.txt', b'pydantic')
    nested = put(organizer, 'nested/keep.txt', b'nested')
    preview = organizer.preview('projects/demo')
    assert len(preview['moves']) == 2
    assert pdf.exists() and image.exists()
    assert not (pdf.parent / 'Organized').exists()
    applied = organizer.apply(preview['preview_id'])
    assert applied['moved_files'] == 2
    assert not pdf.exists() and not image.exists()
    assert code.exists() and requirements.exists() and nested.exists()
    _, backed_up = organizer.snapshots._load(applied['backup_id'])
    assert backed_up['note.pdf'] == b'pdf'
    history = organizer.operations()
    assert history['operations'][0]['status'] == 'complete'
    # Undo works after server restart, but requires a new preview.
    restarted = FileOrganizer(organizer.snapshots)
    undo_preview = restarted.preview_undo(applied['operation_id'])
    undone = restarted.undo(undo_preview['preview_id'])
    assert undone['status'] == 'undone'
    assert pdf.read_bytes() == b'pdf' and image.read_bytes() == b'image'
    with pytest.raises(OrganizationError):
        restarted.preview_undo(applied['operation_id'])


def test_collision_stale_and_expired_preview(organizer):
    source = put(organizer, 'note.pdf')
    preview = organizer.preview('projects/demo')
    source.write_bytes(b'changed')
    with pytest.raises(OrganizationError, match='changed since'):
        organizer.apply(preview['preview_id'])
    preview = organizer.preview('projects/demo')
    organizer.previews[preview['preview_id']]['expires'] = time.monotonic() - 1
    with pytest.raises(OrganizationError, match='expired'):
        organizer.apply(preview['preview_id'])
    preview = organizer.preview('projects/demo')
    destination = source.parent / preview['moves'][0]['destination']
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'do not overwrite')
    with pytest.raises(OrganizationError, match='already exists'):
        organizer.apply(preview['preview_id'])
    preview = organizer.preview('projects/demo')
    assert preview['collisions'] == ['note.pdf']
    with pytest.raises(OrganizationError, match='collisions'):
        organizer.apply(preview['preview_id'])
    assert destination.read_bytes() == b'do not overwrite'


def test_preview_single_use_and_restart(organizer):
    put(organizer, 'note.txt')
    preview = organizer.preview('projects/demo')
    with pytest.raises(OrganizationError, match='expired'):
        FileOrganizer(organizer.snapshots).apply(preview['preview_id'])
    organizer.apply(preview['preview_id'])
    with pytest.raises(OrganizationError, match='already used'):
        organizer.apply(preview['preview_id'])


def test_failed_apply_rolls_back_and_failed_rollback_blocks(organizer, monkeypatch):
    a = put(organizer, 'a.txt')
    b = put(organizer, 'b.txt')
    preview = organizer.preview('projects/demo')
    original = organizer._move
    def fail_second(source, destination):
        if Path(source) == b:
            raise OSError('injected failure')
        original(source, destination)
    monkeypatch.setattr(organizer, '_move', fail_second)
    with pytest.raises(OrganizationError, match='rollback completed'):
        organizer.apply(preview['preview_id'])
    assert a.exists() and b.exists()
    assert not organizer.operations()['pending_recoveries']
    preview = organizer.preview('projects/demo')
    def fail_rollback(source, destination):
        if Path(source) == b or Path(destination) == a:
            raise OSError('injected failure')
        original(source, destination)
    monkeypatch.setattr(organizer, '_move', fail_rollback)
    with pytest.raises(OrganizationError, match='rollback incomplete'):
        organizer.apply(preview['preview_id'])
    assert organizer.operations()['pending_recoveries']
    with pytest.raises(OrganizationError, match='Unfinished recovery'):
        organizer.apply(organizer.preview('projects/demo')['preview_id'])


def test_undo_rejects_changed_file_and_original_collision(organizer):
    original = put(organizer, 'note.pdf')
    preview = organizer.preview('projects/demo')
    applied = organizer.apply(preview['preview_id'])
    original.write_bytes(b'new original')
    with pytest.raises(OrganizationError, match='already exists'):
        organizer.preview_undo(applied['operation_id'])
    original.unlink()
    moved = original.parent / preview['moves'][0]['destination']
    moved.write_bytes(b'changed file')
    with pytest.raises(OrganizationError, match='changed since'):
        organizer.preview_undo(applied['operation_id'])


def test_undo_partial_failure_preserves_organized_state(organizer, monkeypatch):
    put(organizer, 'a.txt')
    put(organizer, 'b.txt')
    preview = organizer.preview('projects/demo')
    applied = organizer.apply(preview['preview_id'])
    undo_preview = organizer.preview_undo(applied['operation_id'])
    original = organizer._move
    def fail_second(source, destination):
        if Path(destination).name == 'b.txt' and Path(destination).parent.name == 'demo':
            raise OSError('injected undo failure')
        original(source, destination)
    monkeypatch.setattr(organizer, '_move', fail_second)
    with pytest.raises(OrganizationError, match='rollback completed'):
        organizer.undo(undo_preview['preview_id'])
    assert organizer.operations()['operations'][0]['status'] == 'undo_rolled_back'
    for move in preview['moves']:
        assert (organizer.guard.workspace / 'projects/demo' / move['destination']).exists()


def test_stop_during_apply_rolls_back(organizer, monkeypatch):
    a = put(organizer, 'a.txt')
    b = put(organizer, 'b.txt')
    preview = organizer.preview('projects/demo')
    original = organizer._move
    count = 0
    def interrupt(source, destination):
        nonlocal count
        original(source, destination)
        count += 1
        if count == 1:
            (organizer.guard.workspace / '.jarvis-stop').touch()
    monkeypatch.setattr(organizer, '_move', interrupt)
    with pytest.raises(OrganizationError, match='rollback completed'):
        organizer.apply(preview['preview_id'])
    assert a.exists() and b.exists()


@pytest.mark.parametrize('project', ['../outside', '.', 'projects/demo/nested', 'documents', True])
def test_scope_and_strict_types(organizer, project):
    with pytest.raises((OrganizationError, ValidationError, GuardrailError)):
        organizer.preview(project)


def test_tampered_history_cannot_move_outside(organizer):
    put(organizer, 'a.txt')
    operation = organizer.apply(organizer.preview('projects/demo')['preview_id'])
    path = organizer.guard.workspace / '.jarvis-organization' / (operation['operation_id'] + '.json')
    data = json.loads(path.read_text())
    data['moves'][0]['destination'] = '../../outside.txt'
    path.write_text(json.dumps(data))
    with pytest.raises((OrganizationError, ValueError)):
        organizer.preview_undo(operation['operation_id'])


def test_noop_and_duplicate_names_are_only_review_flags(organizer):
    put(organizer, 'main.py')
    assert organizer.apply(organizer.preview('projects/demo')['preview_id'])['status'] == 'no_changes'
    put(organizer, '보고서 최종 (1).pdf')
    preview = organizer.preview('projects/demo')
    assert preview['name_review'][0]['reason'] == 'name_only_review_not_proven_duplicate'


def test_downloads_optin_and_metadata_only(organizer, tmp_path, monkeypatch):
    monkeypatch.delenv('JARVIS_DOWNLOADS_READONLY', raising=False)
    with pytest.raises(OrganizationError, match='disabled'):
        organizer.preview_downloads()
    folder = tmp_path / 'downloads'
    folder.mkdir()
    (folder / 'report (1).pdf').write_bytes(b'sensitive content must not be opened')
    (folder / 'partial.crdownload').write_bytes(b'in progress')
    (folder / 'folder').mkdir()
    monkeypatch.setenv('JARVIS_DOWNLOADS_READONLY', '1')
    monkeypatch.setattr(file_organizer, 'downloads_directory', lambda: folder)
    def never_open(*args, **kwargs):
        raise AssertionError('Downloads file contents must not be opened')
    monkeypatch.setattr(Path, 'open', never_open)
    result = organizer.preview_downloads()
    assert result['file_count'] == 2
    assert result['can_apply'] is False
    assert result['category_counts'] == {'Documents': 1, 'ManualReview': 1}
    assert result['skipped'][0]['path'] == 'folder'
    assert not (folder / 'Organized').exists()


def test_downloads_scan_limit(organizer, tmp_path, monkeypatch):
    folder = tmp_path / 'downloads'
    folder.mkdir()
    (folder / 'a.pdf').touch()
    monkeypatch.setenv('JARVIS_DOWNLOADS_READONLY', '1')
    monkeypatch.setattr(file_organizer, 'downloads_directory', lambda: folder)
    monkeypatch.setattr(organizer, 'MAX_DOWNLOAD_ENTRIES', 0)
    with pytest.raises(OrganizationError, match='scan limit'):
        organizer.preview_downloads()


def test_new_tools_obey_shared_stop(organizer):
    mcp = server.create_server(organizer.guard)
    (organizer.guard.workspace / '.jarvis-stop').touch()
    for name, arguments in [
        ('preview_file_organization', {'project': 'projects/demo'}),
        ('apply_file_organization', {'preview_id': 'a' * 32}),
        ('list_file_organization_operations', {}),
        ('preview_file_organization_undo', {'operation_id': 'a' * 32}),
        ('undo_file_organization', {'preview_id': 'a' * 32}),
        ('preview_downloads_organization', {}),
    ]:
        with pytest.raises(ToolError, match='stopped'):
            asyncio.run(mcp.call_tool(name, arguments))


def test_real_mcp_organization_flow(organizer):
    source = put(organizer, 'report.pdf', b'report')
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__).resolve())],
                                       env={**os.environ, 'JARVIS_WORKSPACE': str(organizer.guard.workspace),
                                            'JARVIS_DOWNLOADS_READONLY': '0'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, response
                    return json.loads(response.content[0].text)
                preview = await call('preview_file_organization', {'project': 'projects/demo'})
                applied = await call('apply_file_organization', {'preview_id': preview['preview_id']})
                assert not source.exists()
                undo_preview = await call('preview_file_organization_undo', {'operation_id': applied['operation_id']})
                assert (await call('undo_file_organization', {'preview_id': undo_preview['preview_id']}))['status'] == 'undone'
                assert source.read_bytes() == b'report'
                assert len((await call('list_file_organization_operations', {}))['operations']) == 1
                assert (await session.call_tool('preview_downloads_organization', {})).isError
    asyncio.run(exercise())
