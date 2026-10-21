# AI 직원 명부와 온보딩

[문서 안내](../README.md) · [현재 계획](../PLAN.md)

자비스와 함께 일하는 AI 도구는 `config/agents.json` 한 곳에 등록한다. 게시판과 무료 배정은 이 명부를 읽으므로, 새 직원을 들일 때 코드를 고치지 않는다.

## 현재 명부 (2026-10-21)

| ID | 이름 | 상태 | 최대 등급 | 비고 |
|---|---|---|---|---|
| `codex` | OpenAI Codex | active | sensitive | 주 개발 |
| `claude-code` | Claude Code | active | sensitive | 검토·개발 |
| `cursor` | Cursor | probation | standard | 2026-10-21 합류. CLI `agent` 2026.10.19 설치·로그인, 게시판 첫 작업 완료 |
| `opencode` | OpenCode | probation | light | 2026-10-21 합류. 무료 모델, 게시판 도구만, 개인정보 금지 |
| `gemini` | Gemini CLI | candidate | light | 이 PC에서 미발견 |

## 항목

- `kind`: `cli`, `api`, `mcp-client`, `local-model`
- `command`: 실행 파일 이름만. 셸 명령줄은 거부한다.
- `capabilities`: `research`, `coding`, `review`, `summary`
- `cost`: `free`, `subscription`, `paid`, `unknown`
- `status`: `active`(정식), `probation`(수습: 제안·검토만), `candidate`(후보: 아직 일하지 않음), `retired`(퇴사: 게시판·배정 거부)
- `max_tier`: 허용 의도 등급 `light`/`standard`/`sensitive`. [시스템 도구](system-tools.md)의 호출 등급과 같다.
- `instructions`: 그 도구가 처음 읽는 프로젝트 안내 파일. 모두 [AGENTS_CONTEXT.md](../../AGENTS_CONTEXT.md)를 가리키고 규칙을 중복하지 않는다.

## 협업 상황판

자비스 HUD의 **직원 협업 상황판**을 열면 직원별 담당 작업, 최근 메시지와 인수인계를 볼 수 있다. HUD 연결 인증을 그대로 사용하며, 펼쳐 둔 화면은 5초마다 갱신한다. 화면을 접거나 브라우저가 숨겨지면 조회를 멈춘다.

- 게시판의 담당·완료·검증 문구는 **자기 보고**다. 담당 기록이 있다는 이유로 모델이 실행 중이거나 온라인이라고 표시하지 않는다. 실제 프로세스 관찰은 별도 패널에서 확인한다.
- 메시지의 **우편함 조회**는 해당 세션이 우편함에서 조회했다는 기록이다. 이해·동의·착수·검증 완료를 뜻하지 않는다. 상황판 조회 자체는 메시지를 읽음 처리하지 않는다.
- 담당 기한이 지나면 상태 확인이 필요한 기록으로 표시한다. 자동 재배정·재실행·종료는 하지 않는다.
- 작업은 최대 50개(담당 기록 우선), 최근 메시지·노트는 합쳐 최대 40개를 조회한다. 본문은 비밀정보를 가린 뒤 최대 600자로 표시한다. 전체 기록이나 모델의 사고 과정·토큰 스트리밍 화면은 아니다.
- 조회가 실패하면 기존 내용과 마지막 조회 시각을 남기고 최신 여부를 확인할 수 없다고 알린다. 실행별 생존 신호의 구현 범위는 아래 등록 실행 관찰을 참고한다.

## 등록 실행 관찰 (운영 반영 승인 대기)

`codex/activity-collector`의 수집기와 HUD 담당 브랜치의 화면을 결합하는 범위다. 기존 게시판 자기 보고와 실행 관찰은 별도로 표시한다. 구현 브랜치가 있다고 운영 화면에 반영됐다고 간주하지 않는다.

