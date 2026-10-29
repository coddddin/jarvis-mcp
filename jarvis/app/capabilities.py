"""Code-owned descriptions of the chat's read-only tools, shared by metadata, the per-turn prompt/schema and the
fixed capability and failure answers.

Registration (a guarded callable in ChatService.tools) is not provider health, a successful call or permission for
a request. Nothing here probes, downloads or calls a tool: health is always reported as not_checked."""
from dataclasses import dataclass
import re

from jarvis.agents.chat import ChatError
from jarvis.core.guardrails import GuardrailError
from jarvis.features.web.service import WebError


@dataclass(frozen=True)
class Capability:
    tool_id: str
    label: str          # user-facing Korean name
    description: str    # model-facing argument contract
    sends_out: bool     # sends a query or URL out of this PC


CAPABILITIES = (
    Capability('read_public_web_page', '검색된 공개 페이지 원문 일부 읽기',
               'Read one page from successful search results IN THIS REQUEST. '
               'argument is the exact returned URL, never an invented or modified URL. '
               'Use after search when the user asks for page contents or verification.', True),
    Capability('get_system_status', 'PC 상태 확인', 'Read CPU, memory and disk status. argument must be empty.', False),
    Capability('search_local_documents', '로컬 문서 검색', 'Search indexed local documents. argument is the search query.', False),
    Capability('search_public_web', '공개 웹 검색 (검색 발췌)',
               'Search the public web with Bing, max 3 excerpts. argument is a concise public query, 2–200 characters, '
               'starting with the main topic. Prefer English for international software topics. '
               'No secrets. Result pages are not read; do not claim live rankings or verified page contents.', True),
    Capability('list_web_sources', '등록된 웹 출처 확인', 'List registered public source IDs. argument must be empty.', False),
    Capability('collect_web_information', '등록된 공개 출처(피드·문서·날씨) 읽기',
               'Read one registered public source (feed, documentation or weather). '
               'argument is an exact source_id returned by list_web_sources. '
               'Does not search the web or save a report.', True),
    Capability('read_registered_feed_with_agent_reach', '공개 피드 읽기',
               'Read RSS/Atom feeds only, never documentation or weather. '
               'argument is an exact registered feed source_id.', True),
    Capability('analyze_program_static', '직접 지정한 프로젝트 파일의 정적 분석',
               'Statically analyze one project JS/PE file with REA, without executing it. '
               'argument is an exact projects/... path explicitly supplied in the latest user message.', False),
)
BY_ID = {item.tool_id: item for item in CAPABILITIES}
DESCRIPTIONS = {item.tool_id: item.description for item in CAPABILITIES}
SENDS_OUT = frozenset(item.tool_id for item in CAPABILITIES if item.sends_out)
UNSUPPORTED = '동영상 재생·시청(유튜브 영상 내용 확인 포함), 로그인·쿠키 접근, 파일 변경·삭제, 코드 적용, 프로그램 실행'


def descriptors(registered, allowed=None):
    """JSON-safe descriptors for the registered tool IDs only, in code order. request_allowed is None when no
    request's allowed set is given; health is never probed."""
    names = set(registered)
    allowed = None if allowed is None else set(allowed)
    return [{'id': item.tool_id, 'label': item.label, 'registered': True, 'health': 'not_checked',
             'request_allowed': None if allowed is None else item.tool_id in allowed, 'sends_out': item.sends_out}
            for item in CAPABILITIES if item.tool_id in names]


def prompt_tools(names):
    return {name: DESCRIPTIONS[name] for name in names if name in DESCRIPTIONS}


def prompt_summary(names):
    """The capability sentence of this turn's system prompt, from the same tool list as its schema."""
    labels = [BY_ID[name].label for name in names if name in BY_ID]
    text = ('이번 요청에서 사용 가능한 기능은 일반 대화, 승인된 노트 검색(노트를 언급한 질문)'
            + ''.join(', ' + label for label in labels) + '이다. '
            '도구 호출이 실패하거나 결과가 없어도 그 기능이 없다는 뜻이 아니다. 이 목록의 기능을 없다고 말하지 마라. '
            '할 수 없는 일: ' + UNSUPPORTED + '. ')
    if 'search_public_web' in names:
        text += '공개 웹 검색은 검색 발췌만 반환하며 동영상을 보거나 재생하지 않는다. '
    return text


def capability_reply(registered, allowed=None):
    """Fixed answer to a capability-only question: registered tools in code order, never probed."""
    allowed = set(registered) if allowed is None else set(allowed)
    lines = ['저는 자비스, 이 PC에서 일하는 개인 비서입니다. 지금 연결된 기능은 다음과 같습니다.',
             '- 일반 대화와 최근 대화 문맥 유지', '- 승인된 Vault 노트 검색 (노트를 가리킨 질문, 색인 필요)']
    for item in CAPABILITIES:
        if item.tool_id in registered:
            lines.append('- ' + item.label + ('' if item.tool_id in allowed else ' (지금 선택한 모델로는 쓰지 않음)'))
    lines.append('할 수 없는 일: ' + UNSUPPORTED + '.')
    lines.append('기능이 등록되어 있는지만 확인했고, 각 연결의 현재 상태는 실제로 요청할 때 확인됩니다.')
    return '\n'.join(lines)


