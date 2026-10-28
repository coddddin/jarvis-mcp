# 문서 안내

계획·상태는 [현재 통합 계획](PLAN.md), 개발 방법은 [개발 규칙](DEVELOPMENT.md)을 기준으로 합니다. 프로젝트 루트를 기준으로 실행합니다.

## 현재 기준
- [PLAN.md](PLAN.md): V2~V5를 통합한 유일한 현재 계획·진행 상태.
- [DEVELOPMENT.md](DEVELOPMENT.md): 개발·보안·검증·문서 관리 규칙.
- [패키지 구조와 이전 계획](architecture.md): 기능별 코드 배치·의존 방향·진입점 보존·구조 검사.
- [프로젝트 지도](../AGENTS_CONTEXT.md): 에이전트가 처음 읽는 50줄 이내의 요약.
- [ADR 0001 하드웨어 자원 사용 원칙](adr/0001-hardware-resource-policy.md): 다른 프로그램 우선 자동 양보, GPU·CPU·RAM 역할, 모델 활성·절전·꺼짐 단계와 임시 수치.

## 사용 안내
- [Obsidian 로컬 검색](guides/obsidian.md): 승인된 Markdown 메타데이터 검증·별도 로컬 인덱스·개인자료 비전송.
- [GitHub 우편함](guides/github-inbox.md) · [로그인 수신](guides/github-inbox-logon.md): 수동 미리보기·승인 Issue 수신·선택 설치.
- [원격 인계](handoffs/work-2026-10-22.md) · [보안 검토·검증 기록](handoffs/work-review-2026-10-22.md): PR 의존성과 실제 검증 범위.
- [로컬 리드·외부 작업자 협업](guides/hybrid-team.md): OpenRig 배정 소스 이식, 로컬/Codex 제안 협업과 운영자 CLI.
- [자비스 전용 채팅](guides/chat.md): 로컬 모델 대화·기록·읽기 도구와 인증·예산 경계.
- [로컬 음성](guides/voice.md): hud --voice, Space 눌러 말하기, 받아쓰기·자비스 목소리 설치와 저장하지 않는 경계.
- [Claude Code 세션 상태 보고](guides/claude-session-hooks.md): 공식 hook을 내용 없는 공통 신호로, turn_id=prompt_id, 미연결 이벤트와 설치 전 확인.

- [플러그인 추가·구조](guides/plugins.md): 파일별 도구와 자동 로더 계약, 공통 실행 경계.
- [AI 직원 명부와 온보딩](guides/agents-roster.md): config/agents.json 한 곳에 직원 등록, 수습·등급, 새 도구 들이는 절차.
- [휴대폰 Telegram 상태 조회](guides/telegram.md): PA4-T1 조회 전용, 롱폴링(포트 개방 없음), 페어링한 1:1 대화만, DPAPI 토큰.
- [W5 디스패처](guides/dispatcher.md): 게시판에 일이 온 AI 직원을 자동으로 깨우는 순환, 브랜치 작업·머지 승인·상한·장부.
- [에이전트 스킬: 이슈 트래커](agents/issue-tracker.md): 이슈는 JARVIS 게시판 작업으로 다룸.
- [에이전트 스킬: 분류 라벨](agents/triage-labels.md): 게시판에 라벨이 없어 `Triage: <label>` 줄이나 노트로 기록.
- [에이전트 스킬: 도메인 문서](agents/domain.md): 루트 GLOSSARY.md와 docs/adr/를 필요할 때 만듦.
- [공용 작업 게시판](guides/board.md): Codex·Claude Code가 담당·경로 claim·lease·인수인계를 공유하는 board_* 도구 7개.
- [데이터 암호화 백업](guides/backup.md): JarvisWorkspace·인증 폴더의 restic→Cloudflare R2 백업, 복원 확인, 예약 등록.
- [스냅샷 백업·복원](guides/snapshots.md): 프로젝트 백업, 복원 미리보기·적용·실패 복구와 제한.
- [파일 정리·다운로드 미리보기](guides/file-organization.md): 분류·백업·이동·기록·되돌리기와 다운로드 읽기 전용 경계.
- [등록형 작업 환경 런처](guides/workflows.md): 프로필 등록·미리보기·기동·상태·관리 서비스 종료.
- [에러 분석·수정 제안](guides/debug-assistant.md): 로컬 로그 진단·비밀값 가림·Python 문법 점검·읽기 전용 diff 제안.
- [공개 웹 정보 수집](guides/web-information.md): 등록된 Python/BBC/서울 날씨·문서 수집, 출처·발췌·시간·로컬 보고서.
- [시스템 도구](guides/system-tools.md): 서버 실행, 명령·시스템 상태·화면 캡처, 공통 제한.
- [문서 검색·Google 연결](guides/knowledge-and-google.md): 로컬 인덱싱과 OAuth 준비 절차.
- [에이전트 작업 상태·재시도 제어](guides/agent-tasks.md): Phase 2.5 영속 상태 API, 역할 순서·수정 상한·재시작과 제한.
- [에이전트 QA·예산·기억·이벤트](guides/agent-controls.md): 제한 수식/컨테이너 QA, 예약 예산, 재개·보존, 기억 관리와 인증된 WebSocket.
- [로컬 작업 HUD](guides/local-hud.md): 실행·연결 파일 선택·작업/QA/예산/이벤트 표시와 읽기 전용 범위.
- [데스크톱 앱](guides/desktop-app.md): Tauri 창·트레이·Ctrl+Alt+J·미니 고리, 서버 자식 프로세스와 작업 개체, IPC 경계·측정.
- [REA · Agent Reach](guides/rea-agent-reach.md): 전용 설치, 단일 프로그램 정적 분석과 등록 RSS 읽기.

## 보관 자료 (평소에는 읽지 않음)

당시 기록입니다. 현재 상태는 PLAN.md가 우선합니다. 에이전트는 필요한 절만 grep으로 찾습니다.

- 단계 보고서: [계획 이력](archive/reports/plan-history.md) · [협업 엔진](archive/reports/hybrid-team.md) · [Phase 1](archive/reports/phase-01.md) · [Phase 2](archive/reports/phase-02.md) · [Phase 2.5](archive/reports/phase-02-5.md) · [Phase 3](archive/reports/phase-03.md) · [REA · Agent Reach](archive/reports/rea-agent-reach.md)
- 옛 계획 원문: [V2](archive/MASTER_BLUEPRINT_V2.md) · [V3](archive/MASTER_BLUEPRINT_V3.md) · [V4](archive/MASTER_BLUEPRINT_V4.md) · [이전 개발 지침](archive/DEVELOPMENT_GUIDELINES_ORIGINAL.md) · [V5 원안](master_blueprint_v5.md)(사용자 지정 위치의 보관본)

## 변경 후 확인

단계 완료 체크리스트와 설계 결정 기록은 [현재 계획](PLAN.md)에 누적합니다. 문서를 바꾼 뒤 프로젝트 루트에서 `python scripts/check_docs.py`를 실행합니다. 필수 파일·로컬 파일 링크·문서 목록 누락·지도 길이를 검사하며 저장 시 자동 실행되는 설정은 아닙니다. 검사 범위는 [개발 규칙](DEVELOPMENT.md)을 참조하세요.

