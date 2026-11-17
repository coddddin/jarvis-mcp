"""Check every MCP server JARVIS is set up to use (board 204bc125 part 5). Calls no tool.

  .venv\\Scripts\\python.exe scripts\\mcp_doctor.py              start each approved server once and report
  .venv\\Scripts\\python.exe scripts\\mcp_doctor.py --no-start   configuration and audit log only
  .venv\\Scripts\\python.exe scripts\\mcp_doctor.py --days 30    audit summary over 30 days (kept at most 30)

Reads JarvisWorkspace/.jarvis-settings/mcp_servers.json and JarvisWorkspace/.jarvis-mcp-audit/. It never changes the
settings or calls a tool; it may create the empty log folder and, when starting servers, their .jarvis-mcp/<id>
working folders, as any task session does.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.core.guardrails import Guardrails  # noqa: E402
from jarvis.core.paths import DEFAULT_WORKSPACE  # noqa: E402
from jarvis.features.mcp_doctor.service import doctor, report_lines  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--no-start', action='store_true', help='do not start the servers')
    parser.add_argument('--days', type=int, default=7, choices=range(1, 31), metavar='1-30')
    args = parser.parse_args()
    result = doctor(Guardrails(DEFAULT_WORKSPACE), start=not args.no_start, days=args.days)
    if not result['servers']:
        print('설정된 MCP 서버가 없습니다.')
    for line in report_lines(result, args.days):
        print(line)


if __name__ == '__main__':
    main()
