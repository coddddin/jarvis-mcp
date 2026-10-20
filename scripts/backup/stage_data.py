"""Copy JARVIS data into a staging folder for encrypted backup.

SQLite databases are copied with the online backup API and integrity-checked,
so a running server never produces a torn copy. Links are never followed.
Usage: python stage_data.py <project_root> <staging_dir>
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

SOURCES = ('JarvisWorkspace', '.jarvis-auth')
SQLITE_HEADER = b'SQLite format 3\x00'
SIDE_FILES = ('-wal', '-shm', '-journal')


def is_link(path):
    return path.is_symlink() or path.is_junction()


def is_sqlite(path):
    with path.open('rb') as stream:
        return stream.read(16) == SQLITE_HEADER


def copy_sqlite(src, dst):
    source = sqlite3.connect(f'file:{src.as_posix()}?mode=ro', uri=True, timeout=10)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        result = target.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError(f'Integrity check failed for {src}: {result}')
    finally:
        target.close()
        source.close()


def stage(root, staging):
    counts = {'files': 0, 'sqlite': 0, 'skipped_links': 0}
    for name in SOURCES:
        src_root = root / name
        if not src_root.is_dir() or is_link(src_root):
            raise RuntimeError(f'Missing or linked source folder: {src_root}')
        for folder, dirs, files in os.walk(src_root, followlinks=False):
            folder = Path(folder)
            kept = []
            for d in dirs:
                if is_link(folder / d):
                    counts['skipped_links'] += 1
                else:
                    kept.append(d)
            dirs[:] = kept
            out_dir = staging / name / folder.relative_to(src_root)
            out_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                src = folder / f
                if is_link(src):
                    counts['skipped_links'] += 1
                    continue
                if f.endswith(SIDE_FILES):
                    continue  # captured through the backup API of the main database
                if is_sqlite(src):
                    copy_sqlite(src, out_dir / f)
                    counts['sqlite'] += 1
                else:
                    shutil.copy2(src, out_dir / f)
                counts['files'] += 1
    return counts


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: stage_data.py <project_root> <staging_dir>')
    root, staging = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if staging.is_relative_to(root):
        sys.exit('Staging folder must be outside the project.')
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    counts = stage(root, staging)
    print(f"staged files={counts['files']} sqlite={counts['sqlite']} skipped_links={counts['skipped_links']}")


if __name__ == '__main__':
    main()
