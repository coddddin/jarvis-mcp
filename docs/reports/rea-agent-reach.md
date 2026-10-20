# REA · Agent Reach 구현·검증 기록

## 2026-10-20 — 설치 및 첫 가드레일 연동

PLAN.md의 같은 날짜 설치 실패 인계에서 재개했다. 권한이 있는 실행에서 npm/GitHub 접근이 정상임을 확인하고, 시스템 전체 자동 설정 대신 프로젝트 전용 설치와 고정 기능 어댑터를 선택했다. 기존 클라이언트·MCP·개인 스킬 설정은 변경하지 않았다.

### 설치·준비

- Node 24.14.1에서 REA 6.7.0을 `.integrations/rea`에 설치했다. `--ignore-scripts`와 버전 고정을 적용했다. npm의 기존 하위 의존성 폐기 예고는 남아 있다.
- Agent Reach 공식 커밋 `94f06c1969dfc1834001269d79d3ad0972d9dee6`에서 1.5.0을 별도 venv에 설치했다. 기본 권한의 ensurepip 실패는 권한 있는 환경 생성으로 해결했다. 패키지 자체 설치·CLI 진단과 자비스 기능 연동을 구분했다.
- Ghidra 공식 12.1.4 ZIP 569,649,598바이트의 SHA-256을 확인하고 전용 폴더에 추출했다. 기존 Corretto 25.0.4.1 JDK를 재사용했다.
- `rea doctor --provider ghidra`에서 scope healthy=true와 Windows x64/네이티브 decompiler/JDK/Job Object 준비 확인. environment_healthy=false는 미설치 선택 제공자·클라이언트 등록까지 전체 준비되었다는 뜻이 아님을 보존한다.
- Agent Reach doctor는 SNS 로그인·Exa·gh·일부 영상 기능의 미설치/설정 필요를 보고했다. web 채널의 일반 ok는 실제 HTTP 읽기 증거가 아니므로 전체 채널 동작으로 주장하지 않는다.

### 구현

- `jarvis/features/integrations/`의 서비스와 파일별 어댑터 2개, `config/integrations.json`, 오프라인 RSS 작업자를 추가했다.
- `analyze_program_static`: projects 아래 단일 JS/PE 파일 복사 후 고정 REA 명령만 호출한다. 원본 실행·수정·사용자 명령 전달을 제공하지 않는다. 경로/링크·하드링크·입력 크기·시간·출력·stop 검증을 적용했다.
- `read_registered_feed_with_agent_reach`: 기존 공개 HTTPS 검증을 재사용하고 등록된 feed만 오프라인 Agent Reach RSS/feedparser로 처리한다. 쿠키/로그인/임의 URL은 받지 않는다.
- stdout 상한·시간 초과·실패를 성공 결과로 숨기지 않는다. JSON 직렬화 전체에 redaction을 적용하면 구조가 깨질 수 있음을 실제 PE 결과에서 발견해 문자열 값별 가림으로 수정했다.
- RSS 작업자의 최소 환경에서 사용자 프로필 경로가 없던 실패를 발견해 전용 비자격 프로필 경로를 전달했다. 사용자 브라우저/계정 정보를 가져오지 않는다.

### 검증

- 관련 기능·플러그인·구조·웹 및 기존 계약 **78개 통과**(4.19초). 기능별 의존성 분리 보완 후 새 연동 **11개 통과**(0.74초).
- 실제 REA JavaScript 분석: 별도 시험 프로젝트의 합계 소스 분석과 원본 SHA 기록. 실제 Ghidra 분석: 복사한 Windows `where.exe`를 실행하지 않고 분석 개요를 얻었다.
- 실제 공개 BBC RSS를 제한된 HTTPS 경로로 읽고 Agent Reach 파서가 기사 목록을 반환했다.
- 실제 stdio MCP 클라이언트로 **29개 도구 등록**, 새 두 도구 모두 `is_error=false`를 확인했다. 기존 27개 도구 계약 비교도 통과했다.
- RSS가 네이티브 엔진 경로와 독립적임을 추가 확인한 최종 연동 검사 **12개 통과**(0.79초). 문서·구조 검사도 통과했으며 Agent Reach 설치 환경의 의존성 목록은 .integrations/agent-reach/requirements.lock.txt에 보관했다.
- 실제 증거는 Git 제외 `artifacts/integrations/{js,pe,feed,mcp}-verification.json`에 두었다. 설치물·자격 증명은 보고서에 복사하지 않았다.

### 다음 동작·남은 범위

현재 실행 중인 기존 자비스 연결은 재연결해야 새 두 도구를 불러온다. Android jadx/apktool/ADB, 더 깊은 네이티브 세션 도구, 추가 Agent Reach 공개 채널과 계정 채널은 미완료다. 일반 설치와 완전한 플랫폼 통합을 구분한다. 제거/비활성화 동의가 필요한 작업은 실행하지 않았다.

다음 단위는 무료 Android 분석 엔진의 공식 배포·지원 범위를 확인하고 실제 작은 APK를 대상으로 읽기 전용 분석을 연결하는 것이다. 정책 검증 없는 전체 도구 전달이나 로그인 쿠키 자동 수집은 하지 않는다.
