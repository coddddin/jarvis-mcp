"""Read-only project documentation checks using Python standard library."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

REQUIRED = ('README.md', 'AGENTS_CONTEXT.md', 'docs/README.md', 'docs/PLAN.md',
            'docs/DEVELOPMENT.md', 'docs/guides/system-tools.md',
            'docs/guides/knowledge-and-google.md', 'docs/reports/phase-01.md',
            'docs/reports/phase-02.md', 'docs/guides/agent-tasks.md',
            'docs/reports/phase-02-5.md', 'docs/guides/agent-controls.md',
            'docs/reports/phase-03.md', 'docs/guides/local-hud.md',
            'docs/guides/rea-agent-reach.md', 'docs/reports/rea-agent-reach.md')


def targets(text):
    lines, fence = [], None
    for line in text.splitlines():
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            if fence is None:
                fence = match[1][0]
            elif match[1][0] == fence:
                fence = None
            continue
        if fence is None:
            lines.append(re.sub(r'`[^`]*`', '', line))
    prose = '\n'.join(lines)
    links = re.findall(r'!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+"[^"\n]*")?\s*\)', prose)
    links += re.findall(r'^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)', prose, re.M)
    return [link.strip('<>') for link in links]


def local_path(source, link):
    parsed = urlsplit(link)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return (source.parent / unquote(parsed.path)).resolve()


def check_documents(root):
    root = Path(root).resolve()
    errors = [f'Missing required document: {name}' for name in REQUIRED if not (root / name).is_file()]
    context = root / 'AGENTS_CONTEXT.md'
    if context.is_file() and len(context.read_text(encoding='utf-8-sig').splitlines()) > 50:
        errors.append('AGENTS_CONTEXT.md exceeds 50 lines')
    docs = root / 'docs'
    files = [root / 'README.md', context]
    files += [p for p in docs.rglob('*.md') if 'archive' not in p.relative_to(docs).parts]
    for source in files:
        if not source.is_file():
            continue
        for link in targets(source.read_text(encoding='utf-8-sig')):
            path = local_path(source, link)
            if path is not None:
                if not path.is_relative_to(root):
                    errors.append(f'Local link leaves project: {source.relative_to(root)} -> {link}')
                elif not path.exists():
                    errors.append(f'Broken local link: {source.relative_to(root)} -> {link}')
    index = docs / 'README.md'
    if index.is_file():
        listed = {local_path(index, link) for link in targets(index.read_text(encoding='utf-8-sig'))}
        for source in files:
            if source.is_file() and source.is_relative_to(docs) and source != index and source.resolve() not in listed:
                errors.append(f'Active document missing from docs/README.md: {source.relative_to(root)}')
    return errors


def main():
    try:
        errors = check_documents(Path(__file__).resolve().parents[1])
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'Document check failed: {type(exc).__name__}: {exc}')
        return 1
    for error in errors:
        print('ERROR:', error)
    if not errors:
        print('Documentation checks passed: required files, local links, index coverage, context <= 50 lines.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