- 신뢰받는 로컬 실행기만 자기 프로세스 또는 직접 띄운 자식 프로세스를 등록한다. 등록 API는 HTTP로 공개하지 않는다. 실행별 토큰은 한 번 반환하고 저장소에는 해시만 보관한다. HTTP 보고에는 해당 실행의 토큰이 필요하며 화면 조회용 인증 토큰과 다르다.
- 수집기는 서버에서 하나의 `ActivityStore` 인스턴스를 재사용한다. 실행별 순번으로 중복을 막고, 같은 담당 슬롯의 새 실행이 이전 실행을 무효화한다. 병렬 작업은 서로 다른 슬롯을 사용한다. 이 장치는 직원 신원 인증이나 OS 격리를 대신하지 않는다.
- 완료·실패 보고가 늦게 와도 생존 신호로 덮지 않는다. 늦은 상태 전이는 영속 기록에 남는다. 이력 조회의 `after`는 작업자 순번이 아니라 수집기의 `cursor`이므로 순서가 뒤집혀 도착한 기록도 다시 조회할 수 있다. 누락은 표시만 하며 작업을 재실행하지 않는다.
- 생존 신호는 수집기가 받은 시각으로 판단한다. 수집기를 다시 만들면 이전 단조시계를 비교하지 않고 상태를 미확인으로 둔다. 프로세스는 PID와 생성 시각을 함께 대조하며, PID 재사용은 신원 불일치로 표시한다.
- `self_reported`는 작업자 보고, `observed`는 수신·프로세스 직접 관찰이다. 제안을 받거나 프로세스가 끝났다는 이유로 검증 통과를 만들지 않는다. 현재 검증 어댑터는 연결하지 않아 `verified`는 비어 있다.
- 첫 자동 연결 범위는 HUD가 직접 띄우는 외부 협업 작업자다. 기존 외부 호출 상한 180초 안에서 10초마다 모델 없이 생존 신호를 남긴다. 수집기가 주입되지 않은 실행은 종전대로 동작하고, 관찰 실패는 작업 재시도를 만들지 않는다. 별도로 연 데스크톱 Codex·Claude·OpenCode 세션을 자동 추적하는 기능은 아니다.
- 저장 상한은 실행 200개, 실행별 이벤트 512개, DB 16MiB다. 자동 삭제 없이 상한 도달을 명시적으로 거부한다. 상시 HUD 프로세스나 장기 작업의 무기한 생존 신호는 첫 연결 범위에서 제외한다. 자동 재배정·재실행·종료도 하지 않는다.

## 새 직원 들이기

1. `config/agents.json`에 항목을 추가한다. 처음에는 `probation`, `max_tier: standard` 이하로 둔다.
2. 그 도구가 읽는 안내 파일을 만든다. 내용은 "AGENTS_CONTEXT.md를 먼저 읽고, 게시판 ID는 `<ID>:<세션>`"만 적는다. 예: Claude Code는 `CLAUDE.md`, Cursor는 `.cursor/rules/jarvis.mdc`(Cursor는 루트 `AGENTS.md`도 읽는다).
3. 그 도구의 MCP 설정에 자비스를 등록한다. 실행 파일·인자·작업공간 환경 변수는 Codex·Claude Code와 같다. Cursor는 `~/.cursor/mcp.json`.
4. 도구 자체의 권한 설정에서 자비스 도구를 직원 등급에 맞게 허용·금지한다. 자동 실행(headless) 모드는 허용 목록에 없는 MCP 호출을 거부한다. Cursor는 프로젝트 `.cursor/cli.json`에 `Mcp(jarvis:<도구>)` 형식으로 게시판 7개만 허용하고 민감 도구 8개를 금지했다.
5. 게시판에 첫 작업을 올려 실제 사용을 확인한다. Cursor 예: `agent -p --approve-mcps --trust "<지시>"`(파일 수정 허용 `--force`는 주지 않음).
6. 전체 검사(`pytest -q`)로 명부 형식과 안내 파일 존재를 확인한다.

## OpenCode (무료 모델 직원)

