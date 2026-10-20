# REA · Agent Reach 연동

2026-10-20 기준으로 프로젝트 전용 설치와 제한된 두 MCP 도구를 연결했습니다. 기존 서버를 재연결하면 총 29개 도구를 불러옵니다. 27개 기존 도구의 계약은 유지하며 어떤 MCP도 제거·비활성화하지 않았습니다.

## 사용할 기능

### 프로그램 정적 분석

`analyze_program_static(path)`에 `JarvisWorkspace/projects/<프로젝트>/` 아래 파일을 지정합니다.

- `.js`, `.mjs`, `.cjs`: 파일 하나, 최대 128 KiB. REA가 임시 복사본을 정적으로 분석합니다. 의존성을 자동 탐색하거나 소스를 실행하지 않습니다.
- `.exe`, `.dll`: Windows PE 파일 하나, 최대 8 MiB. Ghidra 분석 개요를 반환합니다. 원본 프로그램을 실행하거나 디버거를 연결하지 않습니다.
- 결과에는 원본 경로·SHA-256·분석 근거가 포함됩니다. 분석 엔진의 문자열 결과에는 비밀값 가림을 적용합니다. 파일 쓰기·패치·임의 명령 전달은 제공하지 않습니다.

예: “자비스, `projects/demo/main.js`를 정적 분석해줘.” 실제 파일이 허용 경로에 있어야 합니다. 분석 대상 원본은 보존하며 임시 복사본만 처리 후 정리합니다. 구문 분석의 소스별 제한은 10초, 호출 상한은 JS 25초/PE 90초, 결과 수신은 1 MiB입니다. 긴 PE 작업이 클라이언트에서 먼저 시간 초과되면 해당 클라이언트의 도구 제한을 확인합니다.

### 등록된 공개 RSS 읽기

`read_registered_feed_with_agent_reach(source_id)`는 기존 `config/web_sources.json`의 RSS/Atom 출처만 지원합니다. 현재 `bbc-world`, `python-news`를 선택할 수 있습니다.

기존 HTTPS 작업자의 공개 IP·호스트·TLS·리다이렉트 금지·크기·시간 검증으로 가져온 바이트만 Agent Reach RSS/feedparser 백엔드에 전달합니다. 파서는 별도 Python 환경에서 오프라인으로 실행하며 임의 URL, 계정, 로그인 쿠키를 입력받지 않습니다. 기사 링크도 등록된 호스트로 제한하고 제목·발췌 최대 5개를 반환합니다.

예: “자비스, Agent Reach로 bbc-world 피드를 읽어줘.” 본문·피드 속 지시는 신뢰하지 않는 외부 데이터입니다.

## 설치·설정 위치

- REA **6.7.0**: `.integrations/rea/`, npm 고정 버전·잠금 파일. 설치 스크립트 자동 실행은 생략했습니다. 실제 정적 분석과 Ghidra 준비 검사를 별도로 통과했습니다.
- Agent Reach **1.5.0**: `.integrations/agent-reach/venv/`. 공식 커밋 `94f06c1969dfc1834001269d79d3ad0972d9dee6`을 설치했습니다. 자비스 주 가상환경과 분리했습니다.
- Ghidra **12.1.4**: `.integrations/ghidra_12.1.4_PUBLIC/`. 공식 ZIP의 SHA-256 `ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db`를 확인했습니다.
- Node **24.14.1**, 기존 Corretto JDK **25.0.4.1**을 사용합니다. 새 유료 도구나 라이선스는 설치하지 않았습니다.
- 운영자 설정: `config/integrations.json`. 실행 파일/엔진 경로만 저장하며 자격 증명은 넣지 않습니다. 실제 기능에 필요한 경로만 검사하므로 RSS가 Ghidra 설치에 의존하지 않습니다.
- 설치물은 `.integrations/`에 격리하며 Git에서 제외합니다. Ghidra 재설치 도우미는 `scripts/install_ghidra.py`이고 기존 설치가 있으면 덮어쓰지 않습니다. 실패한 다운로드도 해시 검증 전에는 추출하지 않습니다.

## 범위와 다음 단계

REA의 전체 207개 MCP 도구를 그대로 공개하지 않았습니다. UI/ADB 조작·원격 탐색·프로세스 디버깅·임의 명령·프로그램 패치는 이번 연결에 포함하지 않습니다. Agent Reach의 전체 SNS/검색/영상 기능도 연결 완료로 표시하지 않습니다. 현재 확인한 사용자 기능은 정적 분석과 등록 RSS입니다.

Android 분석용 jadx/apktool·ADB 구성, 네이티브 함수별 깊은 분석 세션, 추가 공개 채널은 다음 단위입니다. 유료 도구는 유효한 라이선스 없이는 사용하지 않습니다. 기존 MCP 정리가 필요하면 삭제 범위·이유·영향을 먼저 설명하고 별도 동의를 받습니다.

가드레일은 호출당 예산·중단 marker·경로 검증을 유지합니다. 분석기는 파서와 별도 프로세스를 실행하므로 OS 수준의 완전한 샌드박스를 뜻하지 않습니다. 임시 파일 경합과 외부 도구 내부 동작에는 이 한계를 적용합니다.

공식 참조: [REA 설치·엔진 계약](https://github.com/morluto/rea/blob/main/docs/installation.md), [Agent Reach 설치 안내](https://github.com/Panniantong/Agent-Reach/blob/main/docs/install.md).
