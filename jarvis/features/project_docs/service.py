"""Read-only source over the checkout's docs/ for the RAG project_docs collection (board 2e3093a2 part 5).

docs/ is outside JarvisWorkspace, so the workspace guard cannot reach it. This is a separate, narrower read root:
- The folder is fixed in code (PROJECT_ROOT/docs). No setting, request or model chooses it.
- At construction it must be an existing folder, reached without links or junctions, whose resolved path equals the
  written one, and that is not the workspace, inside it, or a folder containing it.
- Only lower-case '.md' files under it, by a relative path without '..', hidden ('.') parts, ':' (streams), drive or
  control characters. Every part from the root down is checked with lstat (no link, junction or reparse point); the
  file is opened read-only, and fstat must show a regular file with one link, within the size cap, and the same file
  that lstat saw (no swap in between).
- walk() lists such files with os.walk(followlinks=False), skipping hidden or linked folders, within entry caps.
There is no method that writes, moves or deletes; the index built from it lives in the workspace.
"""
import os
from pathlib import Path, PurePosixPath
import re
import stat

from jarvis.core.paths import PROJECT_ROOT

COLLECTION = 'project_docs'
MAX_FILE_BYTES = 512_000
MAX_FILES = 400
MAX_ENTRIES = 4000
BAD_PART = re.compile(r'[<>:"|?*%$\\\x00-\x1f]')


class ProjectDocsError(ValueError):
    """A path or file the project docs root refuses; the message never quotes file content."""


def _linked(path):
    """True for a symlink, junction or other reparse point (Windows), checked without following it."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, 'st_file_attributes', 0)
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


class ProjectDocsRoot:
    collection = COLLECTION
    label = '개발 문서'
    prefix = 'docs'                    # cited paths read docs/<relative>

    skipped = ()

    def __init__(self, workspace, root=None):
        written = Path(root) if root is not None else PROJECT_ROOT / 'docs'
        if not written.is_absolute():
            raise ProjectDocsError('The project docs root must be an absolute folder.')
        written = Path(os.path.abspath(written))
        if any(_linked(part) for part in (written, *written.parents) if part != Path(part.anchor)):
            raise ProjectDocsError('The project docs root must be reached without links or junctions.')
        try:
            resolved = written.resolve(strict=True)
        except OSError:
            raise ProjectDocsError('The project docs folder does not exist.') from None
        if not resolved.is_dir() or os.path.normcase(str(resolved)) != os.path.normcase(str(written)):
            raise ProjectDocsError('The project docs root must be a plain folder.')
        workspace = Path(workspace).resolve()
        if resolved == workspace or resolved.is_relative_to(workspace) or workspace.is_relative_to(resolved):
            raise ProjectDocsError('The project docs root must be apart from the workspace.')
        self.root = resolved

    # --- paths ----------------------------------------------------------------------------------------------
    def _path(self, relative):
        if not isinstance(relative, str) or not 0 < len(relative) <= 400 or '\\' in relative:
            raise ProjectDocsError('Use a relative docs path with forward slashes.')
        parts = PurePosixPath(relative).parts
        if (not parts or PurePosixPath(relative).is_absolute() or relative != '/'.join(parts)
                or any(part in ('.', '..') or part.startswith('.') or part != part.strip() or BAD_PART.search(part)
                       for part in parts)):
            raise ProjectDocsError('Only plain relative paths inside docs/ are allowed.')
        if PurePosixPath(parts[-1]).suffix != '.md':
            raise ProjectDocsError('Only Markdown (.md) project docs are read.')
        path = self.root.joinpath(*parts)
        current = self.root
        for part in parts:
            current = current / part
            if _linked(current):
                raise ProjectDocsError('Linked or missing project doc path.')
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise ProjectDocsError('Project doc not found.') from None
        if resolved != path or not resolved.is_relative_to(self.root):
            raise ProjectDocsError('Project doc path does not stay inside docs/.')
        return path

    def cite(self, relative):
        """The path shown in citations: docs/<relative>."""
        return f'{self.prefix}/{relative}'

    # --- reading --------------------------------------------------------------------------------------------
    def read(self, relative):
        """The bytes of one docs Markdown file, read-only, after every check above."""
        path = self._path(relative)
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise ProjectDocsError('Project doc is not a regular file.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
                raise ProjectDocsError('Project doc changed or is not a plain file.')
            if info.st_size > MAX_FILE_BYTES:
                raise ProjectDocsError(f'Project doc exceeds the {MAX_FILE_BYTES} byte limit.')
            with os.fdopen(descriptor, 'rb', closefd=True) as stream:
                descriptor = None
                raw = stream.read(MAX_FILE_BYTES + 1)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(raw) > MAX_FILE_BYTES:
            raise ProjectDocsError(f'Project doc exceeds the {MAX_FILE_BYTES} byte limit.')
        return raw

    def walk(self):
        """Sorted relative paths of the readable .md files (the store's Source shape); what was left out and why is
        kept in self.skipped. Raises when a cap is passed, so the store keeps its previous index."""
        found, skipped, entries = [], [], 0
        self.skipped = skipped

        def failed(error):
            raise ProjectDocsError('Project docs could not be listed.') from error
        for parent, dirs, names in os.walk(self.root, followlinks=False, onerror=failed):
            dirs.sort()
            names.sort()
            kept = []
            for name in dirs:
                entries += 1
                if name.startswith('.'):
                    continue
                if _linked(Path(parent) / name):
                    skipped.append({'path': self._relative(Path(parent) / name), 'reason': 'linked folder'})
                    continue
                kept.append(name)
            dirs[:] = kept
            for name in names:
                entries += 1
                if name.startswith('.') or not name.endswith('.md'):
                    continue
                relative = self._relative(Path(parent) / name)
                try:
                    self._path(relative)
                except ProjectDocsError as exc:
                    skipped.append({'path': relative, 'reason': str(exc)[:80]})
                    continue
                found.append(relative)
            if entries > MAX_ENTRIES or len(found) > MAX_FILES:
                raise ProjectDocsError('Too many project docs to index; nothing was changed.')
        return sorted(found)

    def _relative(self, path):
        return Path(path).relative_to(self.root).as_posix()
