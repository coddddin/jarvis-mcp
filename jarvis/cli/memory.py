"""Operator-only memory CRUD; no model may confirm its own preferences."""
import argparse
import json

from jarvis.agents.memory import MemoryInput, MemoryScope, MemoryStore
from jarvis.agents.store import TaskStore
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import DEFAULT_WORKSPACE


def main():
    parser = argparse.ArgumentParser(description='Scoped, expiring local preferences; not execution permissions.')
    parser.add_argument('action', choices=['put', 'list', 'confirm', 'delete'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--owner', default='local')
    parser.add_argument('--kind', choices=['preference', 'path', 'error_solution'], default='preference')
    parser.add_argument('--key')
    parser.add_argument('--value')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--id')
    parser.add_argument('--revision')
    args = parser.parse_args()
    if args.action == 'put' and (not args.key or not args.value):
        parser.error('put requires --key and --value')
    if args.action in {'confirm', 'delete'} and (not args.id or not args.revision):
        parser.error('confirm/delete require --id and --revision from list')
    try:
        scope = MemoryScope(owner=args.owner, project=args.project)
        store = MemoryStore(TaskStore(Guardrails(DEFAULT_WORKSPACE)))
        if args.action == 'put':
            result = store.put(MemoryInput(**scope.model_dump(), kind=args.kind,
                key=args.key, value=args.value, retention_days=args.days)).model_dump(mode='json')
        elif args.action == 'list':
            result = [r.model_dump(mode='json') for r in store.list(scope)]
        elif args.action == 'confirm':
            result = store.confirm(scope, args.id, args.revision).model_dump(mode='json')
        else:
            store.delete(scope, args.id, args.revision)
            result = {'deleted': True}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': 'Memory operation rejected; check scope/revision/content.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
