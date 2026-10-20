from scripts.check_docs import REQUIRED, check_documents


def fixture_docs(root):
    for name in REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Document\n', encoding='utf-8')
    (root / 'docs/README.md').write_text('\n'.join(
        f'[Doc]({name.removeprefix("docs/")})' for name in REQUIRED
        if name.startswith('docs/') and name != 'docs/README.md'), encoding='utf-8')


def test_valid_and_ignored_content(tmp_path):
    fixture_docs(tmp_path)
    (tmp_path / 'README.md').write_text(
        '```md\n[Example](missing.md)\n```\n`[Example](missing.md)`\n[Web](https://example.com)\n')
    archive = tmp_path / 'docs/archive'
    archive.mkdir()
    (archive / 'old.md').write_text('[Old](missing.md)')
    assert check_documents(tmp_path) == []


def test_missing_document_and_broken_link(tmp_path):
    fixture_docs(tmp_path)
    (tmp_path / 'docs/PLAN.md').unlink()
    errors = check_documents(tmp_path)
    assert any('Missing required document' in item for item in errors)
    assert any('Broken local link' in item for item in errors)


def test_context_and_unlisted_document(tmp_path):
    fixture_docs(tmp_path)
    (tmp_path / 'AGENTS_CONTEXT.md').write_text('line\n' * 51)
    (tmp_path / 'docs/guides/new.md').write_text('# New')
    errors = check_documents(tmp_path)
    assert any('exceeds 50 lines' in item for item in errors)
    assert any('missing from docs/README.md' in item for item in errors)


def test_reference_link_and_escape(tmp_path):
    fixture_docs(tmp_path)
    (tmp_path / 'README.md').write_text('[Guide][x]\n[x]: absent.md\n[Outside](../outside.md)\n')
    errors = check_documents(tmp_path)
    assert any('Broken local link' in item for item in errors)
    assert any('leaves project' in item for item in errors)
