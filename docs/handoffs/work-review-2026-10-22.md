# 원격 보안 검토·Obsidian 구현 기록 (2026-10-22)

## 기준·보존

- 저장소 `coddddin/jarvis-mcp`, 인계 기준 `feat/github-inbox-logon` @ `9a1d812394f29e4d406033a1129ff3ae2e151974` (tree `fd940b2aa3da0ed3763f8cb90aec6f93b0b61f67`). PR #8/#9/#10, Issue #7을 읽었다.
- 별도 원격 브랜치 `feat/work-security-obsidian-20261022`와 격리된 클라우드 사본에서 작업. 비공개 저장소 CLI clone 인증이 없어 GitHub 커넥터로 소스를 읽었다. 사본의 로컬 스냅샷 커밋은 원격 계보로 push하지 않고 원격 기준 tree에 변경 파일만 반영한다.
- 데스크톱의 git/SQLite/실제 Vault·토큰에 접근하지 않았다. 미커밋 변경·예약 작업·main·기존 PR 브랜치는 건드리지 않았다. 파일 삭제, 서비스 활성화, Actions 실행·청구 설정 변경은 하지 않았다.
- PR #9의 classifier와 stdlib 테스트만 이 검토 브랜치에 포함했다. workflow는 복사·활성화하지 않았다. #9는 별도 Draft PR로 유지한다. #10은 #8 위에 쌓인 구조를 유지한다.

## 확인·수정

1. Inbox 작성자 login의 비문자열 예외, 180자 초과 제목 거부, 큰 Issue 번호 접두사의 보드 200자 초과, 제목 강제 문자열 변환, 직접 함수 호출의 승인 우회를 수정했다. 전체 제목의 보안 검사 후 길이를 줄이며 로스터의 user ID 검사를 재사용한다.
2. Authorization 헤더의 리디렉션 전달을 차단하고 승인 라벨로 API 조회를 좁혔다. 3페이지가 모두 차면 조용한 누락 대신 적용 전 실패한다. 응답 크기·오류 비밀값 비출력·네트워크 전 stop·preview 무쓰기 검증을 추가했다.
3. Issue별 매핑·task·event 원자성, 동시 수신 중복 방지와 실패 롤백을 검증했다. 직접 SQL에는 명시적 컬럼을 쓴다. 여러 Issue 전체가 단일 원자적 배치는 아니다.
4. 로그인 스크립트의 `Register-ScheduledTask -Force`를 제거하고 기존 동명 작업이 있으면 중단한다. 현재 사용자 전체 계정명·Interactive/Limited principal을 지정한다. Windows 실행은 미검증이다.
5. Triage의 최상위 비정상 자료·양수가 아닌 번호·빈 제목을 거부한다. 이벤트 파일은 실제 read 상한을 적용하고 JSON 오류에 원문을 출력하지 않는다. 기존 workflow는 읽기 전용 권한, 제한된 이벤트, Issue 텍스트 shell 미삽입을 확인했다. checkout 태그의 SHA 고정·실제 Actions/비용 검증은 #9 후속이다.
6. Vault 어댑터와 2개 도구를 추가했다. approved/non-secret `.md`의 엄격한 frontmatter 검증, 비밀 패턴·링크 차단, 메타데이터 반환, 별도 인덱스·승인 철회 재검증. 기존 KnowledgeStore에 인덱스 디렉터리 상수와 stop 검사를 추가해 검색 엔진을 재사용한다. 기존 도구 계약은 유지한다. [가이드](../guides/obsidian.md).

## 검증

- 환경: Linux, Python 3.12.14. 저장소 requirements.txt의 지정 버전과 pytest 9.1.1을 테스트 환경에 설치했다. 실제 계정·외부 AI·사용자 자료 없이 합성 데이터와 임시 SQLite만 사용했다.
- 최초 Inbox 5개·Triage 4개 통과. 보드/디스패처 묶음 111개 통과, Windows Job Object 검사 1개가 Linux no-op 구현 때문에 실패했다. 이 검사는 Windows에서만 실행하도록 플랫폼 조건을 명시했고 Windows 통과로 보고하지 않는다.
- 수정 후 변경 기능·공유 검색·보드·디스패처·로더/기존 계약·MCP stdio 회귀 묶음 결과는 아래 최종 실행 기록에 기입한다. 전체 회귀 실행이 아니다.
- 프런트엔드 기존 검사: `node --test tests/frontend/*.test.cjs` **22 passed**.
- 문서 검사에서 기존 지도 51줄·신규 Inbox 문서 색인 누락을 확인해 50줄로 정리하고 색인을 보완했다. 처음 사본에서 생략한 보관 문서는 기준 커밋에서 추가로 읽어 링크 검사에 포함했고 원격 내용은 변경하지 않았다.

## 남은 문제·다음 작업

