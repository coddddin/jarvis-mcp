# 문서 안내

계획·상태는 [현재 통합 계획](PLAN.md), 개발 방법은 [개발 규칙](DEVELOPMENT.md)을 기준으로 합니다. 프로젝트 루트를 기준으로 실행합니다.

## 현재 기준

- [PLAN.md](PLAN.md): V2~V5를 통합한 유일한 현재 계획·진행 상태.
- [DEVELOPMENT.md](DEVELOPMENT.md): 개발·보안·검증·문서 관리 규칙.
- [패키지 구조와 이전 계획](architecture.md): 기능별 코드 배치·의존 방향·진입점 보존·구조 검사.
- [프로젝트 지도](../AGENTS_CONTEXT.md): 에이전트가 처음 읽는 50줄 이내의 요약.

## 사용 안내

- [로컬 리드·외부 작업자 협업](guides/hybrid-team.md): OpenRig 배정 소스 이식, 로컬/Codex 제안 협업과 운영자 CLI.

- [자비스 전용 채팅](guides/chat.md): 로컬 모델 대화·기록·읽기 도구와 인증·예산 경계.

- [플러그인 추가·구조](guides/plugins.md): 파일별 도구와 자동 로더 계약, 공통 실행 경계.
- [AI 직원 명부와 온보딩](guides/agents-roster.md): config/agents.json 한 곳에 직원 등록, 수습·등급, 새 도구 들이는 절차.
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
- [REA · Agent Reach](guides/rea-agent-reach.md): 전용 설치, 단일 프로그램 정적 분석과 등록 RSS 읽기.

## 단계별 보고서

- [협업 엔진 검증](reports/hybrid-team.md): 로컬 리드→Codex 실연결·검수 보류·담당 경계와 제한.

- [Phase 1](reports/phase-01.md): 초기 34개 테스트·실제 캡처와 구조 정비 후 71개 회귀 테스트 기록.
- [V2 Phase 2](reports/phase-02.md): 로컬 검색·99개 회귀 검사 이력, 런처 20개 검사 및 다운로드·런처·Google 실계정 MCP 검증 기록.
- [Phase 2.5](reports/phase-02-5.md): 영속 작업 상태·최대 3회 수정 회로 차단과 관련 검사 기록.
- [Phase 3](reports/phase-03.md): 로컬 읽기 전용 작업 화면과 실제 브라우저 연결 검증.
- [REA · Agent Reach 연동 기록](reports/rea-agent-reach.md): 설치·Ghidra/공개 RSS·실제 MCP 검증과 후속 범위.

보고서는 해당 시점의 기록입니다. 현재 상태 판단에는 PLAN.md를 우선합니다. MCP 재연결 상태처럼 이후 달라질 수 있는 사실도 보고서 작성 당시 기준입니다.

## 역사 자료

- [V2 계획 원문](archive/MASTER_BLUEPRINT_V2.md)
- [V3 구조 참조 원문](archive/MASTER_BLUEPRINT_V3.md)
- [V4 실용 기능 원안](archive/MASTER_BLUEPRINT_V4.md)
- [V5 다중 에이전트 원안](master_blueprint_v5.md): 사용자 지정 위치의 보관본. 완료 표시는 현재 구현 상태와 구분합니다.
- [이전 개발 지침 원문](archive/DEVELOPMENT_GUIDELINES_ORIGINAL.md)

보관 문서는 당시 파일명·진행 상태·참조 표현을 그대로 유지합니다. 현재 실행 지침으로 사용하지 않습니다. 새 버전별 계획서를 루트에 추가하지 않고 PLAN.md를 갱신합니다.

## 변경 후 확인

단계 완료 체크리스트와 설계 결정 기록은 [현재 계획](PLAN.md)에 누적합니다. 문서를 바꾼 뒤 프로젝트 루트에서 `python scripts/check_docs.py`를 실행합니다. 필수 파일·로컬 파일 링크·문서 목록 누락·지도 길이를 검사하며 저장 시 자동 실행되는 설정은 아닙니다. 검사 범위는 [개발 규칙](DEVELOPMENT.md)을 참조하세요.
