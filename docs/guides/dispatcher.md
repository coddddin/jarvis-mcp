# W5 디스패처 (자동 순환 v1)

[문서 안내](../README.md) · [게시판·소통](board.md) · [직원 명부](agents-roster.md)

게시판에서 직원 앞으로 온 일이 있으면 그 직원을 자동 실행(헤드리스)으로 깨운다. 직원은 우편함을 읽고 일하고, 다음 사람에게 넘긴다. 사람은 방향과 머지 승인만 한다.

## 사용자 결정 (2026-10-21)

- 커밋은 의미 있는 단위가 끝났을 때만, 직원별 브랜치 `auto/<직원>`에.
- `main` 머지는 **사용자 승인 후에만**. 디스패처는 머지하지 않고 브랜치만 push한다.
- 24시간, 일이 있을 때만 깨운다. 직원당 하루 10회. 한 번 실행은 최대 30분.
- v1은 다음 할 일을 스스로 만들지 않는다.

## 동작

### 직원 환경변수 경계 (2026-10-22 검토 브랜치)

직원 subprocess에는 OS·CLI 기본 실행에 필요한 명시적 목록만 상속한다(`jarvis/core/process_env.py`). GitHub Inbox 토큰, GH_TOKEN, 모델 API 키, 임의 비밀 환경변수, 프록시 인증값, PYTHONPATH/NODE_OPTIONS와 호스트의 OpenCode 설정은 자동 전달하지 않는다. 작업공간은 실제 guard 경로로 고정하고 OpenCode 제한 설정은 신뢰된 어댑터가 다시 생성한다.

API 키 환경변수만으로 로그인한 CLI나 SSH 에이전트 환경에 의존하는 작업은 인증에 실패할 수 있다. 실패 시 비밀값 전체 상속이나 유료 호출로 우회하지 않는다. 실제 Windows의 저장된 구독 로그인·Git 인증 호환성은 운영자 현장 검증이 필요하다. HOME/USERPROFILE/CODEX_HOME은 기존 계정 구성을 찾도록 유지하므로 이 정책이 계정 파일·자격증명 관리자 접근을 차단하는 OS 샌드박스는 아니다. 직원별 인증/ACL은 별도다.

### 순환

1. `python -m jarvis.cli.dispatch tick`이 `config/dispatcher.json`과 직원 명부를 읽는다.
2. 직원마다 깨울지 판단한다. 깨우는 조건: 명부 상태 active/probation, 디스패처 설정 enabled, 실행 중 아님, 한도 소진 대기 아님, 오늘 실행 횟수 상한 미만, **그 직원(또는 `<직원>:auto`) 앞으로 온 메시지 중 그 직원의 어느 세션도 아직 읽지 않은 것이 있음**. 전체 공지(`*`)로는 깨우지 않는다.
3. 직원별 git worktree(`%LOCALAPPDATA%\JarvisWorktrees\<직원>`, 브랜치 `auto/<직원>`)에서 실행한다. 기존 브랜치를 재설정하지 않는다. 사용자가 이전 작업을 머지했으면 브랜치를 main으로 앞당긴다. 검사는 프로젝트의 `.venv` 파이썬을 절대 경로로 쓴다(worktree 안의 `.venv` 연결은 링크 거부 검사에 걸려 쓰지 않는다).
4. 직원별 가벼운 실행 명령(실측 기본 비용 약 21k 토큰, 일반 실행 약 34k):
   - Claude Code: `claude -p --setting-sources "" --strict-mcp-config`(자비스만) `--permission-mode acceptEdits`, 허용 도구는 파일 편집·git 상태/커밋·프로젝트 pytest·게시판 9개. 권한 우회 옵션은 쓰지 않는다.
   - Codex: `codex -a never exec --sandbox workspace-write -C <worktree> --add-dir <.git>`. 네트워크 push는 디스패처가 한다.
   - Cursor(수습): `agent -p --approve-mcps --trust`, `--force` 없음. 파일 수정·커밋 없이 검토·제안만. 프로젝트 `.cursor/cli.json`이 게시판 도구만 허용한다.
   - OpenCode(수습, 무료 모델): `opencode-cli run --standalone -m <무료 모델> --format json`, 설정은 `OPENCODE_CONFIG_CONTENT`(파일 수정·웹·셸·작업 폴더 밖 접근 금지, 게시판 도구만). 403 FreeTierError는 `eligibility`로 기록하고 대기. 자세히는 [직원 명부](agents-roster.md).
