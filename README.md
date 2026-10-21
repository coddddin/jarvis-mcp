# J.A.R.V.I.S. MCP

안전 가드레일을 적용한 로컬 MCP 서버입니다. 시스템 도구, 로컬 문서 검색, 읽기 전용 Google 브리핑 코드를 제공합니다.

## 문서 시작점

- [자비스 전용 채팅 실행·사용법](docs/guides/chat.md)

- [전체 문서 안내](docs/README.md)
- [현재 통합 계획·진행 상태](docs/PLAN.md)
- [개발·문서 관리 규칙](docs/DEVELOPMENT.md)
- [시스템 도구 사용법](docs/guides/system-tools.md)
- [문서 검색·Google 연결](docs/guides/knowledge-and-google.md)
- [프로젝트 요약 지도](AGENTS_CONTEXT.md)
- [기능별 구조·이전 설계](docs/architecture.md)

## 현재 상태

Phase 1/2와 Phase 2.5 핵심 초기 구현을 검증했습니다. 기존 MCP 도구 27개를 보존하고 [REA 정적 분석·Agent Reach 등록 RSS](docs/guides/rea-agent-reach.md) 2개를 추가해 현재 29개입니다. 로컬 모델/격리 QA·명시적 백업 적용·영속 예산/기억·인증 이벤트는 운영자 CLI로 제공합니다. Phase 2.5 전체 검사는 [344개 통과/건너뜀 0개](docs/archive/reports/phase-02-5.md)입니다. Phase 3의 [읽기 전용 작업 HUD](docs/guides/local-hud.md)도 실제 기록에 연결했습니다. 일반 프로젝트 자동 개발과 3D/음성 HUD는 후속 범위입니다.

작업 화면은 `scripts/start-hud.cmd`를 실행한 뒤 연결 파일을 선택합니다. 설치된 Ollama/Docker와 화면 서버의 실행은 별개입니다.

## 실행

프로젝트 루트에서 Python 3.13 기준으로 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

stdio MCP 서버이므로 클라이언트 요청을 기다립니다. 기본 데이터 작업공간은 `JarvisWorkspace/`입니다. 코드·가상환경·인증정보는 문서 데이터와 분리합니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
python scripts/check_docs.py
python scripts/check_architecture.py
```

문서의 현재 기준은 `docs/PLAN.md` 하나로 관리합니다. 단계 보고서와 이전 계획은 [문서 안내](docs/README.md)에서 찾습니다. 실행 코드는 jarvis/의 core/app/features/workers/cli로 정리했습니다. 루트의 두 실행 진입점과 MCP 등록·작업공간·인증정보·설정 위치는 유지했습니다. 기존 서버 프로세스는 재연결해 새 구조를 불러옵니다.

V4 스냅샷·파일 정리·등록형 런처·읽기 전용 에러 분석·등록형 웹 수집의 초기 구현을 완료했습니다. 일반 자동 코드 수정·임의 웹 검색은 후속 항목입니다.

도구별 파일 분리와 자동 로더를 적용했습니다. 새 도구 추가 방법은 [플러그인 안내](docs/guides/plugins.md)를 참조하세요.

V5의 Planner·Coder·QA 흐름은 로컬 Ollama와 명시적 테스트/적용 범위로 구현했습니다. [작업/적용](docs/guides/agent-tasks.md)과 [QA·예산·기억·이벤트](docs/guides/agent-controls.md)를 따릅니다. 광범위한 자동 프로젝트 수정은 현재 초기 범위와 구분합니다.

[스냅샷 사용 안내](docs/guides/snapshots.md): 작업공간의 projects/<name>만 백업·미리보기·병합 복원합니다. 실제 사용자 프로젝트 복원은 수행하지 않았습니다.

[파일 정리 사용 안내](docs/guides/file-organization.md): 프로젝트 분류 미리보기·백업·이동·실행 기록·되돌리기와 선택적인 다운로드 읽기 전용 시뮬레이션을 제공합니다. 실제 사용자 파일은 개발 중 변경하지 않았습니다.

[작업 환경 런처](docs/guides/workflows.md): 등록 작업을 미리보기·기동·조회·종료합니다. local-status와 현재 자비스 코드용 jarvis-development를 제공하며 실제 상태 서버 응답과 종료를 검증했습니다.

[에러 분석·수정 제안](docs/guides/debug-assistant.md): 로그 패턴과 Python 문법을 로컬에서 분석하고 검증 가능한 diff만 제안합니다. 코드를 실행하거나 자동 수정하지 않습니다.

[공개 웹 정보 수집](docs/guides/web-information.md): 등록된 Python 소식·BBC 세계 뉴스·공식 Python 문서·고정 서울 날씨를 수집해 출처·시각·짧은 발췌를 로컬 보고서로 저장합니다.
