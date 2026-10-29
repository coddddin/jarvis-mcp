# MCP 서버 연결

[문서 안내](../README.md) · [웹 정보](web-information.md) · [채팅](chat.md)

자비스가 다른 MCP 서버(Playwright 브라우저, fetch 등)의 도구를 쓰기 위한 클라이언트 층입니다. 코드는 `jarvis/features/mcp_client/service.py`와 격리 작업자 `jarvis/workers/mcp_session.py`입니다. 이 단계(M1)는 백엔드만 있고, 화면(서버 목록·승인)과 에이전트 작업 루프는 다음 단계입니다.

## 설정과 승인

- 설정 파일: `JarvisWorkspace/.jarvis-settings/mcp_servers.json`. **HUD 세션만 씁니다**(모델 선택과 같은 규칙). 모델에게 서버를 추가하거나 도구를 승인·변경하는 도구는 없습니다.
- 서버 항목: `id`, `label`, 실행 파일 **절대 경로** `command`, `args`, 환경 변수 **이름만** `env_names`(값은 저장하지 않음), `state`(`pending`/`approved`), 고정된 도구 목록 `tools`.
- 새 서버는 `pending`이고 도구가 하나도 없습니다. `discover`로 서버가 알려 준 도구와 해시를 보고, 사용자가 고른 도구만 `approve`로 고정합니다. 고르지 않은 도구는 모델에게 보이지 않습니다.
- 도구 고정(`ToolPin`): `family`(`public_web` 또는 `local`), `effect`(`read`/`auto`/`confirm`/`deny`), 시간 한도(최대 120초), 글자 한도, 모델에게 보일 **사용자 승인 설명**, 그리고 서버가 알려 준 이름·설명·입력 스키마의 sha256.
- 서버가 도구 설명이나 스키마를 바꾸면 해시가 달라져 그 도구는 "변경됨"으로 빠지고, 사용자가 다시 승인할 때까지 쓰이지 않습니다. 세션은 설정 파일을 고치지 않습니다.
- 항상 거부: `browser_run_code_unsafe`, `browser_evaluate`, `browser_file_upload`, `browser_install`.
- 금지 플래그: `--allow-unrestricted-file-access`, `--ignore-robots-txt`, `--allow-private-ips`. 서버 옵션을 환경 변수로 넘기는 `PLAYWRIGHT_MCP_*` 이름도 거부합니다.
- Playwright MCP: 실행 파일이나 인자에 `playwright`가 들어가면(npm bin `playwright-mcp.cmd` 포함) Playwright로 보고, 시작할 때 서버가 스스로 Playwright라고 알리는데 그렇게 등록되지 않았으면 시작하지 않습니다. `--no-webmcp --isolated --headless`가 반드시 있어야 하고(웹 페이지가 등록한 도구가 모델에 노출되지 않게), 허용 목록의 플래그만 쓸 수 있습니다: `--browser`, `--output-dir`, `--output-max-size`, `--image-responses`, `--block-service-workers`, `--timeout-action`, `--timeout-navigation`, `--timeout-settle`, `--user-agent`, `--viewport-size`, `--snapshot-mode`, `--proxy-server`, `--proxy-bypass`, `--allowed-origins`, `--blocked-origins`, `--idle-timeout`. 그래서 사용자의 실제 브라우저 연결(`--extension`, `--cdp-endpoint`), 프로필·쿠키·스크립트(`--user-data-dir`, `--storage-state`, `--init-script`), 설정 파일(`--config`), 네트워크 대기(`--host`, `--port`)는 막힙니다.

## 작업별 격리 세션

- 작업 하나에 작업자 프로세스 하나(`McpSession`). 승인된 서버를 stdio로 띄우고(MCP SDK가 Windows 작업 개체로 묶음) 서버마다 `JarvisWorkspace/.jarvis-mcp/<id>/` 폴더를 작업 폴더로 씁니다.
- 환경 변수는 기본 시스템 항목과 `env_names`에 적힌 이름만 넘깁니다.
- 한도(기본값, 바꿀 수 있음): 시작 60초, 세션 600초, 도구별 시간 한도 + 5초 여유. 도구가 시간 안에 답하지 않거나 자비스 중단, 세션 한도에 걸리면 작업자와 서버를 모두 종료합니다.
- `effect=confirm` 도구는 호출마다 사용자 확인이 있어야 실행됩니다(`ConfirmRequired`). `deny`는 제공하지 않습니다.
- 결과는 신뢰할 수 없는 데이터로 기록합니다: `name`=`mcp:<server>/<tool>`, `family`, `server`, 가린 텍스트 최대 6000자, 조회 시각.

## 외부 모델 전송

`family=public_web`으로 승인한 도구의 결과만 외부 모델(예: Claude 구독)에 갈 수 있습니다. `local`이거나 알 수 없는 도구는 로컬 전용입니다(`jarvis.agents.brain.family_of`). 내장 도구의 분류는 덮어쓸 수 없고, 외부 모델에게 주는 자비스 MCP 도구 허용 목록(`EXTERNAL_TOOLS`)도 바뀌지 않습니다.

## 설치 참고(검증된 버전, 2026-10-29)

- Playwright MCP `@playwright/mcp@0.0.83`: 자비스 전용 폴더에 `npm install` 후 `node <폴더>/node_modules/@playwright/mcp/cli.js --headless --isolated --no-webmcp --output-dir <격리 폴더>`. 실행 때 `npx`로 내려받지 않습니다.
- fetch `mcp-server-fetch==2026.10.10`: 이 PC에는 uvx가 없으므로 전용 venv에 설치하고 `<venv>\Scripts\python.exe -m mcp_server_fetch`로 실행합니다.
