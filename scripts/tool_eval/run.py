"""REA tool-task eval (board 204bc125 part 4): the JARVIS tool agent with the local model on one sample binary.

  <repo>\\.venv\\Scripts\\python.exe scripts\\tool_eval\\run.py                 all tasks once
  <repo>\\.venv\\Scripts\\python.exe scripts\\tool_eval\\run.py --repeat 3 r02  median of three runs
  <repo>\\.venv\\Scripts\\python.exe scripts\\tool_eval\\run.py --rescore <results folder>

Each run uses a fresh scratch workspace under %LOCALAPPDATA%\\JarvisWorktrees\\tool-eval (never the real
JarvisWorkspace), approves the REA preset there exactly as scripts/setup_rea_tools.py does, copies the sample in and
asks qwen3.5:9b through the same tool loop chat uses. Only static read pins exist, so nothing is run or written.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from score import score  # noqa: E402

SANDBOX = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisWorktrees' / 'tool-eval'
QWEN = 'qwen3.5:9b'


def run_one(task, spec, work):
    from jarvis.agents.brain import BrainRequest, OllamaProvider
    from jarvis.core.guardrails import Guardrails
    from jarvis.features.mcp_client.service import McpSettings
    from jarvis.features.mcp_presets.service import REA_SERVER_ID, setup_rea
    from jarvis.features.tool_agent import service as tool_agent
    workspace = work / 'ws'
    target = workspace / spec['sample']['to']
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec['sample']['from'], target)
    guard = Guardrails(workspace)
    setup_rea(guard)
    servers = [server for server in McpSettings(guard).load().servers if server.id == REA_SERVER_ID]
    provider, decisions = OllamaProvider(guard), []

    def decide(messages, schema, deadline):
        value = provider.infer(QWEN, BrainRequest(messages=messages, schema=schema, deadline=deadline))
        decisions.append(value)
        return value

    result = tool_agent.run_task(guard, task['prompt'], decide, servers, external=False)
    return {'answer': result['answer'], 'status': result['status'], 'reason': result['reason'],
            'calls': result['calls'], 'decisions': decisions,
            'traces': [{'name': trace['name'], 'status': trace['status'], 'detail': trace['detail'][:300]}
                       for trace in result['traces']],
            'sample_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}


def run_set(args, tasks, spec, folder, pinned):
    rows = []
    for task in tasks:
        work = folder / task['id']
        record_path = work / 'record.json'
        if args.rescore:
            record = json.loads(record_path.read_text(encoding='utf-8'))
        else:
            work.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            try:
                record = run_one(task, spec, work)
            except Exception as exc:  # one broken task must not stop the eval
                record = {'answer': '', 'traces': [], 'calls': None, 'status': 'harness', 'reason': repr(exc)}
            record.update(task=task['id'], wall_s=round(time.monotonic() - started, 1))
            shutil.rmtree(work / 'ws', ignore_errors=True)
        record['score'] = score(task, record['answer'], record['traces'], pinned)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding='utf-8')
        rows.append(record)
        print(task['id'], 'PASS' if record['score']['pass'] else 'fail', record['score']['checks'],
              'calls', record.get('calls'), 'wall', record.get('wall_s'), record.get('status'), flush=True)
    passed = sum(row['score']['pass'] for row in rows)
    summary = {'passed': passed, 'total': len(rows), 'model': QWEN, 'tasks_version': spec['version'],
               'as_of': spec['as_of'], 'sample_matches': all(row.get('sample_sha256') in
                                                            (None, spec['sample']['checked_sha256']) for row in rows)}
    (folder / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    return passed


def main():
    from jarvis.features.mcp_presets.service import REA_PINS
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rescore', type=Path)
    parser.add_argument('--repeat', type=int, default=1, help='run the task set N times; the summary reports the median')
    parser.add_argument('tasks', nargs='*')
    args = parser.parse_args()
    spec = json.loads((HERE / 'tasks.json').read_text(encoding='utf-8'))
    tasks = [task for task in spec['tasks'] if not args.tasks or task['id'] in args.tasks]
    folder = args.rescore or SANDBOX / 'results' / f'jarvis-{datetime.now():%Y%m%d-%H%M%S}'
    rounds = [folder] if args.rescore or args.repeat <= 1 else [folder / f'r{index + 1}' for index in range(args.repeat)]
    counts = sorted(run_set(args, tasks, spec, place, REA_PINS) for place in rounds)
    print(f'median {counts[len(counts) // 2]}/{len(tasks)} over {len(counts)} run(s) {counts}  ({folder})')


if __name__ == '__main__':
    main()
