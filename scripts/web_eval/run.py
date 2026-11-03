"""Re-runnable web-task benchmark: 10 tasks (tasks.json), pinned external MCP servers, scratch-only outputs.

Drivers:
  claude    Claude Code CLI (subscription) with --mcp-config, no built-in tools, dontAsk. Quality baseline.
  qwen-raw  Ollama qwen3.5:9b through a minimal MCP client, no JARVIS help (no trimming, planner or retries).
  jarvis    the real JARVIS web agent (jarvis.features.web_agent) with qwen3.5:9b through the local chat worker,
            Playwright attached through the JARVIS MCP client with the recommended launch flags.

Everything runs in SANDBOX (outside the repo). Downloads are scanned with Microsoft Defender and never opened.
  python scripts/web_eval/run.py --setup                       install the pinned servers into SANDBOX
  <SANDBOX>/fetch-venv/Scripts/python.exe scripts/web_eval/run.py --driver qwen-raw [t01 t05 ...]
  python scripts/web_eval/run.py --driver claude [task ids]
  <repo>/.venv/Scripts/python.exe scripts/web_eval/run.py --driver jarvis [task ids]
  python scripts/web_eval/run.py --rescore <results dir>
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import score  # noqa: E402

HERE = Path(__file__).resolve().parent
SANDBOX = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'JarvisWorktrees' / 'web-eval'
PLAYWRIGHT_MCP = '@playwright/mcp@0.0.83'        # published 2026-09-28
FETCH_MCP = 'mcp-server-fetch==2026.10.10'      # published 2026-10-11
PW = SANDBOX / 'mcp' / 'node_modules' / '@playwright' / 'mcp' / 'cli.js'
FETCH = SANDBOX / 'fetch-venv' / 'Scripts' / 'python.exe'
DEFENDER = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Windows Defender' / 'MpCmdRun.exe'
TASK_SECONDS, MAX_TURNS = 600, 25
QWEN, QWEN_CTX, RESULT_CAP = 'qwen3.5:9b', 32768, 60000
# Tools the model never gets: arbitrary page scripts, uploads, browser installs.
DENY = ['browser_run_code_unsafe', 'browser_evaluate', 'browser_file_upload', 'browser_install']
QWEN_TOOLS = {'browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_navigate_back',
              'browser_wait_for', 'browser_press_key', 'fetch'}
# Files Playwright MCP writes itself; anything else in the output folder is a download.
ARTIFACT = re.compile(r'^(page|console)-\d{4}-\d\d-\d\dT[\d-]+Z\.(yml|log|png|jpe?g)$')

RULES = """You are a web research agent. Rules:
- Public web only. Never log in, never create accounts, never enter personal data, never submit forms except a search box.
- To search, open a search engine results page with the browser: https://www.bing.com/search?q=<query>&setlang=ko
- Open result pages and follow links as needed. Prefer official or primary sources.
- You may use the fetch tool to read a page as text.
- Downloads: only when the task asks. Never open or run a downloaded file.
- Finish with: the answer (in the task's language), then a 'Sources:' list of the URLs you used."""


def playwright_args(output):
    return [str(PW), '--headless', '--isolated', '--browser', 'msedge', '--no-webmcp', '--image-responses', 'omit',
            '--output-dir', str(output)]


def setup():
    (SANDBOX / 'mcp').mkdir(parents=True, exist_ok=True)
    npm = 'npm.cmd' if os.name == 'nt' else 'npm'
    if not (SANDBOX / 'mcp' / 'package.json').exists():
        subprocess.run([npm, 'init', '-y'], cwd=SANDBOX / 'mcp', check=True, capture_output=True)
    subprocess.run([npm, 'install', '--no-fund', '--no-audit', PLAYWRIGHT_MCP], cwd=SANDBOX / 'mcp', check=True)
    if not FETCH.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(SANDBOX / 'fetch-venv')], check=True)
    subprocess.run([str(FETCH), '-m', 'pip', 'install', '-q', FETCH_MCP], check=True)
    print('setup done:', SANDBOX)


def scan(path):
    """True when Defender reports no threat, False on a threat, None when the scan could not run."""
    if not DEFENDER.exists():
        return None
    result = subprocess.run([str(DEFENDER), '-Scan', '-ScanType', '3', '-File', str(path), '-DisableRemediation'],
                            capture_output=True, timeout=300)
    return {0: True, 2: False}.get(result.returncode)


def downloads(folder):
    return [{'name': p.name, 'bytes': p.stat().st_size, 'clean': scan(p)}
            for p in sorted(folder.rglob('*')) if p.is_file() and not ARTIFACT.match(p.name)]


def child_env():
    drop = ('ANTHROPIC_', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(drop)}


