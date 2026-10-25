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
   - Codex: `codex -a never exec --ignore-user-config --sandbox workspace-write -C <worktree> --add-dir <.git>`. 사용자 설정(플러그인·다른 MCP·notify)은 읽지 않고, Windows 샌드박스는 `elevated`로 고정한다. 셸만 남기고 화면 조작·브라우저·앱·플러그인·훅 등은 `--disable`로 끄며, MCP는 자비스 게시판 9개만(`-c`로 지정·자동 승인) 쓴다. 네트워크 push는 디스패처가 한다.
   - Cursor(수습): `agent -p --approve-mcps --trust`, `--force` 없음. 파일 수정·커밋 없이 검토·제안만. 프로젝트 `.cursor/cli.json`이 게시판 도구만 허용한다.
   - OpenCode(수습, 무료 모델): `opencode-cli run --standalone -m <무료 모델> --format json`, 설정은 `OPENCODE_CONFIG_CONTENT`(파일 수정·웹·셸·작업 폴더 밖 접근 금지, 게시판 도구만). 403 FreeTierError는 `eligibility`로 기록하고 대기. 자세히는 [직원 명부](agents-roster.md).
5. 끝나면 장부(`작업공간/.jarvis-dispatch/ledger.sqlite3`)에 상태·종료 코드·토큰을 기록한다. 로그는 `%LOCALAPPDATA%\JarvisDispatch\logs`.
6. 한도 오류(usage/rate limit, quota, 429 등)면 1시간 대기 후 다시 시도하고, 연속이면 2배씩 최대 6시간. 회복은 다음 실행 결과로 확인한다(시간으로 추정하지 않음).
7. 브랜치에 새 커밋이 있으면 `origin`에 push. 실패·시간 초과·한도는 `user:heejun`에게 게시판 메시지로 알린다. 디스패처의 git은 모두 빈 훅 폴더(`%LOCALAPPDATA%\JarvisDispatch\no-hooks`)와 `core.fsmonitor=false`로 실행한다.
   - 실행마다 따로 kill-on-close 작업 개체를 만든다. 실행이 끝나면 그 실행이 띄운 프로그램(백그라운드로 남은 것 포함)을 모두 끝낸 뒤 검사한다. 모두 끝내지 못하면 push를 건너뛰고 일시정지·경고한다.
   - 실행 전후로 `.git/config`, `.git/hooks`, 그 worktree의 연결 파일(`.git`, `commondir`, `gitdir`, `config.worktree`)을 비교해 바뀌었으면 push를 건너뛰고 디스패처를 일시정지한 뒤 `user:heejun`에게 경고한다. `.git/config`는 git이 읽은 항목으로 비교하고, 다른 세션이 늘 바꾸는 `branch.<이름>.*`(upstream)는 빼되 `mergeOptions`는 센다.
   - 마지막으로 확인한 상태를 `작업공간/.jarvis-dispatch/git-baseline.json`에 남기고, 다음 실행은 `ensure_worktree`(merge)보다 먼저 그와 비교한다. 실행 사이에 바뀌었으면 실행하지 않고(장부 메모 `git_changed`) 일시정지·경고한다. 처음 보는 직원은 그때 상태를 기준으로 삼는다. 기록 파일이 없는데 끝난 실행이 이미 있으면(지워졌거나 이 검사를 처음 적용함) 바뀐 것으로 본다.
   - 틱은 세 단계다. (1) 깨울 직원마다 차례로 비교·worktree 준비를 끝낸다. 그 사이 일시정지되면 아무 실행도 시작하지 않는다(장부 메모 `paused`). (2) 실행은 동시에 한다. (3) 모든 실행이 끝나고 각 작업 개체가 비워진 뒤, 다시 한 번 기록과 비교하고 나서야 push한다. 한 직원이 끝났을 때 아직 실행 중인 다른 직원이 `.git/config`를 바꿔도 push가 그 설정을 읽지 않게 하기 위해서다. 다르면 push를 모두 건너뛰고 일시정지·경고한다.
   - 직원들은 같은 틱에 동시에 실행된다. 경고할 때는 일시정지를 먼저 쓰고 새 상태를 나중에 기록하며, 둘 다 같은 잠금 안에서 한다. 비교를 통과한 실행도 일시정지 상태면 시작하지 않는다(장부 메모 `paused`). 그래서 다른 직원의 경고 직후 바뀐 설정으로 merge하는 일이 없다.
   - 경고 뒤 HUD에서 다시 시작하면 그때 상태가 새 기준이 된다. 사람이 원격을 추가하는 등 정상 변경도 한 번 경고된다.
   - 한계: 디스패처 git이 읽는 사용자 전역 설정(`~/.gitconfig`)은 비교하지 않는다. Claude 실행은 샌드박스가 아니어서(pytest가 conftest 등 저장소 코드를 실행) 전역 설정이나 작업 개체 밖 프로그램(예약 작업 등)을 만들 수 있다. 그래서 Claude 직원의 첫 실행들은 사람이 지켜본다.
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