5. 끝나면 장부(`작업공간/.jarvis-dispatch/ledger.sqlite3`)에 상태·종료 코드·토큰을 기록한다. 로그는 `%LOCALAPPDATA%\JarvisDispatch\logs`.
6. 한도 오류(usage/rate limit, quota, 429 등)면 1시간 대기 후 다시 시도하고, 연속이면 2배씩 최대 6시간. 회복은 다음 실행 결과로 확인한다(시간으로 추정하지 않음).
7. 브랜치에 새 커밋이 있으면 `origin`에 push. 실패·시간 초과·한도는 `user:heejun`에게 게시판 메시지로 알린다.
8. 실행 중 `.jarvis-stop`이 생기면 실행을 종료한다. `config/dispatcher.json`의 `enabled: false`면 아무것도 하지 않는다.

## 자비스 상시 서비스 (W5 통합-3)

직원을 깨우는 주체는 자비스다. Claude·Codex·Cursor는 깨워지는 직원일 뿐 디스패처를 돌리지 않는다.

- `python -m jarvis.cli.dispatch serve`: 작업공간당 하나만 실행된다(잠금 `작업공간/.jarvis-dispatch/service.json`, 살아 있는 다른 서비스가 있으면 거부). 프로세스 감시(process_registry)에 `service`로 등록한다. 설정의 `interval_minutes`마다 tick을 돌리고, 5초마다 `.jarvis-stop`을 확인해 멈춘다. `enabled: false`면 켜져 있어도 깨우지 않는다. tick 실패는 기록만 하고 서비스는 계속 돈다.
- 단일 실행은 OS 파일 잠금(`작업공간/.jarvis-dispatch/service.lock`)으로 보장한다. 서비스가 비정상 종료되면 OS가 잠금을 푼다. 깨운 직원 프로세스는 kill-on-close Job Object에 묶여 서비스가 죽으면 함께 종료된다. 설정 파일을 읽거나 검증하다 실패하면 직전 정상 설정을 유지하고(처음부터 없으면 비활성), 프로세스 감시 등록 실패는 기록만 한다. 서비스 오류는 `%LOCALAPPDATA%\JarvisDispatch\logs\service.log`.
- 일시정지: `작업공간/.jarvis-dispatch/paused.json` `{"paused": true|false, "by", "at", "reason"}`. HUD 백엔드가 원자적으로 쓰고 서비스는 읽기만 한다. paused=true면 서비스는 켜진 채 새로 깨우지만 않는다(진행 중 실행은 그대로). 해제하면 다음 5초 확인에서 바로 tick한다. 파일이 없으면 동작, 깨졌거나 형식이 틀리면 보수적으로 일시정지로 본다. `dispatch status`에 pause 상태가 나온다. 전역 `.jarvis-stop`과 달리 자비스 다른 기능은 멈추지 않는다.
- 한도 판단: Codex는 깨우기 전에 자비스 `workload_policy`를 거친다. 사용량 표본(`작업공간/.jarvis-dispatch/usage/codex.json`, W3 수집기 몫)이 10분 이내로 신선하면 정책이 `paused`일 때만 `workload_paused:<상태>`로 건너뛴다(delegate는 조율 역할로 깨움). 표본이 없거나 오래됐으면 `no_usage_sample`/`stale_usage_sample`로 기록하고 장부의 한도 대기만 적용한다(수집기가 멈췄다고 영구 정지하지 않음).
- 사용자가 한 번 켠다: `powershell -ExecutionPolicy Bypass -File scripts\dispatcher\register-service.ps1 -StartNow`. 로그인할 때마다 자동으로 뜬다. 예약 작업은 PowerShell을 거치지 않고 프로젝트의 `pythonw.exe`를 직접 실행하므로 작업을 끝내면 서비스와 그 직원들이 함께 끝난다. 끄기는 `.jarvis-stop` 또는 `enabled: false`, 등록 해제는 `Unregister-ScheduledTask -TaskName 'JARVIS Dispatcher'`.

## 프로세스 감시 (W2, 1단계)