- PC 복귀 시 데스크톱 `git status`, worktree, 게시판 현황부터 확인한다. 강제 checkout/reset/stash나 기존 미커밋 변경 채택 금지. 이 브랜치 동기화·예약 등록은 사용자와 현장 상태를 확인한 후 별도로 진행한다.
- 실제 read-only GITHUB_TOKEN 인증·권한/만료·로그인 환경변수 상속·Windows 예약 실행·Job Object 종료·현장 동시 Dispatcher 수신은 미검증이다. StartNow는 스케줄러의 기존 환경을 쓸 수 있으므로 로그아웃/로그인 및 LastTaskResult 확인이 필요하다.
- Inbox의 open 작업은 리드의 idle 판정을 막는다. 수동 배정/분류가 필요하며 이를 자동 위임으로 바꾸지 않았다. 라벨 부착자 인증·신원 사칭 방지(W4-2)는 기존 한계다.
- PR #8과 새 Draft PR #11은 생성/최초 조회 응답의 mergeable=false 이후 재조회에서 모두 mergeable=true로 확인됐다. 최초 값을 확정된 충돌로 해석하지 않는다. #8/#10/#9와 #11을 각각 검토하고 병합 순서를 정해야 하며 실제 병합은 수행하지 않았다.
- 실제 Vault 형식·검색 품질·메타데이터 변환, 노트 쓰기·동시 편집 정책과 실제 백업 복구는 후속이다. 중복 ID·중간 변경·합성 staging 복원 검증은 아래 후속 기록을 참조한다. 전체 YAML을 지원하지 않는다. 범용 문서 도구의 workspace 읽기 권한과 직원별 ACL은 별도다.
- 후속 수정으로 직원 subprocess의 환경변수 전체 상속을 제한했다. 저장된 CLI 계정·사용자 파일까지 격리하지 않으므로 파일 접근·무인 인증 정책의 현장 검토는 여전히 필요하다. 토큰 저장 정책 변경이나 무인 실행 활성화는 하지 않았다.
- 로컬 보드에 접근하지 못해 board_claim/note/release는 기록하지 못했다. 이 기록과 Draft PR이 원격 검토 증거이며 로컬 이슈 진행 상태로 가장하지 않는다.

## 최종 실행 기록

- `python -m pytest -q tests/test_github_inbox.py tests/test_obsidian.py tests/test_phase2.py tests/test_plugins.py tests/test_dispatch_service.py tests/test_board.py tests/test_dispatch.py tests/test_architecture.py tests/test_github_triage_unittest.py`: **203 passed, 1 skipped**, 6.49초. skip은 Windows Job Object 검사 한 건이다.
- `python scripts/check_docs.py`: 통과. `python scripts/check_architecture.py`: 통과. `git diff --check`: 통과.
- 프런트엔드 별도 **22 passed**. 전체 테스트 실행이나 Windows 실연동 성공으로 해석하지 않는다.
- 새 도구를 포함한 MCP stdio 기동·목록 검사와 기존 계약 보존 검사를 실행했다. PowerShell/pwsh가 설치되어 있지 않아 파서·예약 등록은 실행하지 못했다.
- 원격 후속 Draft PR의 head 커밋이 배포 전 검토 기준이다. main 및 기존 PR의 원격 ref는 이동시키지 않는다.

## 후속 진행 — 환경변수·Vault 충돌·합성 백업 (2026-10-22)

- 사용자 지시: 다음 개발을 진행하고 사용자가 필요한 단계에서 알린다. 기존 별도 브랜치/PR #11에서 이어서 작업했다. 데스크톱·토큰·Vault·백업 계정에는 접근하지 않았다.
- `jarvis/core/process_env.py`로 명시적 환경변수 허용 목록을 만들고 디스패처/HybridTeam에 적용했다. Inbox/모델 토큰·임의 환경변수·호스트 OpenCode 설정·프록시/인터프리터 주입 설정을 자식에게 자동 전달하지 않는다. 실제 worker spawn 모의검사와 실제 Python 자식 프로세스의 합성 비밀값 부재를 확인했다.
- OpenCode 제한 설정은 신뢰된 어댑터가 생성하며 workspace는 guard의 실제 경로를 사용한다. API키·SSH agent 환경만 사용하는 인증은 실패할 수 있다. 전체 환경 상속이나 유료 fallback으로 자동 복구하지 않는다. HOME 등 계정 경로는 유지하므로 OS/자격증명 파일 격리 보장은 아니다.
- Vault 빌드 최종 검증에서 중복 ID와 빌드 중 원본 변경을 거부한다. 실패 시 기존 인덱스를 유지한다. 검색 발췌에 원본 SHA256을 연결해 읽는 도중 다른 승인 버전으로 바뀌면 결과를 숨긴다. 일반 문서 검색의 기존 출력 계약은 바꾸지 않았다.
- 합성 Vault를 기존 backup staging에 포함하고, 실행 중 WAL SQLite의 커밋을 online backup API로 보존했다. 별도 임시 경로에 복원하여 Markdown SHA256·SQLite integrity_check·검색 재개를 확인했다. 기존 백업 코드/설정은 수정하지 않았고 restic/R2 네트워크·암호화·실제 사용자 백업은 검증하지 않았다.
- 최신 관련 회귀 명령: `python -m pytest -q tests/test_process_env.py tests/test_dispatch_cli.py tests/test_dispatch.py tests/test_dispatch_service.py tests/test_hybrid_team.py tests/test_obsidian.py tests/test_backup_vault.py tests/test_phase2.py tests/test_plugins.py tests/test_architecture.py` → **178 passed, 1 skipped**, 6.96초. Windows Job Object 검사는 이 Linux 환경에서 미실행이다. 이전 203개 검사와 겹치므로 합산하지 않는다.
- `python scripts/check_docs.py`, `python scripts/check_architecture.py`, `git diff --check`를 후속 변경에 다시 적용한다. 실제 Windows CLI 로그인·환경 상속·예약 작업/토큰 수신·실제 Vault/백업 정책 확인 시 사용자 PC가 필요하다. 그 전에는 병합·설치·등록을 하지 않는다.
