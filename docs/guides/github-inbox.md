# GitHub Issue → 로컬 게시판 우편함 MVP

현재는 **사용자가 수동 실행하는 안전한 수신기**다. PC가 꺼진 동안 Issue는 GitHub에 남고, PC를 켠 후 명령으로 가져온다. 자동 시작·GitHub Actions·AI 실행·자동 분업은 **아직 연결하지 않았다**.

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
- 첫 실행 시 최대 300개의 열린 Issue를 조회한다. 더 많으면 페이지 확장 필요.
- GitHub API 오류·토큰 부재 시 실패하고, 비밀 토큰을 출력하지 않는다.
- Issue 내용이 바뀌어도 이미 등록된 작업을 덮어쓰지 않는다. 갱신/취소 동기화는 후속 설계.
- 로컬 PC가 꺼져 있으면 수신은 실행되지 않는다. GitHub에 Issue만 보관된다.
- 부팅 후 자동 실행, 이벤트 기반 Actions 분류, 양방향 결과 회신은 후속 승인·검증 과제.
- `main` 병합은 사용자 승인 후에만 진행한다.
