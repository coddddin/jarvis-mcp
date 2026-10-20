"""Inspect registered owned processes; this CLI cannot register arbitrary PIDs or terminate."""
import argparse
import json
import sys

from jarvis.agents.process_registry import ProcessRegistry
from jarvis.app.runtime import load_guardrails


def main():
    parser = argparse.ArgumentParser(description='자비스 소유 프로세스 관찰. 종료·재시작 기능 없음.')
    parser.add_argument('action', choices=['inspect'])
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    try:
        guard = load_guardrails()
        guard.enter_call(tier='light')
        result = ProcessRegistry(guard).inspect(limit=args.limit)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 2 if result['warning_count'] else 0
    except Exception as exc:
        print(json.dumps({'status': 'not_verified', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
