"""Health check of the configured MCP servers (board 204bc125 part 5). Calls no tool; for the user, not a model.

For each server in mcp_servers.json: the program file, the fixed settings (the same checks as a session start, without
naming values), the approved folders, the pins by effect, the REA preset version for the 'rea' server, one pins-only
start (ready/failed/changed, seconds, offered tools) and a summary of its audit log. Plus the audit lines that could
not be written (fail-open read calls) as logged in later lines and as counted in this process.
"""
from collections import Counter
from pathlib import Path
import time

from jarvis.features.mcp_audit.service import AuditLog, missed_writes
from jarvis.features.mcp_client.service import McpConfig, McpError, McpSession, McpSettings, valid_root
from jarvis.features.mcp_presets.service import REA_SERVER_ID, PresetError, rea_launch


def check_server(guard, server, *, start=True, session_factory=McpSession, audit=None, days=7):
    checks = []
    command = Path(server.command)
    checks.append({'name': 'program', 'ok': command.is_file() and not command.is_symlink(),
                   'detail': '' if command.is_file() else '실행 파일이 없습니다.'})
    if server.fixed_env:
        unstarted = McpSession(guard, McpConfig(servers=(server,)), audit=audit)
        ok = unstarted._fixed_env(server) is not None
        checks.append({'name': 'fixed_settings', 'ok': ok,
                       'detail': '' if ok else '고정 설정 폴더가 없거나 허용되지 않습니다. 설정 스크립트를 다시 실행해 주십시오.'})
    bad_roots = [root for root in server.roots if not valid_root(root) or not _is_folder(guard, root)]
    checks.append({'name': 'roots', 'ok': not bad_roots,
                   'detail': ('없거나 쓸 수 없는 폴더: ' + ', '.join(bad_roots)) if bad_roots else ''})
    if server.id == REA_SERVER_ID:
        try:
            rea_launch(guard)
            checks.append({'name': 'rea_version', 'ok': True, 'detail': ''})
        except PresetError as exc:
            checks.append({'name': 'rea_version', 'ok': False, 'detail': str(exc)[:300]})
    report = {'id': server.id, 'label': server.label, 'state': server.state, 'checks': checks,
              'pins': dict(Counter(pin.effect for pin in server.tools)), 'session': None,
              'audit': audit_summary((audit or AuditLog(guard)).entries(days=days, server=server.id))}
    if start and server.state == 'approved':
        began = time.monotonic()
        try:
            with session_factory(guard, McpConfig(servers=(server,)), audit=audit) as session:
                report['session'] = {**session.status.get(server.id, {'state': 'failed'}),
                                     'seconds': round(time.monotonic() - began, 1), 'offered': len(session.offered())}
        except (McpError, OSError) as exc:
            report['session'] = {'state': 'failed', 'reason': str(exc)[:200],
                                 'seconds': round(time.monotonic() - began, 1), 'offered': 0}
    return report


def _is_folder(guard, root):
    try:
        return guard.validate_path(root).is_dir()
    except Exception:  # noqa: BLE001 - any refusal means the folder cannot be used
        return False


def audit_summary(rows):
    """Outcomes of finished calls, refusals by reason, attempts that never got an outcome, and the last call."""
    attempts = {row['id']: row for row in rows if row.get('ev') == 'attempt' and row.get('id')}
    outcomes = [row for row in rows if row.get('ev') == 'outcome']
    finished = {row.get('id') for row in outcomes}
    return {'calls': len(attempts), 'outcomes': dict(Counter(str(row.get('outcome')) for row in outcomes)),
            'refused': dict(Counter(str(row.get('reason')) for row in rows if row.get('ev') == 'refused')),
            'unfinished': len([key for key in attempts if key not in finished]),
            'last': max((row.get('t', '') for row in rows), default='')}


def doctor(guard, *, start=True, session_factory=McpSession, audit=None, days=7):
    """{'servers': [check_server(...)], 'audit': {'missed_logged', 'missed_this_process', 'cap_days'}}."""
    audit = audit or AuditLog(guard)
    servers = [check_server(guard, server, start=start, session_factory=session_factory, audit=audit, days=days)
               for server in McpSettings(guard).load().servers]
    rows = audit.entries(days=days)
    return {'servers': servers,
            'audit': {'missed_logged': sum(int(row.get('missed') or 0) for row in rows),
                      'missed_this_process': missed_writes(),
                      'cap_days': sorted({str(row.get('t', ''))[:10] for row in rows if row.get('ev') == 'cap'})}}


def report_lines(result, days=7):
    """The doctor result as Korean lines for a console."""
    lines = []
    for server in result['servers']:
        session = server['session']
        head = f"[{server['label']}] ({server['id']}) {'승인됨' if server['state'] == 'approved' else '승인 전'}"
        if session:
            head += f" - 시작 {session['state']}, {session['seconds']}초, 제공 도구 {session['offered']}개"
            if session.get('tools'):
                head += ', 바뀐 도구: ' + ', '.join(session['tools'][:8])
            if session.get('reason'):
                head += ', 이유: ' + session['reason']
        lines.append(head)
        lines.append('  고정: ' + (', '.join(f'{effect} {count}' for effect, count in sorted(server['pins'].items()))
                                 or '없음'))
        for check in server['checks']:
            lines.append(f"  {'통과' if check['ok'] else '문제'} {check['name']}" + (f": {check['detail']}"
                                                                                  if check['detail'] else ''))
        audit = server['audit']
        lines.append(f"  최근 {days}일 호출 {audit['calls']}회, 결과 {audit['outcomes'] or '{}'}, 거부 {audit['refused'] or '{}'}"
                     + (f", 끝나지 않은 호출 {audit['unfinished']}회" if audit['unfinished'] else '')
                     + (f", 마지막 {audit['last']}" if audit['last'] else ''))
    audit = result['audit']
    lines.append(f"호출 기록 실패: 기록된 것 {audit['missed_logged']}건, 이 프로세스 {audit['missed_this_process']}건"
                 + (f", 하루 한도에 닿은 날: {', '.join(audit['cap_days'])}" if audit['cap_days'] else ''))
    return lines
