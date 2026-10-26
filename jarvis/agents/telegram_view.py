"""PA4-T1: Telegram status replies as plain data in, plain text out. No network, storage or board access.

Commands are fixed (/status, /task <id prefix>); free text is never interpreted. Replies carry only task ids,
redacted titles (max 60 characters), fixed Korean labels, owner employee names and times: never notes, details,
paths, commands or logs (the alerts.py whitelist rule).
"""
import re
import time

from jarvis.core.redaction import redact

MAX_INPUT = 64        # longer messages get the help text only
MAX_REPLY = 1500      # well below the Bot API sendMessage limit of 4096 characters
BACKLOG_SECONDS = 600  # a message older than this when read was sent while the PC or poller was off
STATUS_LABELS = {'open': '대기', 'claimed': '진행 중', 'blocked': '막힘', 'review': '검토 대기',
                 'done': '완료', 'cancelled': '취소'}
SERVICE_LABELS = {'running': '실행 중', 'stopped': '꺼짐', 'not_registered': '등록 안 됨',
                  'identity_mismatch': '알 수 없음', 'unknown': '알 수 없음'}
HELP = ('자비스 조회 전용 봇입니다.\n'
        '/status - 맡긴 일 진행 상황\n'
        '/task <ID 앞 6자리 이상> - 한 건 보기\n'
        '1분 안에 답이 없으면 PC가 꺼져 있거나 조회 프로그램이 멈춘 상태입니다.')
_COMMAND = re.compile(r'/(status|task|start|help)(?:@[A-Za-z0-9_]{1,64})?(?:\s+(\S+))?')
_PREFIX = re.compile(r'[0-9a-fA-F]{6,32}')


def parse(text):
    """('status', None) | ('task', prefix) | ('help', None) | ('bad_task', None). Anything else is help."""
    if not isinstance(text, str) or len(text) > MAX_INPUT:
        return ('help', None)
    match = _COMMAND.fullmatch(text.strip())
    if match is None:
        return ('help', None)
    command, argument = match.groups()
    if command == 'status' and argument is None:
        return ('status', None)
    if command == 'task':
        if argument is not None and _PREFIX.fullmatch(argument):
            return ('task', argument.lower())
        return ('bad_task', None)
    return ('help', None)


def clock_text(seconds):
    return time.strftime('%m-%d %H:%M', time.localtime(seconds))


def age_text(seconds, now):
    elapsed = max(0, int(now - seconds))
    if elapsed < 60:
        return '방금'
    if elapsed < 3600:
        return f'{elapsed // 60}분 전'
    if elapsed < 172800:
        return f'{elapsed // 3600}시간 전'
    return f'{elapsed // 86400}일 전'


def _title(task):
    return redact(str(task.get('title') or '')).replace('\n', ' ')[:60]


def line(task, now):
    owner = str(task.get('owner') or '-').split(':')[0]
    status = STATUS_LABELS.get(task.get('status'), '알 수 없음')
    text = (f"[{task['id'][:8]}] {_title(task)} | {status} | 담당 {owner} | "
            f"갱신 {clock_text(task['updated'])} ({age_text(task['updated'], now)})")
    return text + (' | 멈춤 의심(담당 갱신 없음)' if task.get('stale') else '')


def header(now, service):
    """service: {'status': <service_state status>, 'paused': bool} or None when it could not be read."""
    if service is None:
        dispatcher = '알 수 없음'
    elif service.get('paused'):
        dispatcher = '일시정지'
    else:
        dispatcher = SERVICE_LABELS.get(service.get('status'), '알 수 없음')
    return f'자비스 상태 (관측 {clock_text(now)}, 디스패처: {dispatcher})'


def status_reply(view, now, service):
    """view: BoardStore.created_by(...) without prefix, or None when the board could not be read."""
    if view is None:
        return finish(header(now, service) + "\n상태 확인 불가(게시판을 읽지 못함). '작업 없음'이 아닙니다.")
    parts = [header(now, service), f"진행 중인 맡긴 일 {view['active_total']}건"]
    parts += ['- ' + line(task, now) for task in view['active']]
    if view['active_total'] > len(view['active']):
        parts.append(f"- 외 {view['active_total'] - len(view['active'])}건")
    if view['closed']:
        parts.append('최근 24시간 완료/취소')
        parts += ['- ' + line(task, now) for task in view['closed']]
    return finish('\n'.join(parts))


def task_reply(matches, now, service):
    """matches: BoardStore.created_by(prefix=...)['matches'], or None when the board could not be read."""
    if matches is None:
        return finish(header(now, service) + '\n상태 확인 불가(게시판을 읽지 못함).')
    if not matches:
        return finish('그 ID로 시작하는 맡긴 일이 없습니다.')
    if len(matches) > 1:
        return finish('그 ID로 시작하는 일이 여러 건입니다. 더 길게 입력해 주세요.')
    task = matches[0]
    return finish(header(now, service) + f"\nID {task['id']}\n" + line(task, now))


def backlog_prefix(count, oldest, now):
    """Opening line for questions read late: the PC or the poller was off when they were sent."""
    return (f'PC가 꺼져 있었거나 조회가 멈춘 동안 받은 질문 {count}건 '
            f'(가장 오래된 것 {clock_text(oldest)}, {age_text(oldest, now)}). 지금 상태로 한 번에 답합니다.')


def finish(text):
    """Final pass on every reply: redaction, no double quotes, bounded length."""
    text = redact(text).replace('"', "'")
    return text if len(text) <= MAX_REPLY else text[:MAX_REPLY - 1] + '…'