def run_claude(task, folder):
    output = folder / 'downloads'
    config = {'mcpServers': {'playwright': {'command': 'node', 'args': playwright_args(output)},
                             'fetch': {'command': str(FETCH), 'args': ['-m', 'mcp_server_fetch']}}}
    argv = ['claude', '-p', task['prompt'], '--output-format', 'stream-json', '--verbose', '--model', 'sonnet',
            '--strict-mcp-config', '--mcp-config', json.dumps(config), '--tools', '',
            '--allowedTools', 'mcp__playwright,mcp__fetch',
            '--disallowedTools', ','.join('mcp__playwright__' + name for name in DENY),
            '--permission-mode', 'dontAsk', '--max-turns', str(MAX_TURNS), '--setting-sources', '',
            '--no-session-persistence', '--append-system-prompt', RULES]
    with open(folder / 'stream.jsonl', 'wb') as log:
        process = subprocess.Popen(argv, cwd=folder, stdout=log, stderr=subprocess.STDOUT, env=child_env())
        try:
            process.wait(TASK_SECONDS)
            timed_out = False
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True)
            process.wait()
            timed_out = True
    calls, answer, turns = [], '', None
    for raw in (folder / 'stream.jsonl').read_bytes().decode('utf-8', 'replace').splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get('type') == 'assistant':
            calls += [{'name': b['name'].split('__')[-1], 'args': b.get('input') or {}}
                      for b in event['message'].get('content', []) if b.get('type') == 'tool_use']
        if event.get('type') == 'result':
            answer, turns = event.get('result') or '', event.get('num_turns')
    return {'answer': answer, 'calls': calls, 'turns': turns, 'notes': ['timeout'] if timed_out else []}


def ollama(messages, tools):
    body = json.dumps({'model': QWEN, 'messages': messages, 'tools': tools, 'stream': False,
                       'options': {'num_ctx': QWEN_CTX}}).encode()
    request = urllib.request.Request('http://127.0.0.1:11434/api/chat', data=body,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())


