"""Operator entry point for the bounded local-lead collaboration engine."""
import argparse
import json
import sys
from uuid import uuid4

from jarvis.app.chat import ChatService
from jarvis.app.runtime import load_guardrails


def main():
    parser = argparse.ArgumentParser(description='JARVIS local lead + native specialist proposals.')
    parser.add_argument('action', choices=['members', 'run', 'status', 'verify'])
    parser.add_argument('--summary')
    parser.add_argument('--conversation-id')
    parser.add_argument('--request-id')
    parser.add_argument('--local-only', action='store_true')
    parser.add_argument('--model', default='qwen2.5-coder:7b')
    args = parser.parse_args()
    if args.action == 'run' and not args.summary:
        parser.error('run requires --summary')
    if args.action in ('status', 'verify') and not args.conversation_id:
        parser.error(args.action + ' requires --conversation-id')
    if args.action == 'verify' and not args.request_id:
        parser.error('verify requires --request-id')
    try:
        service = ChatService(load_guardrails(), model=args.model)
        if args.action == 'members':
            result = service.team.metadata()
        elif args.action == 'status':
            result = service.store.get(args.conversation_id)
        elif args.action == 'verify':
            result = service.team.verify_history(args.conversation_id, args.request_id)
        else:
            identity = args.conversation_id or service.store.create()['id']
            request = uuid4().hex
            # Emit IDs before execution so an interrupted operation can be inspected.
            print(json.dumps({'conversation_id': identity, 'request_id': request}, ensure_ascii=True), flush=True)
            result = service.send(identity, request, args.summary, mode='team-local' if args.local_only else 'team')
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        if args.action == 'verify' and result['status'] != 'ok':
            return 1
        return 0
    except Exception as exc:
        # Native CLI stderr, credentials and private model responses are never echoed.
        print(json.dumps({'error': type(exc).__name__, 'status': 'not_completed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
