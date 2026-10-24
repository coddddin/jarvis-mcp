# GitHub Inbox 로그인 자동 수신 (선택 설치)

이 브랜치는 `feat/github-inbox-mvp`를 기반으로 한다. PR #8을 먼저 검토·통합해야 한다.
기존 JARVIS Dispatcher 예약 작업은 수정하지 않는다. 이 기능은 **로그인 시 1회** 승인된 Issue를 게시판에 가져오는 것뿐이며 AI 직원 실행, 원격 PC 부팅, Issue 명령 실행은 하지 않는다.

## 안전한 설치 조건

- Windows 데스크톱에 직접 로그인해야 트리거된다. 단순 전원 켜짐만으로 실행되는 기능은 아니다.
- 프로젝트의 `.venv\Scripts\python.exe` 및 `JarvisWorkspace` 폴더가 있어야 한다.
- 사용자 환경 변수 `GITHUB_TOKEN`에 해당 저장소 Issues **읽기 전용** 토큰을 설정한다. 토큰을 저장소에 기록하지 않는다. 설정 후 로그아웃·로그인이 필요할 수 있다.
- 기존 작업과 브랜치가 깨끗한지 먼저 확인하고, PR #8 코드가 설치된 환경에서 수동으로 미리보기를 실행한다.
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

## 제한 및 복구

- 토큰 만료/네트워크 실패 시 해당 실행만 실패하며 기존 게시판 작업은 유지된다.
- 로그인 시 1회만 수신한다. 로그인 후에 등록한 Issue는 다음 로그인 또는 수동 실행 때 수신한다.
- GitHub API 요청은 승인 라벨이 있는 열린 Issue 최대 3페이지다. 마지막 페이지도 100개면 적용 전에 실패한다. 해당 경우 수동 범위 확장 설계가 필요하다.
- 기존 디스패처가 동시에 실행되어도 별도 보드 수신 작업만 생성하며 AI를 직접 깨우지 않는다.
- 사용자 승인 전 `main` 병합, 기존 예약 작업 수정, 자동 설치는 금지한다.

