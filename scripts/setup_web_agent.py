"""One-time setup for the chat web task agent (M4a): install the pinned Playwright MCP and approve its browser tools.

  .venv\\Scripts\\python.exe scripts\\setup_web_agent.py            show what will be installed and approved, then ask
  .venv\\Scripts\\python.exe scripts\\setup_web_agent.py --yes      same, without the question (the user ran it)

What it does, in order:
1. npm install @playwright/mcp@0.0.83 into JarvisWorkspace/.jarvis-mcp/runtime (no npx at run time).
2. Add the server 'pw' to JarvisWorkspace/.jarvis-settings/mcp_servers.json with the recommended launch flags
   (headless, isolated profile in memory, no WebMCP, Edge, Edge user agent, images omitted, downloads to a
   quarantine folder, localhost blocked as a first layer). An existing 'pw' entry is replaced.
3. Start it once, list the tools it reports and pin only browser_navigate, browser_snapshot and browser_click
   (family public_web) after you confirm; browser_snapshot's filename (saves a file) is blocked. Everything else
   stays hidden from the model.
The agent reads pages with JARVIS's own HTTPS worker, so mcp-server-fetch is not needed here.
Restart the HUD afterwards so chat picks up the approved server.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.core.guardrails import Guardrails  # noqa: E402
from jarvis.core.paths import DEFAULT_WORKSPACE  # noqa: E402
from jarvis.features.mcp_client.service import McpSettings, discover  # noqa: E402
from jarvis.features.web_agent.service import BLOCKED_ORIGINS, BROWSER_USER_AGENT, PLAYWRIGHT_PINS  # noqa: E402

PLAYWRIGHT_MCP = '@playwright/mcp@0.0.83'         # published 2026-09-28, the version the benchmark measured
SERVER_ID = 'pw'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--yes', action='store_true', help='approve the three browser tools without asking')
    parser.add_argument('--installed', type=Path, help='use an existing node_modules/@playwright/mcp/cli.js of '
                        + PLAYWRIGHT_MCP + ' instead of running npm install')
    args = parser.parse_args()
    node, npm = shutil.which('node'), shutil.which('npm.cmd') or shutil.which('npm')
    if not node or not npm:
        raise SystemExit('Node.js (node and npm) was not found. Install Node.js LTS from nodejs.org, then run again.')
    guard = Guardrails(DEFAULT_WORKSPACE)
    runtime = guard.workspace / '.jarvis-mcp' / 'runtime'
    quarantine = guard.workspace / '.jarvis-mcp' / SERVER_ID / 'out'
    runtime.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    if args.installed:
        cli = args.installed.resolve()
        version = json.loads((cli.parent / 'package.json').read_text(encoding='utf-8')).get('version')
        if cli.name != 'cli.js' or 'mcp@' + str(version) != PLAYWRIGHT_MCP.split('/', 1)[1]:
            raise SystemExit(f'{cli} is not {PLAYWRIGHT_MCP} (found version {version}).')
    else:
        if not (runtime / 'package.json').exists():
            subprocess.run([npm, 'init', '-y'], cwd=runtime, check=True, capture_output=True)
        print('Installing', PLAYWRIGHT_MCP, 'into', runtime, flush=True)
        subprocess.run([npm, 'install', '--no-fund', '--no-audit', '--save-exact', PLAYWRIGHT_MCP], cwd=runtime,
                       check=True)
        cli = runtime / 'node_modules' / '@playwright' / 'mcp' / 'cli.js'
    if not cli.is_file():
        raise SystemExit(f'{cli} is missing after npm install.')
    launch = [str(cli), '--headless', '--isolated', '--no-webmcp', '--browser', 'msedge',
              '--user-agent', BROWSER_USER_AGENT, '--image-responses', 'omit', '--output-dir', str(quarantine),
              '--blocked-origins', BLOCKED_ORIGINS]
    settings = McpSettings(guard)
    if any(server.id == SERVER_ID for server in settings.load().servers):
        settings.remove(SERVER_ID)
    settings.add_server(SERVER_ID, 'Playwright (web tasks)', node, launch)
    found = discover(guard, settings.load(), SERVER_ID)
    if found['status'].get('state') != 'ready':
        raise SystemExit('Playwright MCP did not start: ' + str(found['status']))
    names = {tool['name'] for tool in found['tools']}
    missing = set(PLAYWRIGHT_PINS) - names
    if missing:
        raise SystemExit('The server did not report: ' + ', '.join(sorted(missing)))
    print(f'The server reports {len(names)} tools. Only these are pinned (family public_web):')
    for name, pin in PLAYWRIGHT_PINS.items():
        blocked = f", model may not set {', '.join(pin['blocked_args'])}" if pin.get('blocked_args') else ''
        print(f"  {name}: effect={pin['effect']}, timeout {pin['timeout_s']}s, up to {pin['max_chars']} characters"
              + blocked)
    if not args.yes and input('Approve these three tools? [y/N] ').strip().lower() != 'y':
        settings.remove(SERVER_ID)
        raise SystemExit('Not approved; the server entry was removed.')
    settings.approve(SERVER_ID, found['tools'], PLAYWRIGHT_PINS)
    print('Approved. Restart the HUD; then ask in chat, for example "제주 날씨 찾아줘".')


if __name__ == '__main__':
    main()
