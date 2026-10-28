# GitHub Issue → 로컬 게시판 우편함 MVP

현재는 **사용자가 수동 실행하는 안전한 수신기**다. PC가 꺼진 동안 Issue는 GitHub에 남고, PC를 켠 후 명령으로 가져온다. 로그인 자동 수신은 아래 선택 절차로 별도 등록할 수 있습니다. 이 수신기가 AI 실행·자동 분업을 시작하지는 않습니다.

## 안전 규칙
- 고정 저장소 `coddddin/jarvis-mcp`, 작성자 `coddddin`, 열린 Issue, `jarvis-approved` 라벨 모두 필요.
- GitHub Issue 제목과 링크만 보드에 등록. 본문은 복사하거나 명령으로 실행하지 않는다.
- 기존 `BoardStore` SQLite 트랜잭션에서 Issue→Task 매핑을 함께 저장하므로 반복 실행해도 중복 생성되지 않는다.
- 수신된 작업은 `open` 상태, 경로 claim 없음, 직원 깨우기 없음. 라벨은 **작업 수신 허용**일 뿐 코드 실행 승인이 아니다.
- 기본값은 미리보기. `--apply`를 넣어야 로컬 게시판에 쓴다.
- **보안 한계:** Issue 작성자 확인은 GitHub API의 작성자 값에 의존하고, 라벨을 누가 부착했는지 검증하지 않는다. 따라서 저장소의 라벨 수정 권한을 신뢰할 수 있는 사용자로 제한해야 한다. `board`의 에이전트 ID 인증은 별도 W4-2 과제다.
- 별도 클라우드 AI API 호출이 없고, 신규 상시 서버도 없다.

## 준비
1. GitHub의 저장소 Issue 기능을 사용한다.
2. `jarvis-approved` 라벨을 만들고, 가져올 Issue에 직접 부착한다. 민감정보를 Issue에 쓰지 않는다.
3. 로컬 PC에서 GitHub 토큰을 **사용자 환경 변수** `GITHUB_TOKEN`에 넣는다. 최소한 해당 저장소 Issues 읽기 권한만 부여한다. 토큰을 채팅·명령 인자·저장소에 붙여넣지 않는다.
4. 프로젝트의 기존 Python 가상환경을 사용한다. 작업공간 경로는 실제 `JarvisWorkspace` 절대 경로를 입력한다.

```powershell
# 미리보기(읽기 전용)
.\.venv\Scripts\python.exe -m jarvis.cli.github_inbox --workspace "C:\path\to\JarvisWorkspace"

# 승인된 Issue만 로컬 게시판에 등록
.\.venv\Scripts\python.exe -m jarvis.cli.github_inbox --workspace "C:\path\to\JarvisWorkspace" --apply

# 테스트
.\.venv\Scripts\python.exe -m pytest -q tests/test_github_inbox.py
```

## 운영·제약
- 승인 라벨이 있는 열린 Issue만 최대 3페이지 조회한다. 마지막 페이지까지 100개가 차면 누락을 성공으로 표시하지 않고 적용 전에 실패한다(정확히 300개인 경우도 보수적으로 실패).
- GitHub API 오류·토큰 부재 시 실패하고, 비밀 토큰을 출력하지 않는다.
- Issue 내용이 바뀌어도 이미 등록된 작업을 덮어쓰지 않는다. 갱신/취소 동기화는 후속 설계.
- 로컬 PC가 꺼져 있으면 수신은 실행되지 않는다. GitHub에 Issue만 보관된다.
- 로그인 시 선택 수신과 전원 부팅은 다릅니다. 이벤트 기반 Actions 분류·양방향 결과 회신은 별도 과제입니다.
- `main` 병합은 사용자 승인 후에만 진행한다.

## 검토 브랜치의 수정 (2026-10-22)

