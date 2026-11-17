"""One-time setup for REA as a JARVIS tool server (board 204bc125 part 4): approve the static analysis pins.

  .venv\\Scripts\\python.exe scripts\\setup_rea_tools.py                 show what will be approved, then ask
  .venv\\Scripts\\python.exe scripts\\setup_rea_tools.py --yes           same, without the question (the user ran it)
  .venv\\Scripts\\python.exe scripts\\setup_rea_tools.py --root projects/re   folders REA may open (repeatable)

What it does, in order:
1. Check the installed REA (.integrations/rea, rea-agents 6.7.0 from its lockfile; paths in config/integrations.json)
   together with Ghidra and Java. Nothing is downloaded.
2. Add the server 'rea' (label REA) to JarvisWorkspace/.jarvis-settings/mcp_servers.json, started as
   `node rea.mjs --mcp` with GHIDRA_INSTALL_DIR, JAVA_HOME and GHIDRA_HEADLESS_MAXMEM as fixed settings. An existing
   'rea' entry is replaced.
3. Start it once, list its tools and pin only the static read tools of jarvis.features.mcp_presets (family local,
   effect read) after you confirm. Debuggers, devices, process/UI/browser capture and tools that write are not pinned.
4. Approve the folders binaries may be opened from (default: projects).
Restart the HUD afterwards; then ask in chat, for example "REA로 projects/demo/app.exe 개요 보여줘".
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.core.guardrails import Guardrails  # noqa: E402
from jarvis.core.paths import DEFAULT_WORKSPACE  # noqa: E402
from jarvis.features.mcp_presets.service import REA_PINS, REA_VERSION, PresetError, setup_rea  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--yes', action='store_true', help='approve the listed tools without asking')
    parser.add_argument('--root', action='append', help='workspace folder REA may open files from (default projects)')
    args = parser.parse_args()
    guard = Guardrails(DEFAULT_WORKSPACE)
    roots = tuple(args.root or ['projects'])

    def confirm(found):
        print(f'REA {REA_VERSION} reports {len(found["tools"])} tools. Only these {len(REA_PINS)} are pinned '
              '(family local: results never go to an external model; effect read):')
        for name, pin in REA_PINS.items():
            extra = ''
            if pin.get('path_args'):
                extra += ', path ' + '/'.join(item['name'] for item in pin['path_args'])
            if pin.get('blocked_args'):
                extra += ', model may not set ' + ', '.join(pin['blocked_args'])
            print(f"  {name}: timeout {pin['timeout_s']}s{extra}")
        print('Folders REA may open files from: ' + ', '.join(roots))
        return args.yes or input('Approve these tools? [y/N] ').strip().lower() == 'y'

    try:
        setup_rea(guard, roots=roots, confirm=confirm)
    except PresetError as exc:
        raise SystemExit(str(exc)) from None
    print('Approved. Restart the HUD; then ask in chat, for example "REA로 projects/demo/app.exe 개요 보여줘".')


if __name__ == '__main__':
    main()
