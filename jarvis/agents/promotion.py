"""W4-3 probation review: suggest probation employees ready for promotion. Suggestions only.

Input is plain data (the roster, an evaluation report and adapter problem codes), so this stays inside the
agents layer. Only exactly counted work (full board status history) is used. The roster status is never
changed here; the user edits config/agents.json.
"""

NO_RECORD = '아직 판단할 기록 없음'
NOTE = '제안만 함. 명부 status는 사용자가 config/agents.json에서 직접 바꿈.'


def review(roster, report, problem_codes):
    """{'criteria', 'candidates', 'not_ready', 'note'} for every probation employee in the roster.

    report: features.evaluation report; problem_codes: {employee: [code, ...]} from worker_adapters."""
    criteria = roster.promotion
    candidates, not_ready = [], []
    for name, agent in sorted(roster.agents.items()):
        if agent.status != 'probation':
            continue
        row = report.get('employees', {}).get(name, {})
        done, reworked = row.get('exact_done', 0), row.get('exact_reworked_done', 0)
        ratio = round(reworked / done, 3) if done else None
        reasons = []
        if not done:
            reasons.append(NO_RECORD)
        elif done < criteria.min_exact_done:
            reasons.append(f'정확 집계 done {done}건 < 기준 {criteria.min_exact_done}건')
        if ratio is not None and ratio > criteria.max_rework_ratio:
            reasons.append(f'재작업 비율 {ratio} > 기준 {criteria.max_rework_ratio}')
        blocking = [code for code in problem_codes.get(name, []) if code in criteria.blocking_problems]
        if blocking:
            reasons.append('어댑터 점검: ' + ', '.join(blocking))
        entry = {'employee': name, 'done': done, 'reworked': reworked, 'ratio': ratio}
        if reasons:
            not_ready.append({**entry, 'reasons': reasons})
        else:
            candidates.append(entry)
    return {'criteria': criteria.model_dump(), 'candidates': candidates, 'not_ready': not_ready, 'note': NOTE}


def digest_line(result):
    """'- 승격 후보: cursor(done 5, 재작업 0)' or None when there is no candidate."""
    parts = [f"{item['employee']}(done {item['done']}, 재작업 {item['reworked']})" for item in result['candidates'][:10]]
    return '- 승격 후보: ' + ', '.join(parts) if parts else None