async def qwen_task(task, folder):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    output = folder / 'downloads'
    servers = [StdioServerParameters(command='node', args=playwright_args(output), cwd=str(folder)),
               StdioServerParameters(command=str(FETCH), args=['-m', 'mcp_server_fetch'], cwd=str(folder))]
    calls, notes, answer, tokens, seen = [], [], '', [], {}
    start = time.monotonic()
    async with stdio_client(servers[0]) as (pr, pw), stdio_client(servers[1]) as (fr, fw):
        async with ClientSession(pr, pw) as browser, ClientSession(fr, fw) as fetcher:
            await browser.initialize()
            await fetcher.initialize()
            owner, tools = {}, []
            for session in (browser, fetcher):
                for tool in (await session.list_tools()).tools:
                    if tool.name in QWEN_TOOLS:
                        owner[tool.name] = session
                        tools.append({'type': 'function', 'function': {
                            'name': tool.name, 'description': tool.description or '', 'parameters': tool.inputSchema}})
            messages = [{'role': 'system', 'content': RULES}, {'role': 'user', 'content': task['prompt']}]
            for _ in range(MAX_TURNS):
                if time.monotonic() - start > TASK_SECONDS:
                    notes.append('timeout')
                    break
                reply = await asyncio.to_thread(ollama, messages, tools)
                tokens.append(reply.get('prompt_eval_count'))
                message = reply.get('message', {})
                messages.append({k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls')})
                if not message.get('tool_calls'):
                    answer = message.get('content') or ''
                    if not answer.strip():
                        notes.append(f"empty final answer (thinking {len(message.get('thinking') or '')} chars, "
                                     f"done_reason {reply.get('done_reason')})")
                    break
                for call in message['tool_calls']:
                    name = call.get('function', {}).get('name')
                    args = call.get('function', {}).get('arguments') or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            notes.append('bad json arguments')
                            args = {}
                    key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] == 3:
                        notes.append('loop: same call 3x ' + str(name))
                    if name not in owner:
                        text, error = 'Unknown tool ' + str(name), True
                    else:
                        try:
                            result = await asyncio.wait_for(owner[name].call_tool(name, args), 120)
                            text = '\n'.join(getattr(part, 'text', '') for part in result.content)
                            error = bool(result.isError)
                        except Exception as exc:  # a failed tool call is a result the model sees
                            text, error = 'Tool error: ' + str(exc), True
                    calls.append({'name': name, 'args': args, 'result_chars': len(text), 'error': error})
                    messages.append({'role': 'tool', 'tool_name': name, 'content': text[:RESULT_CAP]})
            else:
                notes.append('max turns')
    if any((count or 0) >= QWEN_CTX - 512 for count in tokens):
        notes.append('context full')
    return {'answer': answer, 'calls': calls, 'turns': len(tokens), 'notes': notes, 'max_prompt_tokens': max(tokens or [0])}


def jarvis_task(task, folder):
    """The JARVIS agent loop on a scratch workspace: Playwright approved through McpSettings like the HUD does."""
    sys.path.insert(0, str(HERE.parent.parent))
    import shutil
    from jarvis.agents.brain import BrainRequest, OllamaProvider
    from jarvis.core.guardrails import Guardrails
    from jarvis.features.mcp_client.service import McpSession, McpSettings, discover
    from jarvis.features.snapshots.service import SnapshotStore
    from jarvis.features.web.service import WebCollector
    from jarvis.features.web_agent import service as web_agent
    workspace, quarantine = folder / 'ws', folder / 'quarantine'
    workspace.mkdir(parents=True, exist_ok=True)
    guard = Guardrails(workspace)
    settings = McpSettings(guard)
    args = [*playwright_args(quarantine), '--user-agent', web_agent.BROWSER_USER_AGENT,
            '--blocked-origins', web_agent.BLOCKED_ORIGINS]
    settings.add_server('pw', 'Playwright', shutil.which('node'), args)
    found = discover(guard, settings.load(), 'pw')
    if found['status'].get('state') != 'ready':
        return {'answer': '', 'calls': [], 'turns': None, 'notes': ['playwright: ' + json.dumps(found['status'])]}
    settings.approve('pw', found['tools'], web_agent.PLAYWRIGHT_PINS)
    provider = OllamaProvider(guard)

    decisions = []

    def decide(messages, schema, deadline):
        value = provider.infer(QWEN, BrainRequest(messages=messages, schema=schema, deadline=deadline))
        decisions.append({'offered': schema['properties']['tool']['enum'], 'chose': value})
        return value

    with McpSession(guard, settings.load()) as session:
        if session.status.get('pw', {}).get('state') != 'ready':
            return {'answer': '', 'calls': [], 'turns': None, 'notes': ['session: ' + json.dumps(session.status)]}
        browser = web_agent.Browser(session, 'pw', quarantine)
        agent = web_agent.WebAgent(guard, WebCollector(SnapshotStore(guard)), browser, decide,
                                   downloads_dir=folder / 'downloads', scan=web_agent.defender_scan)
        result = agent.run(task['prompt'])
    calls = []
    for trace in result['traces']:
        url = trace.get('url') or (trace.get('from') if str(trace.get('from', '')).startswith('https://') else '')
        calls.append({'name': trace['tool'], 'args': {'url': url} if url else {'query': trace.get('query', '')},
                      'status': trace.get('status'), 'source': trace.get('source'),
                      'from': str(trace.get('from') or '')[:120], 'reason': trace.get('reason', '')})
    notes = [result['status']] + ([result['reason']] if result['reason'] else [])
    return {'answer': result['answer'], 'calls': calls, 'turns': result['calls'], 'notes': notes,
            'searches': result['searches'], 'agent_files': result['files'], 'decisions': decisions}


def visited(calls):
    return [call['args']['url'] for call in calls if isinstance(call.get('args'), dict) and call['args'].get('url')]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--driver', choices=['claude', 'qwen-raw', 'jarvis'])
    parser.add_argument('--rescore', type=Path)
    parser.add_argument('--repeat', type=int, default=1, help='run the task set N times; the summary reports the median')
    parser.add_argument('tasks', nargs='*')
    args = parser.parse_args()
    if args.setup:
        return setup()
    spec = json.loads((HERE / 'tasks.json').read_text(encoding='utf-8'))
    tasks = [task for task in spec['tasks'] if not args.tasks or task['id'] in args.tasks]
    if args.rescore:
        folder = args.rescore
    elif args.driver:
        folder = SANDBOX / 'results' / f"{args.driver}-{datetime.now():%Y%m%d-%H%M%S}"
    else:
        parser.error('choose --setup, --driver or --rescore')
    rounds = [folder] if args.rescore or args.repeat <= 1 else [folder / f'r{index + 1}' for index in range(args.repeat)]
    counts = [run_set(args, tasks, spec, place) for place in rounds]
    counts.sort()
    print(f"{args.driver or 'rescore'}: median {counts[len(counts) // 2]}/{len(tasks)} over {len(counts)} run(s) {counts}  ({folder})")


def run_set(args, tasks, spec, folder):
    rows = []
    for task in tasks:
        work = folder / task['id']
        record_path = work / 'record.json'
        if args.rescore:
            record = json.loads(record_path.read_text(encoding='utf-8'))
        else:
            (work / 'downloads').mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            try:
                record = (run_claude(task, work) if args.driver == 'claude' else jarvis_task(task, work)
                          if args.driver == 'jarvis' else asyncio.run(qwen_task(task, work)))
            except Exception as exc:  # one broken task must not stop the benchmark
                record = {'answer': '', 'calls': [], 'turns': None, 'notes': ['harness error: ' + repr(exc)]}
            record.update(task=task['id'], driver=args.driver, wall_s=round(time.monotonic() - started, 1),
                          files=downloads(work / 'downloads'))
        record['score'] = score(task, record['answer'], visited(record['calls']), record['files'])
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding='utf-8')
        rows.append(record)
        print(task['id'], 'PASS' if record['score']['pass'] else 'fail', record['score']['checks'],
              'turns', record.get('turns'), 'wall', record.get('wall_s'), record.get('notes'), flush=True)
    passed = sum(row['score']['pass'] for row in rows)
    summary = {'driver': rows[0].get('driver') if rows else args.driver, 'passed': passed, 'total': len(rows),
               'tasks_version': spec['version'], 'as_of': spec['as_of']}
    (folder / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"{summary['driver']}: {passed}/{len(rows)}  ({folder})", flush=True)
    return passed


if __name__ == '__main__':
    main()
