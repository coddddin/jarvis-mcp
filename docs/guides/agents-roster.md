# AI 직원 명부와 온보딩

[문서 안내](../README.md) · [현재 계획](../PLAN.md)

자비스와 함께 일하는 AI 도구는 `config/agents.json` 한 곳에 등록한다. 게시판과 무료 배정은 이 명부를 읽으므로, 새 직원을 들일 때 코드를 고치지 않는다.

## 현재 명부 (2026-10-21)

| ID | 이름 | 상태 | 최대 등급 | 비고 |
|---|---|---|---|---|
| `codex` | OpenAI Codex | active | sensitive | 주 개발 |
| `claude-code` | Claude Code | active | sensitive | 검토·개발 |
| `cursor` | Cursor | probation | standard | 2026-10-21 합류. CLI `agent` 2026.10.19 설치·로그인, 게시판 첫 작업 완료 |
| `opencode` | OpenCode | candidate | light | 무료 배정 후보, CLI 무료 모델 403 |
| `gemini` | Gemini CLI | candidate | light | 이 PC에서 미발견 |

## 항목

- `kind`: `cli`, `api`, `mcp-client`, `local-model`
- `command`: 실행 파일 이름만. 셸 명령줄은 거부한다.
- `capabilities`: `research`, `coding`, `review`, `summary`
- `cost`: `free`, `subscription`, `paid`, `unknown`
- `status`: `active`(정식), `probation`(수습: 제안·검토만), `candidate`(후보: 아직 일하지 않음), `retired`(퇴사: 게시판·배정 거부)
- `max_tier`: 허용 의도 등급 `light`/`standard`/`sensitive`. [시스템 도구](system-tools.md)의 호출 등급과 같다.
- `instructions`: 그 도구가 처음 읽는 프로젝트 안내 파일. 모두 [AGENTS_CONTEXT.md](../../AGENTS_CONTEXT.md)를 가리키고 규칙을 중복하지 않는다.

## 새 직원 들이기

1. `config/agents.json`에 항목을 추가한다. 처음에는 `probation`, `max_tier: standard` 이하로 둔다.
2. 그 도구가 읽는 안내 파일을 만든다. 내용은 "AGENTS_CONTEXT.md를 먼저 읽고, 게시판 ID는 `<ID>:<세션>`"만 적는다. 예: Claude Code는 `CLAUDE.md`, Cursor는 `.cursor/rules/jarvis.mdc`(Cursor는 루트 `AGENTS.md`도 읽는다).
3. 그 도구의 MCP 설정에 자비스를 등록한다. 실행 파일·인자·작업공간 환경 변수는 Codex·Claude Code와 같다. Cursor는 `~/.cursor/mcp.json`.
4. 도구 자체의 권한 설정에서 자비스 도구를 직원 등급에 맞게 허용·금지한다. 자동 실행(headless) 모드는 허용 목록에 없는 MCP 호출을 거부한다. Cursor는 프로젝트 `.cursor/cli.json`에 `Mcp(jarvis:<도구>)` 형식으로 게시판 7개만 허용하고 민감 도구 8개를 금지했다.
5. 게시판에 첫 작업을 올려 실제 사용을 확인한다. Cursor 예: `agent -p --approve-mcps --trust "<지시>"`(파일 수정 허용 `--force`는 주지 않음).
6. 전체 검사(`pytest -q`)로 명부 형식과 안내 파일 존재를 확인한다.

## 한계 (다음 단계)

- 신원은 아직 자기 신고다. 누구든 `codex:main`을 쓸 수 있다. `status`와 `max_tier`는 기록일 뿐 서버가 직원별로 강제하지 않는다. 직원별 출입 토큰과 등급·예산 강제는 W4-2에서 한다.
- 협업 팀(`team.py`)의 실행 파일 탐색과 한도 정책(`workload_policy.py`)의 직원 목록은 아직 명부를 읽지 않는다. 공통 작업자 어댑터 단계에서 연결한다.
- 명부를 바꾸면 자비스 서버를 다시 연결해야 반영된다.
