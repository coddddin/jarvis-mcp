"""Retrieval eval for local RAG (board 2e3093a2): recall@5 and MRR over queries.json, per retrieval method.

Indexes the checkout docs/ (collection project_docs) and the synthetic sample Vault in vault/ (collection vault)
into a throwaway workspace, then searches all 20 questions with lexical, and embedding-only and hybrid for each
local model. Relevance is per file: the rank of a hit is its rank among distinct files. Local Ollama only.
  <repo>/.venv/Scripts/python.exe scripts/rag_eval/run.py [--models ...] [--lexical-weights 1 0.5 0.25] [--json out]
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jarvis.core.guardrails import Guardrails  # noqa: E402
from jarvis.features.embeddings.service import MODELS  # noqa: E402
from jarvis.features.knowledge import service as knowledge  # noqa: E402
from jarvis.features.knowledge.service import KnowledgeStore  # noqa: E402

HERE = Path(__file__).resolve().parent
K = 5


class FolderSource:
    max_files, max_total_bytes, max_chunks = 1000, 20_000_000, 8000

    def __init__(self, root, collection):
        self.root, self.collection = root, collection

    def walk(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*.md'))

    def read(self, rel):
        return (self.root / rel).read_bytes()


def ranks(store, queries, sources, mode):
    """Per query: 1-based rank of the first relevant file among distinct files, or None."""
    out = []
    for item in queries:
        hits = store.search(item['query'], 10, sources=sources, mode=mode)['results']
        files = []
        for hit in hits:
            key = hit['collection'] + ':' + hit['path']
            if key not in files:
                files.append(key)
        out.append(next((i + 1 for i, key in enumerate(files) if key in item['relevant']), None))
    return out


def metrics(found):
    return {'recall@5': round(sum(r is not None and r <= K for r in found) / len(found), 3),
            'mrr': round(sum(1 / r for r in found if r) / len(found), 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models', nargs='*', default=list(MODELS))
    parser.add_argument('--json')
    parser.add_argument('--lexical-weights', nargs='*', type=float, default=[1.0, 0.5, 0.25])
    args = parser.parse_args()
    queries = json.loads((HERE / 'queries.json').read_text(encoding='utf-8'))['queries']
    sources = {'project_docs': FolderSource(ROOT / 'docs', 'project_docs'),
               'vault': FolderSource(HERE / 'vault', 'vault')}
    report = {'queries': len(queries), 'runs': {}}
    with tempfile.TemporaryDirectory() as workspace:
        guard = Guardrails(Path(workspace))
        lexical = KnowledgeStore(guard)
        for source in sources.values():
            lexical.index_source(source)
        found = ranks(lexical, queries, sources, 'lexical')
        report['runs']['lexical'] = {**metrics(found), 'ranks': found}
        for model in args.models:
            store = KnowledgeStore(guard, embed_model=model)
            start = time.monotonic()
            built = [store.index_source(source) for source in sources.values()]
            if any(result['embedding']['status'] != 'ok' for result in built):
                report['runs'][model] = {'error': [result['embedding'] for result in built]}
                continue
            seconds = round(time.monotonic() - start, 1)
            for mode, weight in [('embedding', None)] + [('hybrid', w) for w in args.lexical_weights]:
                if weight is not None:
                    knowledge.LEXICAL_WEIGHT = weight
                start = time.monotonic()
                found = ranks(store, queries, sources, mode)
                name = f'{mode}:{model}' + ('' if weight is None else f' w_lex={weight}')
                report['runs'][name] = {**metrics(found), 'ranks': found, 'index_seconds': seconds,
                                                     'search_seconds': round(time.monotonic() - start, 1),
                                                     'chunks': sum(result['chunks'] for result in built)}
    print(f"{'method':40} recall@5   MRR")
    for name, run in report['runs'].items():
        print(f"{name:40} {run.get('recall@5', '-'):>8} {run.get('mrr', '-'):>6}  {run.get('error', '')}")
    misses = {name: [q['id'] for q, r in zip(queries, run['ranks']) if not r or r > K]
              for name, run in report['runs'].items() if 'ranks' in run}
    print('misses@5:', json.dumps(misses, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