**사용량 품질(J2)**: 장부 `runs.usage_quality`에 실행마다 사용량을 어떻게 읽었는지 남긴다. `measured`(입력·출력 둘 다 읽음), `partial`(한쪽만, 또는 cache만), `unknown`(사용량 보고는 있으나 읽을 수 없음), `missing`(끝났는데 사용량 보고가 아예 없음). 빈 값(NULL)은 기록 안 됨이다: 이 칸이 생기기 전 실행, 아직 도는 실행, 시작하지 못한 실행, lost·예외로 끝난 실행. 옛 실행은 숫자가 있어도 `measured`로 올리지 않는다. `measured`는 보고된 입력·출력 두 값을 읽었다는 뜻일 뿐 실행 전체를 관측했다는 뜻이 아니다(마지막 보고 이벤트를 쓰고 합산하지 않는다). Cursor의 cache 0은 보고하지 않았다는 뜻일 수 있다. `tokens_today()`는 `partial_runs`와 `unknown_runs`(unknown+missing)를 더 돌려주고, 장부 요약과 HUD 스냅숏은 직원별 `usage_quality_runs`(품질별 실행 수, NULL은 `unrecorded`)와 `known_input_tokens`·`known_output_tokens`(품질이 기록된 실행에서 보고된 값만의 합, 하나도 없으면 null) 및 `known_*_runs`, 품질 기록 전 옛 실행의 숫자 합 `unrecorded_input_tokens`·`unrecorded_output_tokens`(없으면 null)를 기존 숫자 옆에 더한다. 스냅숏의 최근 실행 `usage_quality`는 정해진 값이 아니면 null로 보낸다. input에 cache가 이미 들어 있으므로 cache를 다시 더하지 않는다. 기존 `total_*` 숫자와 상한 판단은 그대로다.

**HUD 표시(J2·J4)**: "직원별 누적"은 `known_*` 합을 쪽별 보고 범위(`보고 2/3회`, 분모는 품질이 기록된 실행 수)와 품질별 실행 수(측정·일부 보고·읽지 못함·보고 없음·기록 안 됨)와 함께 보여 준다. 보고한 실행이 하나도 없는 쪽은 0이 아니라 `— 모름`이고, 측정된 0은 0이다. 품질 기록 전 옛 실행의 숫자와 이 키가 없는 예전 서버의 `total_*`는 `정확도 알 수 없음`으로 표시하며 측정값으로 보이지 않는다. 최근 실행은 정해진 품질 값만 한국어로 보이고, null은 `기록 안 됨`, 그 밖의 값이나 키 없음은 `사용량 정확도 알 수 없음`이다. 측정은 보고된 이벤트 값을 읽었다는 뜻일 뿐 실행 전체를 관측했다는 뜻이 아니다. cache는 다시 더하지 않고, 계정 사용률("마지막 사용량")과 섞지 않는다.

**지금 일하는 직원**: 스냅숏의 `working`은 장부에서 아직 `running`인 실행(최대 20개)마다 `id`·`employee`·`model`·`started`와 관찰한 `state`를 보낸다. `working`은 실행이 띄운 프로그램이 살아 있음(생성 시각으로 번호 재사용 제외), `starting`은 시작 후 120초 안이라 아직 프로그램 번호가 없음, `ended`는 프로그램은 끝났지만 장부가 아직 결과를 기록하지 않음(성공·실패를 뜻하지 않음), `unknown`은 번호 없이 120초가 지났거나 프로그램을 확인할 수 없음이다. 읽기만 하고 장부를 고치지 않는다. 최근 실행에는 `model`과 `result`가 붙는다. `result`는 정해진 장부 메모(`executable_missing`·`models_backed_off`·`no_pid`·`owner_unverifiable`·`git_changed`·`paused`)만 코드로 보내고 로그 이름·예외 문구는 보내지 않는다(null). HUD는 이 목록과 최근 실행 5개(직원·모델·걸린 시간·결과 한 줄)를 보여 주고, 끝나지 않은 장부 행은 "실행 중"이 아니라 "안 끝남"으로 쓴다. `working` 키가 없는 예전 서버는 "알 수 없음"이다. 직원을 깨운 메시지는 장부에 지문만 있어 아직 보여 주지 않는다.

