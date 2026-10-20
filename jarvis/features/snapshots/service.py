"""Bounded ZIP snapshots and preview-bound merge restore within workspace projects/."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
from uuid import uuid4
import zipfile

from jarvis.core.guardrails import Guardrails


class SnapshotError(ValueError):
    """A snapshot or restore was rejected or failed with a reportable recovery state."""


class SnapshotStore:
    MAX_FILES = 500
    MAX_FILE_BYTES = 5_000_000
    MAX_TOTAL_BYTES = 25_000_000
    MAX_ARCHIVE_BYTES = 30_000_000
    MAX_SNAPSHOTS = 100
    MAX_ENTRIES = 2000
    PREVIEW_TTL = 600
    EXCLUDED = {'.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build', 'sources'}

    def __init__(self, guard: Guardrails):
        self.guard = guard
        self.lock = threading.RLock()
        self.previews = {}

    @staticmethod
    def _digest(raw):
        return hashlib.sha256(raw).hexdigest()

    def _path(self, relative, *, exists=False):
        return self.guard.validate_path(relative, must_exist=exists)

    def _read(self, path, limit):
        path = self._path(str(path), exists=True)
        if not path.is_file():
            raise SnapshotError('A file path conflicts with a directory or special entry.')
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SnapshotError('Only regular non-hardlinked files are supported.')
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise SnapshotError('File exceeds snapshot size limit.')
        return raw

    def _write(self, path, raw):
        path = self._path(str(path))
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def _excluded(cls, relative):
        return any(part.startswith('.') or part.casefold() in cls.EXCLUDED
                   or part.casefold().startswith('client_secret')
                   or part.casefold().endswith(('.pem', '.key', '.p12', '.pfx'))
                   for part in PurePosixPath(relative).parts)

    @classmethod
    def _member(cls, value):
        if (not isinstance(value, str) or not value or len(value) > 800
                or '\\' in value or any(c in value for c in ':*?%$')
                or any(ord(c) < 32 for c in value)):
            raise SnapshotError('Unsafe snapshot member path.')
        parts = value.split('/')
        if any(part in {'', '.', '..'} or part.endswith((' ', '.'))
               or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)
               for part in parts) or cls._excluded(value):
            raise SnapshotError('Unsafe or excluded snapshot member path.')
        return value

    def _project(self, project, *, exists=False):
        if not isinstance(project, str):
            raise SnapshotError('Project must be projects/<name>.')
        self._member(project)
        if len(project.split('/')) != 2 or not project.startswith('projects/'):
            raise SnapshotError('Only projects/<name> inside JarvisWorkspace is supported.')
        path = self._path(project, exists=exists)
        if path.exists() and not path.is_dir():
            raise SnapshotError('Project path must be a directory.')
        return path

    def _vault(self):
        path = self._path('.jarvis-snapshots')
        path.mkdir(exist_ok=True)
        return path

    def _id(self, value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
            raise SnapshotError('Invalid snapshot or preview identifier.')
        return value

    def _scan(self, project):
        root = self._project(project, exists=True)
        files, excluded = {}, []
        total = visited = 0

        def failed(exc):
            raise SnapshotError('Project traversal failed; snapshot not created.') from exc

        for parent, dirs, names in os.walk(root, followlinks=False, onerror=failed):
            allowed = []
            for name in sorted(dirs):
                visited += 1
                rel = (Path(parent) / name).relative_to(root).as_posix()
                if visited > self.MAX_ENTRIES or len(PurePosixPath(rel).parts) > 20:
                    raise SnapshotError('Project scan limit exceeded.')
                if self._excluded(rel):
                    excluded.append(rel + '/')
                    continue
                self._path(str(Path(parent) / name), exists=True)
                allowed.append(name)
            dirs[:] = allowed
            for name in sorted(names):
                self.guard.check_running()
                visited += 1
                if visited > self.MAX_ENTRIES:
                    raise SnapshotError('Project scan limit exceeded.')
                rel = (Path(parent) / name).relative_to(root).as_posix()
                if self._excluded(rel):
                    excluded.append(rel)
                    continue
                self._member(rel)
                raw = self._read(Path(parent) / name, self.MAX_FILE_BYTES)
                files[rel] = raw
                total += len(raw)
                if len(files) > self.MAX_FILES or total > self.MAX_TOTAL_BYTES:
                    raise SnapshotError('Project file count or total size limit exceeded.')
        return files, excluded

    def create(self, project):
        with self.lock:
            files, excluded = self._scan(project)
            vault = self._vault()
            if max(len(list(vault.glob('*.json'))), len(list(vault.glob('*.zip')))) >= self.MAX_SNAPSHOTS:
                raise SnapshotError('Snapshot capacity reached; operator archival is required.')
            snapshot_id = uuid4().hex
            if len({name.casefold() for name in files}) != len(files):
                raise SnapshotError('Case-colliding project paths are unsupported.')
            manifest = {'version': 1, 'id': snapshot_id, 'project': project,
                        'created_at': datetime.now(timezone.utc).isoformat(), 'excluded': excluded,
                        'files': [{'path': name, 'size': len(raw), 'sha256': self._digest(raw)}
                                  for name, raw in sorted(files.items())]}
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                for name, raw in sorted(files.items()):
                    archive.writestr('files/' + name, raw)
            raw_archive = buffer.getvalue()
            if len(raw_archive) > self.MAX_ARCHIVE_BYTES:
                raise SnapshotError('ZIP size limit exceeded.')
            metadata = {key: manifest[key] for key in ('id', 'project', 'created_at', 'excluded')}
            metadata.update(file_count=len(files), total_bytes=sum(map(len, files.values())),
                            archive_sha256=self._digest(raw_archive))
            metadata_bytes = json.dumps(metadata).encode()
            if len(metadata_bytes) > 500_000:
                raise SnapshotError('Snapshot metadata size limit exceeded.')
            self._write(vault / (snapshot_id + '.zip'), raw_archive)
            self._write(vault / (snapshot_id + '.json'), metadata_bytes)
            return metadata

    def _load(self, snapshot_id):
        self._id(snapshot_id)
        vault = self._vault()
        metadata = json.loads(self._read(vault / (snapshot_id + '.json'), 500_000))
        raw = self._read(vault / (snapshot_id + '.zip'), self.MAX_ARCHIVE_BYTES)
        if self._digest(raw) != metadata.get('archive_sha256'):
            raise SnapshotError('Snapshot archive checksum mismatch.')
        files = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > self.MAX_FILES + 1 or len({m.filename.casefold() for m in members}) != len(members):
                raise SnapshotError('Duplicate members or archive member limit exceeded.')
            for member in members:
                mode = member.external_attr >> 16
                if member.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise SnapshotError('ZIP links and special entries are forbidden.')
                if member.file_size > self.MAX_FILE_BYTES:
                    raise SnapshotError('ZIP member exceeds size limit.')
            manifest = json.loads(archive.read('manifest.json'))
            if (manifest.get('version') != 1 or manifest.get('id') != snapshot_id
                    or manifest.get('project') != metadata.get('project')):
                raise SnapshotError('Invalid snapshot manifest.')
            self._project(manifest['project'])
            records = manifest.get('files')
            if not isinstance(records, list) or len(records) > self.MAX_FILES:
                raise SnapshotError('Invalid file manifest.')
            expected = {'manifest.json'}
            total = 0
            for record in records:
                name = self._member(record['path'])
                if name in files:
                    raise SnapshotError('Duplicate manifest file.')
                expected.add('files/' + name)
                size = record['size']
                if type(size) is not int or not 0 <= size <= self.MAX_FILE_BYTES:
                    raise SnapshotError('Invalid file size.')
                total += size
                if total > self.MAX_TOTAL_BYTES:
                    raise SnapshotError('Expanded archive size limit exceeded.')
                info = archive.getinfo('files/' + name)
                if info.file_size != size:
                    raise SnapshotError('ZIP member size mismatch.')
                with archive.open(info) as stream:
                    data = stream.read(size + 1)
                if len(data) != size or self._digest(data) != record['sha256']:
                    raise SnapshotError('Snapshot file checksum mismatch.')
                files[name] = data
            if {m.filename for m in members} != expected:
                raise SnapshotError('Unexpected ZIP members.')
            for name in files:
                if any(parent.as_posix().casefold() in {key.casefold() for key in files}
                       for parent in PurePosixPath(name).parents if parent.as_posix() != '.'):
                    raise SnapshotError('Conflicting file and directory paths in archive.')
        return metadata, files

    def _state(self, project, names):
        root = self._project(project)
        state = {}
        for name in sorted(names):
            path = self._path(str(root / name))
            state[name] = self._digest(self._read(path, self.MAX_FILE_BYTES)) if path.exists() else None
        return state

    def _pending(self):
        recovery = self._path('.jarvis-snapshots/recovery')
        if not recovery.exists():
            return []
        pending = []
        entries = list(recovery.iterdir())
        if len(entries) > self.MAX_SNAPSHOTS:
            raise SnapshotError('Recovery storage limit exceeded.')
        for path in sorted(entries):
            self._id(path.name)
            journal = self._path(str(path / 'journal.json'))
            if not journal.exists():
                pending.append(path.name)
            elif json.loads(self._read(journal, 2_000_000)).get('status') not in {'complete', 'rolled_back'}:
                pending.append(path.name)
        return pending

    def list(self):
        with self.lock:
            vault = self._vault()
            paths = sorted(vault.glob('*.json'))
            if len(paths) > self.MAX_SNAPSHOTS:
                raise SnapshotError('Snapshot capacity exceeded.')
            items = []
            for path in paths:
                self._id(path.stem)
                metadata = json.loads(self._read(path, 500_000))
                items.append({key: metadata[key] for key in ('id', 'project', 'created_at', 'file_count', 'total_bytes')})
            return {'snapshots': items, 'pending_recoveries': self._pending()}

    def preview(self, snapshot_id):
        with self.lock:
            metadata, files = self._load(snapshot_id)
            if self._pending():
                raise SnapshotError('Unfinished recovery exists; operator review required before restore.')
            state = self._state(metadata['project'], files)
            changes = [{'path': name, 'action': 'create' if state[name] is None else 'replace'}
                       for name, raw in files.items() if state[name] != self._digest(raw)]
            now = time.monotonic()
            self.previews = {key: value for key, value in self.previews.items() if value['expires'] > now}
            if len(self.previews) >= 100:
                raise SnapshotError('Too many pending previews.')
            preview_id = uuid4().hex
            self.previews[preview_id] = {'snapshot_id': snapshot_id, 'archive_hash': metadata['archive_sha256'],
                                        'project': metadata['project'], 'state': state,
                                        'expires': now + self.PREVIEW_TTL}
            return {'preview_id': preview_id, 'project': metadata['project'], 'changes': changes,
                    'excluded': metadata['excluded'], 'expires_in_seconds': self.PREVIEW_TTL,
                    'policy': 'Merge restore only; extra and excluded current files are preserved.'}

    def restore(self, preview_id):
        with self.lock:
            self._id(preview_id)
            plan = self.previews.pop(preview_id, None)
            if plan is None or plan['expires'] <= time.monotonic():
                raise SnapshotError('Restore preview expired or already used; preview again.')
            if self._pending():
                raise SnapshotError('Unfinished recovery exists; operator review required.')
            metadata, files = self._load(plan['snapshot_id'])
            if metadata['archive_sha256'] != plan['archive_hash'] or self._state(plan['project'], files) != plan['state']:
                raise SnapshotError('Snapshot or target changed since preview; preview again.')
            changed = {name: raw for name, raw in files.items() if self._digest(raw) != plan['state'][name]}
            if not changed:
                return {'restored_files': 0, 'project': plan['project'], 'extra_files_preserved': True}
            root = self._project(plan['project'])
            backup = self.create(plan['project']) if root.exists() else None
            vault = self._vault()
            recovery_root = self._path(str(vault / 'recovery'))
            recovery_root.mkdir(exist_ok=True)
            if len(list(recovery_root.iterdir())) >= self.MAX_SNAPSHOTS:
                raise SnapshotError('Recovery capacity reached; operator archival required.')
            recovery_id = uuid4().hex
            recovery = self._path(str(recovery_root / recovery_id))
            recovery.mkdir()
            journal = {'status': 'preparing', 'project': plan['project'], 'snapshot_id': plan['snapshot_id'],
                       'backup_id': backup['id'] if backup else None, 'original_state': plan['state'],
                       'planned': list(changed), 'applied': []}
            self._journal(recovery, journal)
            try:
                originals = self._stage(recovery, root, changed, plan['state'])
            except Exception as exc:
                journal['status'] = 'rolled_back'
                self._journal(recovery, journal)
                raise SnapshotError('Restore staging failed; no project files changed.') from exc
            if self._state(plan['project'], files) != plan['state']:
                journal['status'] = 'rolled_back'
                self._journal(recovery, journal)
                raise SnapshotError('Target changed while staging; no project files changed.')
            journal['status'] = 'applying'
            self._journal(recovery, journal)
            applied = []
            try:
                for index, (name, raw) in enumerate(changed.items()):
                    self.guard.check_running()
                    destination = self._path(str(root / name))
                    if self._state(plan['project'], [name])[name] != plan['state'][name]:
                        raise SnapshotError('Target changed during restore.')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._path(str(destination.parent), exists=True)
                    self._replace(recovery / f'new-{index}', destination)
                    applied.append((index, name))
                    journal['applied'].append(name)
                    self._journal(recovery, journal)
            except Exception as exc:
                failures = []
                for index, name in reversed(applied):
                    try:
                        destination = self._path(str(root / name), exists=True)
                        if self._digest(self._read(destination, self.MAX_FILE_BYTES)) != self._digest(changed[name]):
                            raise SnapshotError('Restored file was concurrently changed.')
                        if originals[name] is None:
                            destination.unlink()
                        else:
                            self._replace(recovery / f'old-{index}', destination)
                    except Exception:
                        failures.append(name)
                journal.update(status='rollback_failed' if failures else 'rolled_back', rollback_failures=failures)
                self._journal(recovery, journal)
                raise SnapshotError(f'Restore failed; rollback {"incomplete" if failures else "completed"}; '
                                    f'recovery_id={recovery_id}') from exc
            journal['status'] = 'complete'
            self._journal(recovery, journal)
            return {'restored_files': len(changed), 'project': plan['project'], 'recovery_id': recovery_id,
                    'backup_id': backup['id'] if backup else None, 'extra_files_preserved': True}

    def _journal(self, recovery, data):
        temp = self._path(str(recovery / (uuid4().hex + '.tmp')))
        self._write(temp, json.dumps(data).encode())
        self._replace(temp, self._path(str(recovery / 'journal.json')))

    def _stage(self, recovery, root, changed, state):
        originals = {}
        for index, (name, raw) in enumerate(changed.items()):
            self.guard.check_running()
            current = self._path(str(root / name))
            original = self._read(current, self.MAX_FILE_BYTES) if current.exists() else None
            if (self._digest(original) if original is not None else None) != state[name]:
                raise SnapshotError('Target changed while staging.')
            originals[name] = original
            self._write(recovery / f'new-{index}', raw)
            if original is not None:
                self._write(recovery / f'old-{index}', original)
        return originals

    def _replace(self, source, destination):
        source = self._path(str(source), exists=True)
        destination = self._path(str(destination))
        os.replace(source, destination)