- 직접 수신 함수에서도 고정 저장소·작성자·승인 라벨을 재검사한다. 비정상 작성자/번호/제목은 거부한다.
- GitHub 제목은 최대 256자 입력을 검증한 뒤 번호 접두사를 포함해 보드 200자 이내로 줄인다. 자격증명 검사는 자르기 전 전체 제목에 적용한다.
- 인증 헤더가 다른 목적지로 전달되지 않도록 HTTP 리디렉션을 거부한다. 네트워크·API 실패는 비밀값을 출력하지 않고 실패 코드로 반환한다.
- 상대 workspace 경로를 정규화하고 중단 마커가 있으면 API 요청 전 실패한다. preview는 보드 DB를 만들지 않는다.
- 동시 수신은 SQLite `BEGIN IMMEDIATE`와 고유 매핑으로 한 작업·이벤트만 생성한다. 이후 저장 실패 시 해당 Issue 트랜잭션은 롤백한다. 여러 Issue 배치는 Issue별 커밋이므로 중간 실패 후 재실행하면 이미 받은 항목은 건너뛴다.
- 디스패처는 직원별 메시지를 기준으로 깨운다. 수신은 담당자·메시지를 만들지 않으므로 자동 실행하지 않는다. 로컬 리드는 open 작업 때문에 idle이 아니라고 판단한다. 사용자가 별도 배정해야 직원 실행 대상으로 이어진다.


## 로그인 시 자동 수신 (선택 설치)

기존 JARVIS Dispatcher 예약 작업은 수정하지 않는다. 이 기능은 **로그인 시 1회** 승인된 Issue를 게시판에 가져오는 것뿐이며 AI 직원 실행, 원격 PC 부팅, Issue 명령 실행은 하지 않는다.

### 등록 조건

- Windows 데스크톱에 직접 로그인해야 트리거된다. 단순 전원 켜짐만으로 실행되는 기능은 아니다.
- 프로젝트의 `.venv\Scripts\python.exe` 및 `JarvisWorkspace` 폴더가 있어야 한다.
- 사용자 환경 변수 `GITHUB_TOKEN`에 해당 저장소 Issues **읽기 전용** 토큰을 설정한다. 토큰을 저장소에 기록하지 않는다. 설정 후 로그아웃·로그인이 필요할 수 있다.
- 기존 작업과 브랜치가 깨끗한지 먼저 확인하고, 수신 CLI가 설치된 환경에서 수동으로 미리보기를 실행한다.
- 실제 등록은 **사용자가 로컬 PC에서 명령을 실행할 때만** 이루어진다.
- 같은 이름의 Inbox 작업이 이미 있으면 등록을 거부한다. 덮어쓰지 않으며 기존 Dispatcher 작업도 수정하지 않는다. 현재 Windows 사용자 계정의 대화형·일반 권한으로 등록한다.

```powershell
# 먼저 수동으로 확인
.\.venv\Scripts\python.exe -m jarvis.cli.github_inbox --workspace ".\JarvisWorkspace"
# 사용자가 설치하기로 결정한 경우에만
powershell -ExecutionPolicy Bypass -File scripts\github-inbox\register-logon.ps1
# 즉시 1회 동기화도 원하면 -StartNow 추가
```

등록 해제: `Unregister-ScheduledTask -TaskName 'JARVIS GitHub Inbox' -Confirm:$false`

### 실패와 복구

- 토큰 만료/네트워크 실패 시 해당 실행만 실패하며 기존 게시판 작업은 유지된다.
- 로그인 시 1회만 수신한다. 로그인 후에 등록한 Issue는 다음 로그인 또는 수동 실행 때 수신한다.
- GitHub API 요청은 승인 라벨이 있는 열린 Issue 최대 3페이지다. 마지막 페이지도 100개면 적용 전에 실패한다. 해당 경우 수동 범위 확장 설계가 필요하다.
- 기존 디스패처가 동시에 실행되어도 별도 보드 수신 작업만 생성하며 AI를 직접 깨우지 않는다.
- 사용자 승인 전 `main` 병합, 기존 예약 작업 수정, 자동 설치는 금지한다.