## 같은 입력이면 다시 깨우지 않음 (J2 H-02)

직원을 깨울 때 그 직원의 안 읽은 메시지 id 목록 지문을 장부 `runs.input_key`에 남긴다. 다음 tick에서 지문이 지난 실행과 같으면:

- 지난 실행이 `ok` 또는 `stopped`: 다시 깨우지 않는다(사유 `unchanged_input`). 실행은 끝났는데 메시지를 읽지 않았거나 사용자가 멈춘 경우다. 알림에 `같은 입력으로 다시 깨우지 않음: <직원>` 한 줄이 하루 한 번 간다.
- 지난 실행이 `error`·`timeout`·`lost`: 같은 입력에서 연속 실패한 횟수만큼 `next_backoff`(1시간부터 두 배, 최대 6시간) 동안 깨우지 않는다(사유 `retry_backoff`). 실패마다 기존 실행 알림이 이미 간다.
- `limit`·`eligibility`·`model_missing`은 기존 소진 장부(직원 단위, 모델별)가 처리한다. 이 대기는 직원·입력 단위이며 모델별 소진과 섞이지 않는다.

새 메시지가 오면 지문이 바뀌어 평소처럼 깨운다. 지문이 없는 예전 실행 기록은 막지 않는다. `unchanged_input`은 만료가 없으므로, JARVIS 리드 계획 조건(모든 직원이 할 일 없음)에서는 이 직원을 쉬는 직원으로 센다. 안 읽은 메시지 하나가 리드를 영원히 막지 않게 하기 위해서다. `retry_backoff`는 스스로 풀리므로 리드를 계속 기다리게 한다. 계획 담당(lead_planner)은 후보가 없으면 모델을 부르기 전에 멈추고, 하루 요약과 알림은 모델을 쓰지 않으므로 이 규칙은 직원 깨우기에만 적용한다.

## 모델 돌려쓰기 (OpenCode 무료 모델)

직원 설정에 `models`(1~8개, 우선순위 순서)를 넣으면 실행마다 쓸 모델을 고른다. `model`과 함께 쓰면 설정 오류로 거부한다. 실행 직전 백오프 중이 아닌 첫 모델을 골라 `-m`으로 넘기고 장부 `runs.model`에 남긴다. 결과가 `limit`·`model_missing`이면 **그 모델만** 백오프한다(1시간부터 두 배, 최대 6시간, 표 `model_exhaustion`). 다음 tick은 다음 모델을 쓴다. 성공하면 그 모델의 백오프를 지운다. `eligibility`(403 FreeTierError)는 모델이 아니라 실행 방식 문제라 직원 단위로 백오프한다(모든 모델을 다 돌아보는 낭비 방지). 모든 모델이 백오프 중이거나 직원 백오프 중이면 한도 대기(`exhausted`)이고, 둘 다 풀리는 시각까지 깨우지 않는다. 모델 하나의 백오프는 사용자에게 매번 알리지 않고(하루 요약에 표시), 마지막 남은 모델까지 막힐 때 한 번 알린다. 모델 ID는 `-`로 시작할 수 없다. `models`가 없는 직원은 예전처럼 직원 단위로 백오프한다.

`model_missing`은 OpenCode CLI 자체 문구(`ProviderModelNotFoundError`, `Model not found: <공급자>/<모델>`, opencode-cli v2.0.32 바이너리 문자열에서 확인)로 판정하되 실패한 실행(종료 코드 0이 아님)에만 적용한다. 성공한 실행의 출력은 이 저장소의 테스트·문서를 인용해 같은 문구를 담을 수 있기 때문이다. 한계: OpenCode가 모델 없음에도 종료 코드 0을 내는지는 실제 실행으로 확인하지 않았다(실행 금지 범위). 그 경우엔 백오프되지 않고 매번 같은 모델을 시도하므로, 운영에서 처음 보이면 확인한다. 무료 목록이 바뀌어 모델이 사라져도 돌려쓰기가 멈추지 않게 하기 위해서다. 하루 요약에는 `- opencode 모델: 지금 <모델>, 소진 <모델>(HH:MM까지)` 한 줄이 붙는다(설정에서 검증된 모델 ID와 시각만). 현재 설정은 opencode 무료 모델 7개다. 비전용 super-vl과 정체가 불분명한 beam-free는 넣지 않았다.

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

