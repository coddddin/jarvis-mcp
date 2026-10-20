"""Operator CLI for local tasks and preview-bound apply; no host source execution."""
import argparse
import json
import sys

from jarvis.agents.models import TaskRequest
from jarvis.agents.runner import LocalConfig, LocalRunner
from jarvis.agents.store import TaskStore
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import DEFAULT_WORKSPACE, CONFIG_ROOT
from jarvis.agents.sandbox import DockerRuntimeConfig
from jarvis.features.development.service import ProposalApplier, ProposalError
from jarvis.features.snapshots.service import SnapshotStore


def main():
    parser = argparse.ArgumentParser(description='Local Python proposals with static QA only by default; explicit restricted/container cases supported.')
    parser.add_argument('action', choices=['create', 'run', 'status', 'cancel', 'preview', 'apply', 'forget', 'prune', 'resume', 'interrupt'])
    parser.add_argument('--task-id')
    parser.add_argument('--summary')
    parser.add_argument('--model', default=LocalConfig().model)
    parser.add_argument('--target')
    parser.add_argument('--review-digest')
    parser.add_argument('--cases-json', default='[]', help='Optional bounded pure-integer cases; never runs Python source.')
    parser.add_argument('--max-generation-tokens', type=int, default=8192)
    parser.add_argument('--max-model-tokens', type=int, default=65536)
    parser.add_argument('--retention-days', type=int, default=7)
    parser.add_argument('--memory-project')
    parser.add_argument('--memory-owner', default='local')
    parser.add_argument('--qa-mode', choices=['restricted', 'container'], default='restricted')
    parser.add_argument('--feedback')
    args = parser.parse_args()
    if args.action == 'create' and not args.summary:
        parser.error('create requires --summary')
    if args.action not in {'create', 'prune'} and not args.task_id:
        parser.error('action requires --task-id')
    if args.action in {'preview', 'apply'} and not args.target:
        parser.error('preview/apply require --target projects/<name>/<file>.py')
    if args.action == 'apply' and not args.review_digest:
        parser.error('apply requires --review-digest from preview')
    if args.action in {'resume', 'interrupt'} and not args.feedback:
        parser.error('resume/interrupt require explicit operator --feedback')
    try:
        store = TaskStore(Guardrails(DEFAULT_WORKSPACE))
        if args.action in {'forget', 'prune'}:
            print(json.dumps(store.forget(args.task_id) if args.action == 'forget' else store.prune()))
            return 0
        if args.action in {'preview', 'apply'}:
            applier = ProposalApplier(SnapshotStore(store.guard), store)
            report = applier.preview(args.task_id, args.target) if args.action == 'preview' else applier.apply(
                args.task_id, args.target, args.review_digest)
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0
        if args.action == 'create':
            if len(args.cases_json) > 16000:
                parser.error('Case JSON exceeds size limit.')
            request = TaskRequest.model_validate_json(json.dumps({'summary': args.summary, 'timeout_seconds': 600,
                'integer_cases': json.loads(args.cases_json), 'max_generation_tokens': args.max_generation_tokens,
                'max_model_tokens': args.max_model_tokens, 'retention_days': args.retention_days,
                'memory_project': args.memory_project, 'memory_owner': args.memory_owner, 'qa_mode': args.qa_mode}))
            task = store.create(request)
        elif args.action == 'run':
            current = store.get(args.task_id)
            docker = DockerRuntimeConfig.model_validate_json((CONFIG_ROOT / 'agent_runtime.json').read_text(encoding='utf-8')) if current.request.qa_mode == 'container' else None
            task = LocalRunner(store, LocalConfig(model=args.model, docker=docker)).run(args.task_id)
        elif args.action == 'cancel':
            task = store.interrupt(args.task_id, 'Cancelled by local operator.', cancelled=True)
        elif args.action == 'resume':
            task = store.resume(args.task_id, args.feedback)
        elif args.action == 'interrupt':
            task = store.interrupt(args.task_id, args.feedback)
        else:
            task = store.get(args.task_id)
        print(task.model_dump_json(indent=2))
        return 0 if task.status not in {'needs_user_input', 'timed_out'} else 2
    except ProposalError as exc:
        print(json.dumps({'error': 'ProposalError', 'detail': str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': 'Operation failed; inspect task status or local Ollama setup.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