`dispatch watch`와 HUD 디스패처 패널의 "확인이 필요한 기록"은 디스패처 장부와 `service.json`만 본다. 아무것도 종료하거나 고치지 않는다.

- service_down: 설정이 켜져 있고 일시정지가 아닌데 서비스 프로세스가 없음. service_record_stale: `service.json`의 pid가 죽었거나 다른 프로그램에 재사용됨.
- owner_dead: 장부에 실행 중인데 pid가 죽었거나 재사용됨(생성 시각이 pid를 적은 시각(pid_at)보다 5초 넘게 늦음. pid_at이 없는 옛 기록은 시작 시각 + 2분). owner_unknown: pid 없이 2분 넘게 실행 중.
- 정리(2단계): 디스패처는 같은 기준으로 주인이 없거나 재사용된 실행을 lost로 닫는다. pid 없는 실행 기록은 `run_timeout_minutes`가 지나면 lost(note no_pid)로 닫아 그 직원이 다시 깨어날 수 있게 한다. pid는 있지만 생성 시각을 읽을 수 없는 실행(권한 거부)도 그 시간까지만 살아 있다고 보고, 지나면 lost(note owner_unverifiable)로 닫는다. 프로세스는 종료하지 않는다.
- 주의: `dispatch dry-run`은 직원을 깨우지 않지만, 상태 확인 과정에서 위 기준에 걸린 실행 기록은 lost로 닫는다(장부만 바뀜).
- overdue: 실행 중으로 `run_timeout_minutes` + 2분을 넘김.
- 범용 실행 감시는 활동 수집기 몫이다. 알림 전송과 정리는 2단계다.

## 사용량 감시 (W3, 1단계)

모델을 호출하지 않고 5시간·주간 사용량을 `작업공간/.jarvis-dispatch/usage/<직원>.json`에 모은다. Codex 표본은 기존 workload_policy가 판단에 쓰고, 10분보다 오래된 표본은 판단에서 무시한다. HUD 패널의 "마지막 사용량"과 하루 요약의 `사용량(마지막 관찰)` 줄은 마지막 값과 그 시각을 보여 주기만 한다(오래됨 표시).

- Codex: 디스패처 tick(dry-run 제외)이 `~/.codex/sessions` 아래 최근 rollout 파일 3개의 끝 256KB에서 `token_count` 이벤트의 `rate_limits`만 읽는다. 값의 형식은 Codex 공식 코드(protocol.rs, 0~100%, 초 단위 초기화 시각)이고, 파일에 저장된다는 것은 관찰한 사실이라 출처를 `codex_rollout_observed`로 적는다. 대화 원문이 있는 파일이라 두 표시가 모두 있는 줄만 해석하고, 링크·정션은 거부한다. Codex가 쉬면 갱신되지 않는다.
- Claude Code: 공식 statusline 입력의 `rate_limits.five_hour`·`seven_day`(Pro·Max, 세션 첫 응답 이후). `jarvis/cli/claude_statusline_usage.py`가 숫자만 저장하고 한 줄을 출력한다. 설치(사용자 Claude Code 설정의 `statusLine`)는 사용자 승인 후. 예시: `config/statusline.claude.example.json`, 실제 경로 출력: `--workspace <작업공간> --render-config`.
- 수집 실패는 로그만 남기고 이전 파일을 그대로 둔다.

## 알림 (W3, 1단계)

tick은 하루 요약을 보낸 뒤 사용자에게 알릴 사건을 골라 `user:heejun`에게 게시판 메시지 1통으로 보낸다. 대상은 lease가 끝난 claim, blocked 작업, 어댑터 점검 코드(`privacy_open` 포함), 받은 일이 있는데 점검 때문에 깨우지 않은 직원, 프로세스 감시의 `service_down`·`owner_dead`·`overdue`다. 같은 사건은 하루에 한 번만 보낸다(`.jarvis-dispatch/alerts.sqlite3`, 7일 보관). 전송에 성공한 것만 기록하므로 실패하면 다음 tick에 다시 보낸다. 문구에는 작업 제목(redact 후 60자), 직원 ID, 정해진 한국어 문장만 들어가고 노트·경로·명령은 넣지 않는다. 알림이 실패해도 tick은 계속된다. HUD 디스패처 패널은 오늘 보낸 알림을 읽기 전용으로 보여 준다. Windows 토스트 알림은 2단계(별도 승인)다.
## 직원별 하루 토큰 상한 (W5)