# The whole message must be the capability question; anything else ("... 철구 검색해줄래") goes to the normal path.
_WHO = r'(?:(?:너|넌|니가|네가|너가|자비스|당신|jarvis)\s*(?:는|가|은|야|,)?\s*)?'
_NOW = r'(?:(?:지금|현재|여기서|이\s*채팅에서)\s*)?'
_END = r'(?:어요?|니|나요?|냐|는데요?|는지|습니까|으세요|으신가요|아요?)?'
_ASK = r'(?:알려\s*(?:줘|주세요|줄래|주실래요)|말해\s*(?:줘|주세요)|보여\s*(?:줘|주세요)|소개해\s*(?:줘|주세요)|목록)'
_WHAT = r'(?:뭐(?:야|예요|에요|니|지)?|뭔데|무엇(?:인가요|입니까)?)'
CAPABILITY_ONLY = re.compile(
    r'\s*(?:' + _WHO + _NOW + r'(?:'
    r'(?:뭘|뭐|무엇을|무얼|뭐를)\s*(?:할\s*수\s*있|할\s*줄\s*알|도와\s*줄\s*수\s*있)' + _END +
    r'|(?:어떤|무슨)\s*기능(?:들)?(?:이|을|를)?\s*(?:할\s*수\s*)?있' + _END +
    r'|(?:어떤|무슨)\s*일(?:을|를)?\s*할\s*수\s*있' + _END +   # not "무슨 일 있어?" (what's up)
    r'|할\s*수\s*있는\s*(?:게|것|거|일|기능)(?:들)?(?:이|은|는|을|를)?\s*(?:' + _ASK + r'|뭐\s*있' + _END + '|' + _WHAT + r')'
    r'|(?:기능|능력)(?:들)?(?:이|은|는|을|를)?\s*(?:' + _ASK + r'|뭐\s*있' + _END + r')'
    r'|사용\s*(?:가능한|할\s*수\s*있는)\s*(?:기능|도구)(?:들)?(?:은|는|이|을|를)?\s*(?:' + _ASK + r'|뭐(?:야|예요|에요)?)?'
    r')'
    r'|(?:너|네|너의|당신의?|자비스의?)\s*(?:기능|능력)(?:들)?(?:이|은|는)?\s*' + _WHAT +
    r'|(?:hey\s+)?(?:jarvis\s*,?\s*)?(?:what\s+(?:else\s+)?can\s+you\s+(?:do|help\s+(?:me\s+)?with)'
    r'|what\s+are\s+your\s+(?:capabilities|features|tools)|what\s+(?:tools|capabilities|features)\s+do\s+you\s+have'
    r'|(?:list|show)\s+(?:me\s+)?your\s+(?:capabilities|features|tools)|what\s+are\s+you\s+able\s+to\s+do)'
    r'(?:\s+(?:now|here))?'
    r')[\s?!.~]*', re.I)


def is_capability_only(message):
    return len(message) <= 60 and CAPABILITY_ONLY.fullmatch(message) is not None


OUTCOMES = ('no_results', 'unavailable', 'blocked', 'timeout', 'failed')
CAUSE_LIMIT = 8


class ToolRefused(ChatError):
    """The chat's own admission check refused the call before the tool ran."""


def _chain(exc):
    """The exception and its causes, oldest last, at most CAUSE_LIMIT and never revisiting one (cycles)."""
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < CAUSE_LIMIT:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)


def guardrail_cause(exc):
    """A stop or budget refusal inside the chain (guarded_tool wraps it in ToolError), or None."""
    return next((item for item in _chain(exc) if isinstance(item, GuardrailError)), None)


def failure_outcome(exc):
    """Failure category from trusted exception types only; messages are never read. Unknown means failed."""
    for item in _chain(exc):
        if isinstance(item, WebError) and item.outcome in OUTCOMES:
            if item.outcome != 'failed':
                return item.outcome
        elif isinstance(item, ToolRefused):
            return 'blocked'
        elif isinstance(item, TimeoutError):
            return 'timeout'
    return 'failed'


# Model-facing trace detail per outcome. Raw exception text never reaches the model or the user.
FAILURE_DETAIL = {
    'no_results': '도구는 실행됐지만 요청과 맞는 결과가 없었습니다. 기능이 없다는 뜻이 아닙니다. 성공으로 보고하지 마세요.',
    'unavailable': '도구는 등록돼 있지만 지금 실행 경로를 쓸 수 없었습니다. 성공으로 보고하지 마세요.',
    'blocked': '도구 요청이 안전 규칙이나 입력 조건에 맞지 않아 실행되지 않았습니다. 성공으로 보고하지 마세요.',
    'timeout': '도구 요청이 시간 한도 안에 끝나지 않았습니다. 성공으로 보고하지 마세요.',
    'failed': '도구 요청이 거부되었거나 실행에 실패했습니다. 성공으로 보고하지 마세요.',
}


def failure_reply(tool_id, outcome):
    """Fixed user answer for a turn whose every tool call failed. Nothing is retried automatically."""
    label = BY_ID[tool_id].label if tool_id in BY_ID else '요청한 도구'
    if outcome == 'no_results':
        if tool_id == 'search_public_web':
            return ('공개 웹 검색은 실행했지만 검색어와 맞는 발췌를 찾지 못했습니다. 검색 기능이 없다는 뜻은 아닙니다. '
                    '검색어를 더 구체적으로 바꿔 다시 요청해 주십시오.')
        return label + '은(는) 실행했지만 요청과 맞는 결과가 없었습니다. 기능이 없다는 뜻은 아닙니다.'
    if outcome == 'unavailable':
        return label + ' 기능은 등록되어 있지만 지금 실행 경로를 쓸 수 없어 결과를 받지 못했습니다.'
    if outcome == 'blocked':
        return label + ' 요청이 안전 규칙이나 입력 조건에 맞지 않아 실행하지 않았습니다.'
    if outcome == 'timeout':
        return label + ' 요청이 시간 한도 안에 끝나지 않아 결과를 받지 못했습니다. 자동으로 다시 시도하지 않았습니다.'
    return '요청하신 정보를 실제 도구 결과로 확인하지 못했습니다. ' + label + ' 요청이 실패했고 원인은 확인되지 않았습니다.'
