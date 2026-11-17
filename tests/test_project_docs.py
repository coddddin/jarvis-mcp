"""Board 2e3093a2 part 5: the read-only project docs root over the checkout's docs/."""
import hashlib
import os
from pathlib import Path
import sys

import pytest

from jarvis.core.paths import DEFAULT_WORKSPACE, PROJECT_ROOT
from jarvis.features.project_docs import service as project_docs
from jarvis.features.project_docs.service import ProjectDocsError, ProjectDocsRoot


def link_folder(target, link):
    """A directory symlink, or a junction on Windows (no admin rights needed)."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != 'win32':
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def tree(tmp_path):
    workspace, root = tmp_path / 'ws', tmp_path / 'docs'
    (workspace / 'documents').mkdir(parents=True)
    (root / 'guides').mkdir(parents=True)
    (root / '.hidden').mkdir()
    (root / 'README.md').write_text('# 문서 안내\n', encoding='utf-8')
    (root / 'guides' / 'chat.md').write_text('# 채팅\n본문\n', encoding='utf-8')
    (root / 'guides' / 'notes.txt').write_text('not markdown', encoding='utf-8')
    (root / '.hidden' / 'secret.md').write_text('hidden', encoding='utf-8')
    (root / '.dot.md').write_text('hidden file', encoding='utf-8')
    return workspace, root


def test_the_real_checkout_docs_are_listed_and_read_without_change():
    source = ProjectDocsRoot(DEFAULT_WORKSPACE)
    assert source.root == (PROJECT_ROOT / 'docs').resolve()
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.root.rglob('*') if path.is_file()}
    found = source.walk()
    assert 'README.md' in found and all(path.endswith('.md') for path in found)
    for relative in found:
        assert source.read(relative)
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.root.rglob('*') if path.is_file()}
    assert before == after and source.cite('README.md') == 'docs/README.md'


def test_walk_lists_only_plain_markdown_and_skips_hidden_and_linked(tree, tmp_path):
    workspace, root = tree
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'other.md').write_text('outside', encoding='utf-8')
    link_folder(elsewhere, root / 'linked')
    source = ProjectDocsRoot(workspace, root)
    assert source.walk() == ['README.md', 'guides/chat.md']
    assert source.skipped == [{'path': 'linked', 'reason': 'linked folder'}]


@pytest.mark.parametrize('relative', ['../ws/documents/x.md', 'guides/../README.md', '/README.md', 'C:/x.md',
                                      'guides\\chat.md', '.hidden/secret.md', '.dot.md', 'guides/notes.txt',
                                      'README.MD', 'README.md:stream', 'guides//chat.md', 'missing.md', '',
                                      'guides/ chat.md', 'linked/other.md'])
def test_read_refuses_anything_but_a_plain_relative_markdown_path(tree, tmp_path, relative):
    workspace, root = tree
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'other.md').write_text('outside', encoding='utf-8')
    link_folder(elsewhere, root / 'linked')
    with pytest.raises(ProjectDocsError):
        ProjectDocsRoot(workspace, root).read(relative)


def test_read_refuses_hard_links_and_oversized_files(tree, monkeypatch):
    workspace, root = tree
    os.link(root / 'README.md', root / 'twin.md')
    source = ProjectDocsRoot(workspace, root)
    with pytest.raises(ProjectDocsError, match='plain file'):
        source.read('twin.md')
    monkeypatch.setattr(project_docs, 'MAX_FILE_BYTES', 4)
    with pytest.raises(ProjectDocsError, match='byte limit'):
        source.read('guides/chat.md')


@pytest.mark.parametrize('where', ['inside', 'equal', 'parent', 'missing', 'linked', 'relative', 'file'])
def test_the_root_must_be_a_plain_folder_apart_from_the_workspace(tree, tmp_path, where):
    workspace, root = tree
    candidate = {'inside': workspace / 'documents', 'equal': workspace, 'parent': tmp_path,
                 'missing': tmp_path / 'gone', 'linked': tmp_path / 'docs-link', 'relative': Path('docs'),
                 'file': root / 'README.md'}[where]
    if where == 'linked':
        link_folder(root, candidate)
    with pytest.raises(ProjectDocsError):
        ProjectDocsRoot(workspace, candidate)


def test_walk_stops_at_the_caps(tree, monkeypatch):
    workspace, root = tree
    monkeypatch.setattr(project_docs, 'MAX_FILES', 1)
    with pytest.raises(ProjectDocsError, match='Too many'):
        ProjectDocsRoot(workspace, root).walk()


def test_there_is_no_writing_method():
    public = {name for name in dir(ProjectDocsRoot) if not name.startswith('_')}
    assert public == {'collection', 'label', 'prefix', 'skipped', 'cite', 'read', 'walk'}