`config/dispatcher.json`의 직원 설정에 `daily_tokens`(1000 이상 정수)를 넣으면, 오늘(로컬 자정 이후) 시작한 실행의 토큰 합이 그 값 이상일 때 그 직원을 깨우지 않는다(사유 `token_cap`). 값이 없으면 상한도 없다. 이미 돌고 있는 실행은 끊지 않는다. **`daily_tokens`는 cache 포함 합계다**: input(cache 생성·읽기 포함) + output. Claude는 cache 읽기가 매우 커서 실제 비용보다 훨씬 빨리 찰 수 있으니 값을 넉넉히 잡는다. 장부는 실행마다 cache 몫을 `cache_tokens`로 따로 남기고, `DispatchLedger.tokens_today()`가 합계와 내역(input, output, cache, cache를 모르는 옛 실행 수)을 함께 돌려준다. 토큰을 읽지 못한 실행은 0으로 세며 `daily_runs`가 계속 막는다. Claude 5시간 사용률 규칙은 2단계다.

## 순환 닫기: 로컬 팀장과 하루 요약

- 로컬 팀장: 모든 직원에게 처리할 메시지도 실행 중인 일도 없을 때만 tick이 `lead_planner.plan_next`를 부른다. 팀장은 `docs/PLAN.md`의 미체크 항목 하나를 작업으로 만들고 담당 직원에게 메시지로 배정한다. 같은 tick에서 그 직원을 바로 깨운다. 판단은 로컬 Ollama(`lead_judge.ollama_judge`)가 하고, Ollama가 꺼져 있거나 판단 함수가 없으면 팀장만 쉬고 직원 깨우기는 계속한다. 하루 생성 상한은 `lead.daily_tasks`(기본 3). 민감 작업은 작업을 만들지 않고 사용자에게 승인 요청만 보낸다.
- 하루 요약: `digest_hour`(기본 21시) 이후 첫 tick에서 하루 한 번 `user:heejun`에게 게시판 메시지를 보낸다. 내용은 오늘 완료한 작업, 머지·검토 대기, 멈춘 담당, 직원별 깨운 횟수와 결과, 한도 대기, 일시정지 여부, 현재 어댑터 점검으로 깨우지 않는 직원과 사유(명령 조각 없이 분류만, 지금 기준이며 하루 이력은 아님)(최대 1000자).

## 명령

```powershell
.\.venv\Scripts\python.exe -m jarvis.cli.dispatch dry-run   # 판단만, 실행 없음
.\.venv\Scripts\python.exe -m jarvis.cli.dispatch tick      # 한 번 실행 (직원을 실제로 깨움)
.\.venv\Scripts\python.exe -m jarvis.cli.dispatch status    # 장부 요약: 직원별 실행 수·토큰·소진 상태
.\.venv\Scripts\python.exe -m jarvis.cli.dispatch watch     # 감시(읽기 전용): 꺼진 서비스, 주인 없는 실행 기록, 제한 시간 초과
.\.venv\Scripts\python.exe -m jarvis.cli.dispatch serve     # 상시 서비스 (보통은 예약 작업으로 실행)
```

## 머지 승인

직원은 끝나면 `board_release(status='review')`와 `user:heejun` 메시지로 브랜치와 검증 결과를 알린다. 사용자가 승인하면 Claude Code 또는 Codex 대화 세션이 해당 브랜치를 main에 머지·push한다.

## 상태 (2026-10-21)

- 판단 규칙·장부·명령 형태 검사 22개와 전체 642개 통과. dry-run에서 Cursor 1명이 깨울 대상으로 정확히 선택됨(Codex의 검토 요청 미처리).
- 실제 `tick`과 10분 주기 예약 등록은 **사용자 승인 대기**. Claude Code 자동 모드 안전 검사가 무인 에이전트 실행을 막았다. 사용자가 직접 실행하거나 허용 규칙을 추가해야 한다.

## 한계

- 신원은 자기 신고(W4-2 전). 깨운 세션은 `<직원>:auto` ID를 쓴다.
- 남은 한도는 직원 CLI가 알려주지 않아 우리가 정한 상한과 한도 오류로만 관리한다.
- 다음 할 일 자동 생성(팀장 역할)은 v2.

