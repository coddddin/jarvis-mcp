"""Opt-in Graphify experiment on committed code; never scans a live Vault.

Run with Python from an isolated venv containing graphifyy==0.9.95.
Output must be a new directory outside this checkout. No hooks/skills installed.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time


VERSION = '0.9.95'
ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ('dispatch', ['query', 'dispatcher worker adapter', '--budget', '1200']),
    ('board', ['query', 'board_claim claim', '--budget', '1200']),
    ('obsidian', ['query', 'search_obsidian_vault ObsidianStore KnowledgeStore', '--budget', '1200']),
    ('scoped-adapter', ['explain', 'adapter']),
    ('scoped-obsidian', ['explain', 'ObsidianStore']),
    ('scoped-tool', ['explain', 'search_obsidian_vault']),
    ('missing', ['explain', '__jarvis_missing_node__']),
    ('ambiguous', ['explain', 'build']),
)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if importlib.metadata.version('graphifyy') != VERSION:
        parser.error(f'Use the isolated graphifyy=={VERSION} environment')
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents or output.exists():
        parser.error('Output must be a new directory outside the checkout')
    # Snapshot by immutable commit: excludes dirty/untracked files and symlinks.
    commit = git('rev-parse', 'HEAD').decode().strip()
    tree = git('rev-parse', 'HEAD^{tree}').decode().strip()
    entries = git('ls-tree', '-rz', commit).split(b'\0')
    output.mkdir(parents=True)
    corpus = output / 'corpus'
    corpus.mkdir()
    manifest = []
    for entry in entries:
        if not entry:
            continue
        meta, raw_path = entry.split(b'\t', 1)
        mode, kind, blob = meta.decode().split()
        path = PurePosixPath(raw_path.decode('utf-8'))
        if (mode not in ('100644', '100755') or kind != 'blob'
                or path.parts[0] not in ('jarvis', 'tests')
                or path.suffix not in ('.py', '.js')):
            continue
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Unsafe snapshot path')
        data = git('cat-file', 'blob', blob)
        target = corpus / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest.append({'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()})
    home = output / 'home'
    home.mkdir()
    env = {k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR') if k in os.environ}
    env.update(PATH=str(Path(sys.executable).parent), HOME=str(home), USERPROFILE=str(home),
               GRAPHIFY_BACKEND='none', GRAPHIFY_QUERY_LOG_DISABLE='1',
               PYTHONUTF8='1')
    metrics = {'graphify_version': VERSION, 'source_commit': commit, 'source_tree': tree,
               'files': manifest, 'runs': []}

    def run(name, argv):
        start = time.perf_counter()
        result = subprocess.run([sys.executable, '-m', 'graphify', *argv],
                                cwd=output, env=env, capture_output=True, text=True,
                                encoding='utf-8', timeout=180)
        (output / f'{name}.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
        metrics['runs'].append({'name': name, 'exit': result.returncode,
                               'seconds': round(time.perf_counter() - start, 3),
                               'output_chars': len(result.stdout),
                               'truncated': 'TRUNCATED' in result.stdout})
        return result.returncode

    status = run('extract', ['extract', str(corpus), '--code-only', '--no-cluster',
                             '--max-workers', '2', '--out', str(output / 'result')])
    if status == 0:
        graph = output / 'result/graphify-out/graph.json'
        data = json.loads(graph.read_text(encoding='utf-8'))
        metrics.update(nodes=len(data['nodes']), edges=len(data['edges']),
                       input_tokens=data.get('input_tokens'), output_tokens=data.get('output_tokens'))
        for name, argv in CASES:
            code = run(name, [*argv, '--graph', str(graph)])
            expected = {'missing': 4, 'ambiguous': 3}.get(name, 0)
            if code != expected:
                status = 1
    (output / 'metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    print(f'Pilot exit={status}; evidence: {output / "metrics.json"}')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
