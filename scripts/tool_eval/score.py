"""Scoring rules for the REA tool-task eval. Pure functions: no model, no tool server.

A task passes when every check below holds:
- the answer is not empty;
- every `answer_all` regex matches the answer (case-insensitive), not counting the '사용한 도구' line JARVIS adds,
  so a tool name alone never satisfies a check;
- every tool in `tools_ok` was called at least once with an ok result;
- no tool outside the REA preset was called (the session offers only the pins, so this catches harness mistakes).
"""
import re

USED_LINE = '\n\n사용한 도구:'


def score(task, answer, traces, pinned):
    """traces: [{'name', 'status'}] with names like mcp:rea/open_binary. Returns {'pass', 'checks': {name: bool}}."""
    body = (answer or '').split(USED_LINE, 1)[0]
    ok = {trace['name'].split('/', 1)[-1] for trace in traces if trace.get('status') == 'ok'}
    called = {trace['name'].split('/', 1)[-1] for trace in traces}
    checks = {'answered': bool(body.strip()),
              'answer_all': all(re.search(pattern, body, re.IGNORECASE) for pattern in task['answer_all']),
              'tools_ok': set(task.get('tools_ok', ())) <= ok,
              'only_pins': called <= set(pinned)}
    return {'pass': all(checks.values()), 'checks': checks}
