"""W4-3 promotion CLI: probation employees ready for promotion, as JSON. Read-only; never edits the roster."""
import argparse
import json
import sys
import time

from jarvis.agents.promotion import review
from jarvis.agents.worker_adapters import roster_problem_codes
from jarvis.app.runtime import load_guardrails
from jarvis.core.guardrails import GuardrailError
from jarvis.core.roster import RosterError, load_roster
from jarvis.features.evaluation.service import EvaluationError, evaluate


def promotion_review(guard, *, now, roster=None):
    roster = roster if roster is not None else load_roster(workspace=guard.workspace)
    return review(roster, evaluate(guard, now=now), roster_problem_codes(roster))


def main(argv=None):
    argparse.ArgumentParser(description='자비스 수습 검토: 승격 후보 제안 (명부는 바꾸지 않음).').parse_args(argv)
    try:
        result = promotion_review(load_guardrails(), now=time.time())
    except (EvaluationError, GuardrailError, RosterError) as exc:
        print(f'promotion review unavailable: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
