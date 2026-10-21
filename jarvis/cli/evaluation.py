"""W4-5 work evaluation CLI: per-employee board counts as JSON. Read-only; never writes the board."""
import argparse
import sys
import time

from jarvis.features.evaluation.service import EvaluationError, dumps, evaluate
from jarvis.app.runtime import load_guardrails
from jarvis.core.guardrails import GuardrailError


def main(argv=None):
    parser = argparse.ArgumentParser(description='자비스 업무 평가: 게시판 기록에서 직원별 완료·지적·재작업·소요 시간 (근사치, 읽기 전용).')
    parser.add_argument('--days', type=int, help='최근 N일 안에 만든 작업만 집계 (1-365).')
    args = parser.parse_args(argv)
    if args.days is not None and not 1 <= args.days <= 365:
        parser.error('--days must be 1-365.')
    now = time.time()
    try:
        report = evaluate(load_guardrails(), now=now, since=now - args.days * 86400 if args.days else None)
    except (EvaluationError, GuardrailError) as exc:
        print(f'evaluation unavailable: {exc}', file=sys.stderr)
        return 1
    print(dumps(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
