# 휴대폰 Telegram 상태 조회 (PA4-T1)

휴대폰에서 "아까 맡긴 일 어떻게 됐어?"를 **조회만** 하는 기능이다. 작업 중지(T2), 접수(T3), 승인(T4)은 아직 없다. 사용자가 터미널에서 직접 켜는 프로그램이며 서비스나 예약 작업으로 등록하지 않는다.

## 안전 규칙
- PC가 `api.telegram.org`에 먼저 접속해 새 메시지를 가져온다(getUpdates 롱폴링). 웹훅을 쓰지 않고 외부 포트를 열지 않는다. 웹훅이 설정돼 있으면 시작을 거부하며, 웹훅을 지우지도 않는다.
- 답하는 대상은 페어링한 1:1 대화 하나뿐이다. user id와 chat id가 모두 맞아야 하고 봇 계정은 제외한다. 그룹, 채널, 다른 사람, 수정된 메시지에는 답하지 않는다.
- 명령은 `/status`와 `/task <ID 앞 6자리 이상>` 두 개다. 자유 문장은 해석하지 않으며 모델도 호출하지 않는다. 64자를 넘는 입력에는 도움말만 보낸다.
- 보여 주는 정보는 작업 ID, 제목(가림 처리, 60자), 상태, 담당 직원, 마지막 갱신 시각과 경과 시간, 멈춤 의심 여부, 디스패처 상태다. 메모, 상세, 경로, 명령, 로그는 보내지 않는다.
- v1의 "맡긴 일"은 게시판에서 `creator=user:heejun`인 작업이다. GitHub 우편함 작업은 나중에 설정 항목으로 추가한다.
- 게시판에는 쓰지 않는다. 예외는 멈춘 이유를 알리는 고정 문구 알림 한 줄(`local:telegram` -> `user:heejun`, 하루 한 번)이다.
- 봇 토큰은 `%LOCALAPPDATA%\JarvisTelegram\bot-token.bin`에 Windows DPAPI(현재 사용자)로 암호화해 둔다. 프로젝트, 작업공간, 환경 변수, 로그, 게시판에는 남기지 않는다. 환경의 프록시 설정과 리다이렉트는 따르지 않는다(토큰이 요청 주소에 들어가기 때문).

## 사용자가 할 일
1. 휴대폰 Telegram에서 `@BotFather`에게 `/newbot`을 보내 봇을 만들고 토큰을 받는다. `/setjoingroups`에서 Disable을 골라 그룹 초대를 막는다.
2. PC의 본인 PowerShell 창에서 토큰을 저장한다. 토큰은 화면에 표시되지 않는다.
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\telegram\setup-secrets.ps1
   ```
3. 설정을 확인한다. 네트워크에 접속하지 않으며 토큰도 출력하지 않는다.
   ```powershell
   .\.venv\Scripts\python.exe -m jarvis.cli.telegram_poll check
   ```
4. 휴대폰을 페어링한다. 화면에 뜬 6자리 숫자를 5분 안에 휴대폰에서 봇에게 보내고, PC에 표시된 id를 확인한 뒤 `y`를 누른다. 틀린 메시지가 10개 오면 페어링을 중단한다.
   ```powershell
   .\.venv\Scripts\python.exe -m jarvis.cli.telegram_poll pair
   ```
5. 조회를 시작한다. 창을 닫으면 멈춘다. `JarvisWorkspace\.jarvis-telegram\config.json`의 `enabled`를 false로 바꾸거나 작업공간 중단 표시(`.jarvis-stop`)를 만들어도 멈춘다.
   ```powershell
   .\.venv\Scripts\python.exe -m jarvis.cli.telegram_poll poll
   ```

## 상태를 읽는 법
- **1분 안에 답이 없으면** PC가 꺼져 있거나 조회 프로그램이 멈춘 상태다. 봇은 PC에서 돌아가므로 꺼진 동안에는 답할 수 없다.
- PC가 다시 켜지면, 10분 넘게 지난 질문들은 하나로 묶어 한 번만 답한다. 예: "PC가 꺼져 있었거나 조회가 멈춘 동안 받은 질문 3건 (가장 오래된 것 ...)". Telegram 서버는 받지 않은 메시지를 최대 24시간 보관하므로, 그보다 오래 꺼져 있었으면 그 질문은 오지 않는다.
- 답장 첫 줄에는 관측 시각과 디스패처 상태(실행 중, 꺼짐, 등록 안 됨, 일시정지, 알 수 없음)가 있다. 작업마다 마지막 갱신 시각이 붙는다. 담당 lease가 끝난 진행 중 작업에는 "멈춤 의심"이 붙는다. 이것으로 기록이 오래된 것과 PC가 꺼진 것을 구분한다.
- 게시판을 읽지 못하면 "상태 확인 불가"라고 답하고, "작업 없음"으로 바꾸지 않는다.

## 중복과 실패 처리
- 받은 update_id마다 `JarvisWorkspace\.jarvis-telegram\state.sqlite3`에 처리 결과를 기록한다. 같은 update가 다시 오거나 프로그램을 다시 시작해도 한 번만 답한다.
- 답장은 같은 트랜잭션에서 보낼 목록에 넣은 뒤에 offset을 올린다. 그래서 중간에 꺼져도 답장이 사라지지 않는다. 다만 전송 직후에 꺼지면 같은 조회 답장이 한 번 더 갈 수 있다.
- 빈도 제한: 받은 묶음마다 1건, 직전 답장과 5초 간격, 시간당 30건까지 답한다. 넘는 질문에는 답하지 않는다.
- 네트워크 오류와 5xx는 5초부터 두 배씩 최대 5분까지 늘려 다시 시도한다. 답장은 최대 5회, 10분 안에서만 다시 보내고, 그 뒤에는 포기한다. 429는 `retry_after`만큼 기다린다. 400은 그 답장을 버린다.
- 401/404(토큰 거부)와 409(다른 조회 프로그램 또는 웹훅과 충돌)이면 멈추고 게시판 알림을 남긴다. 403(사용자가 봇을 차단)이면 남은 답장을 버리고 알린다. 같은 PC에서 두 번 켜면 잠금 파일 때문에 두 번째 프로그램은 시작하지 않는다.

## 검사
```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_telegram.py
```
가짜 transport만 쓰며 실제 Telegram에는 접속하지 않는다. DPAPI 왕복 검사는 가짜 토큰으로 Windows에서만 실행한다.
