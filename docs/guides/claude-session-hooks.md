# Claude Code 세션 상태 보고 (hook 어댑터)

[문서 안내](../README.md) · [채팅·세션 상태](chat.md) · [게시판](board.md)

Claude Code의 공식 hook을 Codex와 같은 공통 세션 신호로 바꿔 자비스 세션 상태 저장소에 보낸다. 저장하는 것은 6개 필드뿐이다: `employee`(claude-code), `session_id`, `event_id`, `turn_id`, `event`, `source`(self_reported). 프롬프트, 도구 입력, 도구 결과, 응답, transcript 경로는 받더라도 버린다.

- 코드: `jarvis/agents/claude_session_signals.py`(변환), `jarvis/cli/claude_session_signal.py`(hook 수신기). Codex의 `session_signals.py`·`session_status.py`를 가져다 쓰며 수정하지 않는다.
- 예시 설정: `config/session-hooks.claude.example.json`. 설치된 것이 아니다.

## 이벤트 대응

| Claude Code hook | 공통 신호 |
|---|---|
| SessionStart | session_started |
| UserPromptSubmit | turn_started |
| PreToolUse | tool_started |
| PostToolUse, PostToolUseFailure | tool_finished |
| PermissionRequest | permission_requested |
| Stop, StopFailure(API 오류로 턴 종료) | turn_stopped |
| SessionEnd | session_ended |

- **미연결: interrupted.** 공식 문서상 사용자 중단에 실행되는 hook이 없고, Stop은 중단 때 실행되지 않는다. 추정해서 만들지 않는다.
- Subagent, Notification 등 나머지 hook은 대응하지 않는다.

## turn_id

Claude Code hook 입력에는 `turn_id`가 없다. 모든 이벤트의 공통 필드 `prompt_id`("처리 중인 사용자 프롬프트의 UUID", 첫 입력 전에는 없음)를 `turn_id`로 쓴다. 상태 파일이 필요 없다. 턴 이벤트에 `prompt_id`가 없거나 형식이 맞지 않으면 그 이벤트는 버린다.

**설치 전 확인 항목:** 실제 세션 하나에서 `prompt_id`가 UserPromptSubmit부터 Stop까지 같은 값인지 확인한다. 문서 정의로만 확인했다(2026-10-21).

## 수신기 규칙

- stdin 최대 256KB를 읽고, `cwd`가 `--project-root` 아래일 때만 처리한다. `cwd`는 저장하지 않는다.
- 아무것도 출력하지 않고 항상 exit 0으로 끝난다. Claude Code는 SessionStart·UserPromptSubmit의 stdout을 대화 문맥에 넣고, exit 2는 동작을 막기 때문이다.
- 저장 실패, 잠금 대기, 잘못된 입력은 조용히 넘긴다. 전달은 보장하지 않는다.

## 설치 (사용자 승인 후)

1. `python jarvis/cli/claude_session_signal.py --workspace <작업공간> --project-root <저장소> --render-config`로 실제 절대 경로가 들어간 설정을 출력한다. 경로는 존재하는 절대 경로여야 하고, 링크나 셸 특수문자가 있으면 거부한다.
2. 출력은 exec 형식(`command` + `args`)이라 셸을 거치지 않는다. 모든 hook은 `async: true`로 실행된다. 예시의 `timeout: 5`는 비동기 hook에서 강제되지 않으므로 5초 종료를 보장하지 않는다. 수신기의 저장소 잠금 대기는 0.2초이며, stdin 전달·프로세스 실행 시간의 상한은 별도로 보장하지 않는다.
3. 검토한 뒤 `hooks` 블록을 Claude Code 설정 파일에 넣는 것은 사용자가 승인한 경우에만 한다. 이 코드는 설정 파일을 쓰지 않는다.

## 검증 범위

PR2 병합으로 저장소는 codex와 claude-code를 구분해 받는다. 합성 이벤트의 CLI → 저장소 연결을 검증하며, 실제 Claude 플랫폼의 자동 보고와 prompt_id 연속성은 별도 미검증이다. 설치·신뢰·설정 파일 변경이나 모델 호출은 수행하지 않는다.