- 실행 파일: OpenCode Desktop에 들어 있는 `%LOCALAPPDATA%\Programs\@opencodedesktop\resources\opencode-cli.exe`(v2.0.32). 로그인 없이 `opencode/*-free` 모델 9개.
- 디스패처 실행: `opencode-cli run --standalone -m opencode/glm-5.3-flash-free --format json "<지시>"`, 설정은 환경 변수 `OPENCODE_CONFIG_CONTENT`로 넣는다(파일 수정·웹 접속·셸·작업 폴더 밖 접근·하위 에이전트 금지, `git status/diff/log`만 허용, 자비스 MCP 연결).
- `--standalone`이 필요하다. 없으면 OpenCode Desktop의 백그라운드 서버에 붙어 넣은 설정(MCP)이 적용되지 않는다. 입력은 반드시 막아서(stdin 없음) 실행한다. 터미널 입력이 붙어 있으면 자체 서버 기동 단계에서 멈춘다.
- 자비스 도구는 `tools: {"jarvis_*": false, "jarvis_board_*": true}`로 게시판만 연다. 2026-10-21 실측: 밑줄 패턴은 다른 자비스 도구(시스템 상태 등)를 막고, 점 패턴(`jarvis.*`)은 막지 못했다.
- 403 `FreeTierError`: 2026-10-20 실패는 직접 만든 제한 에이전트(`--agent jarvis-proposal`)로 실행한 것이 원인으로 보인다(공개 이슈와 같은 양상). 기본 에이전트에 권한만 제한하면 2026-10-21 시험 7가지 모두 정상 응답했다. 실행 중 403이 나오면 장부에 `eligibility`로 기록하고 한도와 같이 1시간부터 대기한다.
- 무료 모델은 입력이 학습에 쓰일 수 있다는 지적이 있어 개인정보(메일·일정·화면·개인 문서)는 맡기지 않는다. 지시문과 도구 제한 양쪽으로 막는다.
- 권한 기본값(opencode.ai/docs/permissions): 대부분 allow, `external_directory`는 ask, `read`는 allow(.env만 거부). `--auto` 없는 `opencode run`에서 ask가 어떻게 처리되는지는 문서에 없다. 그래서 `external_directory`·`websearch`·`webfetch`·`task`(하위 에이전트)를 명시적으로 deny한다. 작업 폴더 안 읽기는 검토·제안에 필요해 허용한다. 셸 규칙은 문서의 새 이름 `shell`과 v2.0.32 스키마의 `bash`에 같이 넣는다.
- 경계 점검: `worker_adapters.privacy_markers`가 위 deny와 게시판 전용 도구가 풀린 것을 찾고, 디스패처는 그 직원을 수습·정식 상관없이 깨우지 않는다(사유 `privacy_open`, 하루 요약 '외부 읽기·웹 차단 안 됨'). 실제 실행으로 deny를 확인하는 단계는 사용자 승인 후 진행한다(2026-10-22 기준 미검증).
- 사용량: OpenCode JSON 출력에 토큰 수가 항상 담기지 않아 장부에는 실행 횟수만 남을 수 있다.

## 업무 평가 (W4-5 1단계)

`python -m jarvis.cli.evaluation [--days N]`은 게시판을 읽기 전용으로 열어 직원별(세션 합침) 작업 수, 상태별 개수, 다른 에이전트의 finding 수, finding 뒤 재인계(재작업), 완료 작업의 생성부터 마지막 인계까지 시간을 JSON으로 낸다. 게시판에 상태 이력이 없어 값은 근사치이며 출력에 `approximate: true`와 정의 문구가 들어간다. 정확한 재작업·처리 시간은 상태 변경 이벤트 기록(board c26dd03e) 뒤에 바꾸고, 배정 반영은 2단계에서 한다.

## 한계 (다음 단계)

- 신원은 아직 자기 신고다. 누구든 `codex:main`을 쓸 수 있다. `status`와 `max_tier`는 기록일 뿐 서버가 직원별로 강제하지 않는다. 직원별 출입 토큰과 등급·예산 강제는 W4-2에서 한다.
- 협업 팀(`team.py`)의 실행 파일 탐색과 한도 정책(`workload_policy.py`)의 직원 목록은 아직 명부를 읽지 않는다. 공통 작업자 어댑터 단계에서 연결한다.
- 명부를 바꾸면 자비스 서버를 다시 연결해야 반영된다.
