"""Preview-bound, backed-up file organization and undo; Downloads is metadata-only."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis.features.snapshots.service import SnapshotStore


class OrganizationError(ValueError):
    """Reportable organization policy or recovery error."""


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class ProjectRequest(Record):
    project: str = Field(pattern=r'^projects/[^/\\]+$', max_length=800)


class IdRequest(Record):
    id: str = Field(pattern=r'^[0-9a-f]{32}$')


class Move(Record):
    source: str
    destination: str
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=0, le=5_000_000)
    mtime_ns: int = Field(ge=0)


class Operation(Record):
    version: Literal[1] = 1
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    project: str
    created_at: str
    status: Literal['applying', 'complete', 'rolled_back', 'rollback_failed',
                    'undoing', 'undone', 'undo_rolled_back', 'undo_rollback_failed']
    moves: list[Move] = Field(max_length=500)
    backup_id: str
    undo_backup_id: str | None = None
    applied: list[str] = Field(default_factory=list)


CATEGORIES = {
    'Documents': {'.pdf', '.doc', '.docx', '.txt', '.md', '.rtf', '.ppt', '.pptx', '.hwp', '.hwpx'},
    'Spreadsheets': {'.xls', '.xlsx', '.csv', '.tsv', '.ods'},
    'Images': {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.heic'},
    'Audio': {'.mp3', '.wav', '.flac', '.m4a', '.ogg'},
    'Video': {'.mp4', '.mkv', '.mov', '.avi', '.webm'},
    'Archives': {'.zip', '.7z', '.rar', '.tar', '.gz'},
    'Installers': {'.exe', '.msi', '.msix', '.dmg', '.pkg'},
}
PROTECTED = {'readme.md', 'license', 'license.txt', 'agents.md', 'agents_context.md'}


def classify(name):
    lower = name.casefold()
    if (lower in PROTECTED or lower.startswith(('requirements', 'client_secret'))
            or SnapshotStore._excluded(name)):
        return None, 'protected'
    extension = Path(name).suffix.casefold()
    if extension in {'.tmp', '.part', '.crdownload', '.download'}:
        return None, 'incomplete_download'
    for category, extensions in CATEGORIES.items():
        if extension in extensions:
            return category, None
    return None, 'unknown_or_code_file'


def downloads_directory():
    """Resolve only the OS Downloads known folder, never an arbitrary tool argument."""
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            value, _ = winreg.QueryValueEx(key, '{374DE290-123F-4565-9164-39C4925E467B}')
        return Path(os.path.expandvars(value))
    return Path.home() / 'Downloads'


class FileOrganizer:
    TTL = 600
    MAX_OPERATIONS = 100
    MAX_DOWNLOAD_ENTRIES = 5000

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.guard = snapshots.guard
        self.lock = threading.RLock()
        self.previews = {}

    def _id(self, value):
        return IdRequest(id=value).id

    def _project(self, value):
        return self.snapshots._project(ProjectRequest(project=value).project, exists=True)

    def _save_preview(self, data):
        now = time.monotonic()
        self.previews = {key: item for key, item in self.previews.items() if item['expires'] > now}
        if len(self.previews) >= 100:
            raise OrganizationError('Too many pending organization previews.')
        preview_id = uuid4().hex
        self.previews[preview_id] = {**data, 'expires': now + self.TTL}
        return preview_id

    def _take_preview(self, preview_id, kind):
        plan = self.previews.pop(self._id(preview_id), None)
        if plan is None or plan['expires'] <= time.monotonic() or plan['kind'] != kind:
            raise OrganizationError('Preview expired, already used or wrong operation type; preview again.')
        return plan

    def _paths(self, project, move):
        root = self.snapshots._project(ProjectRequest(project=project).project)
        if '/' in move.source or '\\' in move.source:
            raise OrganizationError('Organization source must be a top-level project file.')
        self.snapshots._member(move.source)
        self.snapshots._member(move.destination)
        parts = move.destination.split('/')
        if (len(parts) != 4 or parts[0] != 'Organized' or parts[1] not in CATEGORIES
                or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', parts[2]) or parts[3] != move.source):
            raise OrganizationError('Invalid organization destination.')
        return (self.guard.validate_path(str(root / move.source), must_exist=False),
                self.guard.validate_path(str(root / move.destination), must_exist=False))

    def _check(self, project, moves, *, reverse=False):
        for move in moves:
            source, destination = self._paths(project, move)
            if reverse:
                source, destination = destination, source
            if destination.exists():
                raise OrganizationError('Destination already exists; no overwrite is allowed.')
            raw = self.snapshots._read(source, self.snapshots.MAX_FILE_BYTES)
            info = source.stat()
            if (len(raw) != move.size or self.snapshots._digest(raw) != move.sha256
                    or info.st_mtime_ns != move.mtime_ns):
                raise OrganizationError('File changed since preview or last operation; preview/review again.')

    def preview(self, project):
        with self.lock, self.snapshots.lock:
            root = self._project(project)
            files, excluded = self.snapshots._scan(project)
            moves, skipped, collisions, review = [], [], [], []
            for name, raw in sorted(files.items()):
                if '/' in name:
                    continue
                category, reason = classify(name)
                if category is None:
                    skipped.append({'path': name, 'reason': reason})
                    continue
                info = (root / name).stat()
                month = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime('%Y-%m')
                move = Move(source=name, destination=f'Organized/{category}/{month}/{name}',
                            sha256=self.snapshots._digest(raw), size=len(raw), mtime_ns=info.st_mtime_ns)
                _, destination = self._paths(project, move)
                if destination.exists():
                    collisions.append(name)
                if re.search(r'최종|복사본|copy|\(\d+\)', name, re.I):
                    review.append({'path': name, 'reason': 'name_only_review_not_proven_duplicate'})
                moves.append(move)
            preview_id = self._save_preview({'kind': 'apply', 'project': project, 'moves': moves,
                                             'collisions': collisions})
            return {'preview_id': preview_id, 'project': project, 'expires_in_seconds': self.TTL,
                    'moves': [item.model_dump() for item in moves], 'collisions': collisions,
                    'skipped': skipped, 'excluded': excluded, 'name_review': review,
                    'scope': 'Top-level files only; extension and UTC modification month. No duplicate deletion.'}

    def _store(self):
        path = self.guard.validate_path('.jarvis-organization', must_exist=False)
        path.mkdir(exist_ok=True)
        return path

    def _read_operation(self, operation_id):
        path = self._store() / (self._id(operation_id) + '.json')
        operation = Operation.model_validate_json(self.snapshots._read(path, 2_000_000))
        if operation.id != operation_id:
            raise OrganizationError('Operation identifier mismatch.')
        for move in operation.moves:
            self._paths(operation.project, move)
        return operation

    def _persist(self, operation):
        folder = self._store()
        temporary = folder / (uuid4().hex + '.tmp')
        self.snapshots._write(temporary, operation.model_dump_json().encode())
        self.snapshots._replace(temporary, folder / (operation.id + '.json'))

    def operations(self):
        with self.lock:
            paths = sorted(self._store().glob('*.json'))
            if len(paths) > self.MAX_OPERATIONS:
                raise OrganizationError('Organization history capacity exceeded.')
            records = [self._read_operation(path.stem) for path in paths]
            return {'operations': [{'id': item.id, 'project': item.project, 'status': item.status,
                                    'file_count': len(item.moves), 'backup_id': item.backup_id,
                                    'created_at': item.created_at} for item in records],
                    'pending_recoveries': [item.id for item in records
                                           if item.status in {'applying', 'undoing', 'rollback_failed', 'undo_rollback_failed'}]}

    def _ready(self):
        history = self.operations()
        if history['pending_recoveries'] or self.snapshots._pending():
            raise OrganizationError('Unfinished recovery exists; operator review required.')
        return history

    def _move(self, source, destination):
        if os.name != 'nt':
            raise OrganizationError('File moves currently require Windows no-overwrite rename semantics.')
        source = self.guard.validate_path(str(source))
        destination = self.guard.validate_path(str(destination), must_exist=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.guard.validate_path(str(destination.parent))
        # Windows rename fails when a destination exists; unlike POSIX rename it does not replace it.
        os.rename(source, destination)

    def _execute(self, operation, *, reverse=False):
        completed = []
        operation.status = 'undoing' if reverse else 'applying'
        operation.applied = []
        self._persist(operation)
        try:
            for move in operation.moves:
                self.guard.check_running()
                self._check(operation.project, [move], reverse=reverse)
                source, destination = self._paths(operation.project, move)
                if reverse:
                    source, destination = destination, source
                self._move(source, destination)
                completed.append(move)
                operation.applied.append(move.source)
                self._persist(operation)
        except Exception as exc:
            failures = []
            for move in reversed(completed):
                try:
                    self._check(operation.project, [move], reverse=not reverse)
                    source, destination = self._paths(operation.project, move)
                    if reverse:
                        source, destination = destination, source
                    self._move(destination, source)
                except Exception:
                    failures.append(move.source)
            operation.status = ('undo_rollback_failed' if failures else 'undo_rolled_back') if reverse else (
                'rollback_failed' if failures else 'rolled_back')
            self._persist(operation)
            raise OrganizationError(f'Operation failed; rollback {"incomplete" if failures else "completed"}; '
                                    f'operation_id={operation.id}') from exc
        operation.status = 'undone' if reverse else 'complete'
        self._persist(operation)
        return {'operation_id': operation.id, 'status': operation.status, 'moved_files': len(completed),
                'backup_id': operation.undo_backup_id if reverse else operation.backup_id}

    def apply(self, preview_id):
        with self.lock, self.snapshots.lock:
            plan = self._take_preview(preview_id, 'apply')
            history = self._ready()
            if len(history['operations']) >= self.MAX_OPERATIONS:
                raise OrganizationError('History capacity reached; operator archival required.')
            if plan['collisions']:
                raise OrganizationError('Preview contains destination collisions; resolve them and preview again.')
            self._check(plan['project'], plan['moves'])
            if not plan['moves']:
                return {'status': 'no_changes', 'moved_files': 0}
            backup = self.snapshots.create(plan['project'])
            self._check(plan['project'], plan['moves'])
            operation = Operation(id=uuid4().hex, project=plan['project'], status='applying',
                                  moves=plan['moves'], backup_id=backup['id'],
                                  created_at=datetime.now(timezone.utc).isoformat())
            return self._execute(operation)

    def preview_undo(self, operation_id):
        with self.lock, self.snapshots.lock:
            self._ready()
            operation = self._read_operation(operation_id)
            if operation.status not in {'complete', 'undo_rolled_back'}:
                raise OrganizationError('Only completed organization operations can be undone.')
            self._check(operation.project, operation.moves, reverse=True)
            preview_id = self._save_preview({'kind': 'undo', 'operation': operation.model_dump()})
            return {'preview_id': preview_id, 'operation_id': operation.id, 'expires_in_seconds': self.TTL,
                    'moves': [{'source': item.destination, 'destination': item.source} for item in operation.moves]}

    def undo(self, preview_id):
        with self.lock, self.snapshots.lock:
            plan = self._take_preview(preview_id, 'undo')
            self._ready()
            operation = self._read_operation(plan['operation']['id'])
            if operation.model_dump() != plan['operation']:
                raise OrganizationError('Operation changed since undo preview.')
            self._check(operation.project, operation.moves, reverse=True)
            operation.undo_backup_id = self.snapshots.create(operation.project)['id']
            self._check(operation.project, operation.moves, reverse=True)
            return self._execute(operation, reverse=True)

    def preview_downloads(self):
        if os.environ.get('JARVIS_DOWNLOADS_READONLY') != '1':
            raise OrganizationError('Downloads metadata scan is disabled. Operator must set JARVIS_DOWNLOADS_READONLY=1.')
        folder = downloads_directory()
        if not folder.is_absolute() or folder.is_symlink() or folder.is_junction() or not folder.is_dir():
            raise OrganizationError('Downloads folder must be an existing non-linked local directory.')
        if str(folder).startswith(('\\\\', '//')):
            raise OrganizationError('Network Downloads folders are not supported.')
        items, skipped = [], []
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                self.guard.check_running()
                if index >= self.MAX_DOWNLOAD_ENTRIES:
                    raise OrganizationError('Downloads scan limit exceeded; no complete report produced.')
                path = Path(entry.path)
                if entry.is_symlink() or path.is_junction() or not entry.is_file(follow_symlinks=False):
                    skipped.append({'path': entry.name, 'reason': 'folder_or_link_not_scanned'})
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    skipped.append({'path': entry.name, 'reason': 'metadata_unavailable'})
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                category, reason = classify(entry.name)
                month = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime('%Y-%m')
                flags = []
                if re.search(r'최종|복사본|copy|\(\d+\)', entry.name, re.I):
                    flags.append('name_review_not_proven_duplicate')
                items.append({'path': entry.name, 'size_bytes': info.st_size,
                              'modified_at': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                              'category': category, 'reason': reason, 'flags': flags,
                              'proposed_destination': f'Organized/{category}/{month}/{entry.name}' if category else None})
        items.sort(key=lambda item: item['path'].casefold())
        return {'mode': 'read_only_metadata_simulation', 'source': str(folder), 'files': items,
                'skipped': skipped, 'file_count': len(items), 'total_bytes': sum(x['size_bytes'] for x in items),
                'category_counts': dict(Counter(x['category'] or 'ManualReview' for x in items)),
                'can_apply': False, 'scope': 'Top-level only. No file contents, hashes, moves or deletions.'}
