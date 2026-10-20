# J.A.R.V.I.S. 통합 계획 및 진행 상태

갱신일: 2026-10-20 (Asia/Seoul). V2의 지식·HUD 로드맵, V3의 모듈화 원칙, V4의 실용 도구, V5의 다중 에이전트 구상을 통합한 현재 기준입니다. 원본은 [문서 안내](README.md)의 역사 자료에 보관합니다.

## 비전

안전한 FastMCP 파이썬 백엔드와 시각·음성 HUD를 통해 PC 제어, 개인 지식 검색, 일정·이메일 브리핑을 제공하는 개인 에이전트를 구축합니다. 실행 코드·인증정보와 도구가 접근하는 문서 작업공간을 분리합니다.

## 현재 상태

- 2026-10-21 Claude Code 전체 회귀 확인(Codex 한도 대기 중): tests/ 전체 509개 통과, 구조/문서 검사 통과. 도구 수 고정 테스트 2개(27/29개 기준)를 현재 31개에 맞추고, `pytest -q`가 artifacts/references를 수집하지 않도록 pytest.ini를 추가했으며, phase-03 보고서의 링크 오인식 1건을 고쳤다. 제품 코드 변경 없음. 10-20 변경분 검토에서 재현된 버그 2건(무료 배정의 호출 예산 초과 시 거짓 running 기록, 페이지 읽기의 이중 HTML 해석으로 본문 소실)과 수정 제안, 확인 항목은 `artifacts/claude-review/HANDOFF-2026-10-21.md`.
- 2026-10-20 무료 자동 배정 연결부: app/dispatch.py와 요청별 dispatch JSON을 추가했다. 실행 전 원자적 예약, 요청별 3회/12000 토큰 예약 상한, 확인된 한도 실패만 다른 무료 후보로 전환, timeout/unknown 재실행 금지, 응답 원문 보존을 검사했다. 자연어의 명시적 무료 API 요청과 mode=free는 로컬 추론 없이 연결되며 현재 실제 등록 후보/실행기는 없다. 무료 호출 성공으로 간주하지 않는다. 관련 Python111개·Node12개 통과, 직접 무료 경계9개 별도 통과. [기록](reports/phase-03.md).

- 2026-10-20 무료 API 배정·한도 정책: 검증된 무료 후보 선택, 요청 전체 시도/토큰 누적, 한도 실패 시 다른 무료 후보, 불명확 timeout 중복 방지 계약 구현·19검사 통과. 실제 자동 실행/영속 트랜잭션 연결은 미완료. OpenCode 앱 사용 가능은 사용자 확인, CLI 무료 모델 호출은403으로 미연결이며 Gemini 앱/CLI는 일반 경로 미발견이다. 새 OpenCode 경로에 Ollama·유료 자동 승격은 사용하지 않는다. [세부 검증](reports/hybrid-team.md).

- 2026-10-20 자동 요청 판단: `mode=auto`를 먼저 영속 승인한 뒤 로컬 모델이 대화/로컬 협업·필요 근거·검색/원문·미지원 여부를 분류한다. requested_mode/request_plan을 추가해 분류 전후와 인스턴스 간 중복 요청을 재실행하지 않는다. 외부 AI 자동 선택 없음. 사전 분류가 요구한 근거는 최종 general 답변으로 해제할 수 없다. 실제 6유형 시험에서 PC 조회/검색/협업 분기·미지원 거부 확인, 함수 협업은 검수 보류 needs_user_input이었다. 관련 163개 검사 통과, 최신 필수 필드 관련 10개 별도 통과. 8788 반영 후 브라우저의 자연어 PC 조회도 확인했다. 모델 오분류와 키워드 보조 검사의 한계는 남는다. [기록](reports/phase-03.md).

- 2026-10-20 Claude 연결: 사용자 결제·직접 Pro 로그인 후 Claude Code 2.1.302의 도구 없는 호출 및 실제 로컬 리드→로컬 조사→Claude 작성→로컬 검수/종합 전달을 확인했다. 시험은 검수보류/외부1회상한으로 needs_user_input, 기록 해시 ok였다. native 발견 보완 포함 협업33검사 통과. 서버 반영은 화면 담당과 조율하며 자동 외부 전송 정책은 유지한다. [검증](reports/hybrid-team.md).

- 2026-10-20 검색 원문 확인: `read_public_web_page` 추가(총31도구/기존27계약보존). 채팅에서는 현재 요청의 성공 검색 결과 URL과 정확히 일치하는 주소만 읽는다. 기존 pinned HTTPS 제한으로 최대3000자 HTML/텍스트 발췌를 반환하고, 검색 발췌와 실제 페이지 발췌를 구분한다. 실제 로컬 모델 및8788 브라우저에서 검색→Python 공식 사이트 조회 성공을 확인했다. 관련155개 검사·문서/구조 검사 통과. 전체 문서·JS 렌더링·사실 정확성 검증은 아니다. [기록](reports/phase-03.md).

- 2026-10-20 동시 요청 검증: 별도 저장소/엔진 인스턴스 두 개의 동일 요청 경쟁에서 한 번만 계획·작업 실행, 다른 호출은 거부됨을 확인했다. 전용 31개 및 화면/이벤트 Node 11개 통과(별도 실행). 새 종료사유 한국어 매핑 소스 확인, 실제 서버·브라우저 검증은 화면 담당이 진행 중이다. [기록](reports/hybrid-team.md).

- 2026-10-20 협업 품질 묶음 완료: 일부 원문만 본 검수의 전체 통과 차단, 종합 답변의 작업 원문 누락 방지·6000자 내 균등 발췌, 종합 실패 시 보존 결과 제공, 단계 실패의 안전한 안내를 반영했다. 전용 30개 및 최신 채팅 호환 65개 통과(별개 실행). 외부 모델 호출·코드 적용 없음. 화면 담당에게 추가 종료 사유를 전달했으며 최신 서버 반영은 별도 확인 범위다. [세부 보고](reports/hybrid-team.md).

- 2026-10-20 제한형 실제 검색: `search_public_web`를 추가해 총 MCP 30개, 기존 27개 계약을 보존했다. 고정 Bing RSS를 기존 검증 IP 고정 HTTPS 작업자로 조회하며 최대3개 발췌/200자 검색어/저장 없음이다. 한국어 검색어는 예약 예산을 차감한 로컬 추론으로 다듬고, 최종 검색 답변은 서버가 실제 제목·발췌·주소·UTC 시각으로 구성한다. 실제 한국어/영어 검색, 미지원 순위 차단, 목록→문서 연속 조회 확인. 관련136검사 통과. 검색 결과의 정확성/최신성/원문 확인과 완전한 의미 관련성은 보장하지 않는다. [기록](reports/phase-03.md), [안내](guides/web-information.md).

- 2026-10-20 협업 검수 모순 차단: 통과 표시와 미해결 문제 목록이 충돌하면 완료 처리하지 않고 수정/사용자 확인으로 보낸다. 원래 판정과 조정 이유를 보존한다. 협업 검사 24개 통과, 외부 모델 호출 없음. 모델 검수 내용의 정확성은 여전히 개선 과제다.

- 2026-10-20 검증 대조: 협업·채팅 검사 53개 통과(호스트, 4.71초). 화면 담당의 실제 자동 로컬 협업은 배정 3개와 모델 검수 완료였고, 해당 요청의 보존 기록 4개 해시도 일치했다. 실행 QA 완료는 아니다. Claude 데스크톱 앱 설치 확인, CLI는 일반 경로 미발견·무료 요금제 이용 권한 없음(공식 안내). 이번 외부 모델 호출 없음. [세부 기록](reports/hybrid-team.md).

- 2026-10-20 협업 보존 기록 확인: 운영자 `team verify`로 버전·검수 파일의 저장 해시를 비교한다. 변경·누락·잘못된 참조를 성공과 구분하고 모델 호출이나 작업 재실행 없이 확인한다. 협업 검사 22개 통과, 기존 실제 협업의 기록 7개 모두 해시 일치. 코드 정확성·DB 진위 보장은 별도 범위다. [안내](guides/hybrid-team.md).

- 2026-10-20 채팅 화면 후속: 사용자 요청에 따라 모드 선택 UI 대신 `mode=auto` 전송을 적용했다. 현재 문구 규칙으로 일반 대화/조회와 로컬 협업 제안을 나누며 외부 전송은 자동 선택하지 않는다. 기존 명시적 chat/team-local/team API는 유지한다. 한국어 협업 진행·모델 검토·수정 횟수·정지 사유와 응답 전달/작업 판정을 구분한다. 관련 Python 57개·화면 동작 Node 11개 통과(기존 작업 스트림 8개 포함). 8788 서버 반영 후 방식 선택 없는 실제 함수 제안 요청이 로컬 협업으로 분기됐고 turn/team 모두 completed였다. 코드 실행·적용과 일반 웹 검색은 미수행/미연결이다. [기록](reports/phase-03.md).

- 2026-10-20 협업 결과 안내 보완: 중단 이유·수정 횟수·외부 호출 수를 최종 답변에 서버가 표시하고, 길이 제한에서도 실행·적용 미수행 안내를 유지한다. 관련 협업 검사 20개 통과(2.62초). 화면 담당은 별도로 인증·모드·진행 표시를 검증 중이다.

- 2026-10-20 협업 수정 루프: 로컬 리드가 같은 담당자에게 수정·재검수를 배정하고 의존 작업을 재계산한다. 최대 수정 3회·외부 호출 3회·전체 20단계·600초를 공유한다. 최신 협업 검사 20개 통과. 실제 Codex 수정 2회 후 외부 호출 상한에서 `needs_user_input`으로 정지했고 버전·검수 기록을 보존했다. [검증 기록](reports/hybrid-team.md). HUD 연결·서버 확인은 새 로컬 채팅 담당 세션이 진행 중이다.

- 2026-10-20 협업 엔진: OpenRig 역할 선택 소스를 이식하고 로컬 리드의 배정→로컬/Codex 작업→독립 문맥 검수→로컬 취합 및 운영자 CLI를 구현했다. 실제 결과 전달은 확인했으나 시험의 검수 판정은 `needs_user_input`이다. 관련 협업 검사 14개 통과. [사용 안내](guides/hybrid-team.md), [실검증/인수인계](reports/hybrid-team.md). Claude 실제 실행과 HUD 연결은 미완료이며 공용 채팅 파일은 채팅 개선 세션 담당이다.

- 2026-10-20: 전용 로컬 챗봇 추가. 대화 기록·최근 문맥, 기존 가드레일을 거치는 5개 읽기 도구, 인증된 HUD 채팅 탭을 구현했다. 실제 한국어 응답·문맥 기억·PC 조회를 확인했다. [사용 안내](guides/chat.md), [검증](reports/phase-03.md). OpenRig 소스는 구조 참고이며 팀 런타임 설치/연결은 미수행이다.
- 채팅 인수인계: 8788 포트의 로컬 화면과 설치된 Ollama를 이번 작업에서 시작했다. 대화는 .jarvis-chat/conversations.sqlite3에 보존한다. 관련 57개 검사 후 최신 정보/기능 소개 보완에 채팅 14개 검사 통과. 다음 확장은 채팅→기존 개발 작업 제어 연결과 더 적합한 로컬 대화 모델 평가이며, OpenRig daemon/계정 연결·자동 코드 적용·도구 제거는 이번에 수행하지 않았다.

- Phase 1: 코어·플러그인 정비 완료. 최초 8개 도구 분리 후 현재 27개 도구와 서비스를 기능별 패키지로 이전했습니다.
- Phase 2 지식·Google: 로컬 검색 및 독립 OAuth 연결 검증 완료. 실제 자비스 MCP에서 Gmail·Calendar 모두 정상 응답 확인(2026-10-17). V4 추가 실용 도구 전체 완료를 뜻하지 않습니다.
- 최근 단계 검증: Phase 2.5 핵심 묶음 후 전체 344개 통과/건너뜀 0개(19.98초). 기존 27개 도구 계약·진입점·보안 경계를 포함했습니다. 테스트 클라이언트 의존성 폐기 예고 경고 1개가 있으며 실제 통신도 검증했습니다. [Phase 2.5 보고서](reports/phase-02-5.md).
- Phase 3: 로컬 읽기 전용 작업 HUD 첫 단위 구현·실제 연결 검증. 3D/음성 등은 후속 범위입니다. [사용 안내](guides/local-hud.md), [보고서](reports/phase-03.md).
- V4 추가 반영: 플러그인 정비·스냅샷·파일 정리·런처·읽기 전용 에러 분석·등록형 웹 수집 초기 구현 완료. 현재 도구 27개이며 일반 자동 코드 수정·임의 웹 검색은 후속 범위입니다.
- V5 Phase 2.5 핵심 초기 구현: 로컬 모델→정수/컨테이너 QA, 명시적 백업/적용, 영속 예산/중단/재개/보존, 확인된 기억, 인증된 이벤트 전달을 구현하고 실검증했습니다. 일반 프로젝트 자동 수정/적용과 환경별 통합 테스트는 확장 범위입니다. [운영 안내](guides/agent-controls.md).
- 2026-10-20 통합 확장: REA 6.7.0·Agent Reach 1.5.0·Ghidra 12.1.4 설치 및 정적 분석/등록 RSS 2개 MCP 실호출 검증. 기존 27개 보존, 총 29개. [연동 안내](guides/rea-agent-reach.md). 무료 Android 분석과 추가 채널은 다음 단위입니다.

## Phase 1 — 코어와 안전장치

- [x] FastMCP 연결과 시스템 도구 3개 실행 확인.
- [x] 경로·명령·예산·중단 정책 및 오류 동작 검증.
- [x] 결과·제한을 Phase 1 보고서에 기록. OS 격리·즉시 중단은 별도 과제로 유지.
- [x] V4 정비: 신뢰된 tools 자동 로더와 도구별 모듈로 이전.
- [x] 이전 후 기존 8개 도구 계약·공유 가드레일·전체 회귀 검증.

위 정비 항목은 기존 Phase 1 완료 기록에 추가된 새 범위입니다. 기존 구현 완료와 모듈화 완료를 구분합니다.

완료 범위는 FastMCP stdio 서버, 허용 명령 실행, CPU·메모리·디스크 상태, 시간 제한이 있는 화면 캡처입니다. 기존 요청에 따라 최초 계획의 시스템 도구를 Phase 1에 포함했습니다.

공통 경계는 작업공간 경로 검증, 명령 허용 목록, 프로세스 전체 20회 호출 예산, `.jarvis-stop` 확인입니다. [검증 기록](reports/phase-01.md)과 [사용 안내](guides/system-tools.md)를 참조합니다.

## Phase 2 — 지식 연결 및 실용 비서 확장

완료한 로컬 기능:

- UTF-8 md/txt/rst 문서 청크화, 어휘 특징 벡터·코사인 검색, 로컬 SQLite 저장.
- 출처·줄 번호·발췌문 반환, 변경·삭제 문서의 오래된 결과 제외, 검색용 사본 삭제.
- 기본 대상은 `JarvisWorkspace/documents/`. 외부 문서 업로드·신경망 임베딩 모델은 사용하지 않습니다.

구현한 Google 기능:

- 서버 전용 Desktop OAuth 로그인 도우미.
- Gmail 현재 미열람 받은편지함의 제목·발신자·날짜, 한국 날짜 기준 기본 Calendar 일정 조회.
- 읽기 전용 권한과 API별 실패 표시. 메일 발송·일정 변경·예약 브리핑은 미포함.

완료 체크리스트:

- [x] 로컬 인덱싱·검색·원본 보존·오래된 결과 제외 검증.
- [x] Google 읽기 전용 코드와 모의 API 실패 경로 검증.
- [x] 기존 기능 포함 테스트 54개 통과 기록.
- [x] Google Cloud 전용 프로젝트·Gmail/Calendar API 활성화·Desktop 클라이언트 생성·본인 테스트 사용자 1명 등록 확인.
- [x] OAuth Desktop JSON 저장: 사용자가 로컬 인증 파일에 직접 입력해 저장했고 비밀값 출력 없이 준비 여부 확인.
- [x] 사용자 직접 로그인·동의 완료, 독립 서버 읽기 전용 토큰 저장.
- [x] 실제 MCP로 Gmail·Calendar 정상 응답 확인. 실패 경로는 기존 모의 검사 기록을 유지하며 실제 계정 오류를 일부러 유발하지 않음.
- [x] 검증 결과를 Phase 2 보고서에 추가하고 지식·Google 연결 상태 갱신.

[연결·검색 안내](guides/knowledge-and-google.md)를 따릅니다. Codex의 연결된 Google 계정 인증과 독립 서버의 인증은 공유되지 않습니다.

### V4 실용 도구 확장 — 스냅샷·파일 정리 초기 구현 완료

기존 지식 검색·Google 기능을 유지하면서 다음 5종을 추가합니다. 파일명은 설계 후보이며 실제 구현 시 MCP 도구당 파일 하나 원칙에 맞춰 더 나눌 수 있습니다.

- [ ] **파일 정리:** 확장자·생성 날짜·내용별 분류와 '최종'·'복사본' 후보 식별, 이동 계획·실행 보고. 먼저 미리보기만 제공하고 적용 시 원본 식별·경로·동명 충돌을 재검증합니다. 이름만으로 삭제·덮어쓰지 않습니다. 다운로드 폴더는 현재 허용 경로가 아니므로 사용자 지정 범위와 권한 정책부터 마련합니다. 내용 기반 외부 모델 전송은 별도 결정합니다.
- [x] **에러 분석·수정 보조 (읽기 전용 초기 버전):** 입력 로그와 지정 Python 소스에서 규칙 기반 원인 후보·조치와 단일 누락 콜론 diff를 제안합니다. 비밀값 가림·경로/크기 제한·파싱/컴파일 검증을 적용하며 명령 실행·코드 실행·파일 변경·모델 전송은 없습니다. 실제 적용과 일반 AI 수정은 별도 범위입니다.
- [x] **작업 환경 런처 (기본 백엔드):** 미리 등록한 앱·서버·웹사이트 묶음을 실행합니다. 임의 셸을 열지 않고 실행 파일·인자·작업 위치·도메인을 허용 목록으로 관리합니다. 중복 실행 방지, 기동 확인, 부분 실패·종료 대상을 기록합니다. 음성 문구는 추후 Phase 3와 연결하고 자동 시작은 별도 설정합니다.
- [ ] **스냅샷 백업·복원:** 지정 프로젝트의 주요 변경 전 ZIP과 파일 목록·해시를 생성합니다. 백업 자체·가상환경·캐시·인증정보는 기본 제외하고 제외 목록을 보고합니다. 복원은 미리보기·현재 상태 백업·충돌 확인을 거치며 ZIP 경로 탈출·링크·압축 폭탄을 차단합니다. 서버 운영 코드와 일반 데이터의 복원 권한을 구분하고 안전한 임시 적용·복구 절차를 검증합니다. 주기적 실행은 별도 요청 전 활성화하지 않습니다.
- [x] **웹 정보 수집 (등록형 초기 버전):** Python/BBC RSS·공식 Python 문서·고정 서울 날씨를 수집해 출처·조회 시각·짧은 발췌를 작업공간에 저장합니다. HTTPS 호스트·검증 IP 고정·리디렉션 금지·크기/시간/시도 상한·중단·안전한 새 보고서 저장을 적용합니다. 검색/요약 모델을 사용하지 않으며 임의 검색·AI 요약·다른 도시는 후속 범위입니다.

각 항목의 완료 조건은 정상·실패·권한 거부·되돌리기(상태 변경 도구 해당) 검증과 공통 정책 적용, 사용 안내 및 동일 Phase 보고서 갱신입니다. 기능을 추가해도 기존 셸 허용 목록을 무조건 확대하지 않습니다.

권장 구현 순서는 **공통 플러그인 정비 → 스냅샷 보호 → 파일 정리 미리보기·적용 → 등록형 런처 → 에러 분석 보조 → 웹 정보 수집**입니다. Google 인증 준비는 병행할 수 있으며 이 순서가 각 후속 도구를 자동 착수한다는 뜻은 아닙니다.

스냅샷 세부 상태 (2026-10-17):

- [x] projects/<name> 백업·목록·복원 미리보기·병합 복원 도구 4개. 합계 MCP 도구 12개.
- [x] 복원 전 백업·파일별 교체·실패 롤백·미완료 복구 기록·만료 및 변경 상태 거부 검증.
- [x] 기존 8개 계약 보존과 전체 테스트 99개 통과, [운영 안내](guides/snapshots.md) 작성.
- [ ] 후속 도구의 변경 전 자동 백업 연결. 주기 실행·운영 코드 및 외부 폴더 백업·장애 후 자동 복구는 이번 초기 구현에 포함하지 않음.

스냅샷은 기존 추가 파일을 삭제하지 않는 병합 복원입니다. 파일 정리에서는 별도의 이동 내역 기반 undo를 제공합니다. 등록형 런처와 읽기 전용 에러 분석도 초기 구현을 완료했으며 등록형 웹 수집도 아래 항목에서 구현 상태를 관리합니다.

### 파일 정리 세부 상태 (2026-10-17)

- [x] 프로젝트 최상위 파일의 확장자·UTC 수정월별 이동 미리보기와 이름 검토 표시.
- [x] 직전 스냅샷·충돌/변경 거부·Windows 무덮어쓰기 이동·실패 롤백·실행 기록.
- [x] 되돌리기 미리보기·재백업·undo·undo 실패 롤백. 새로운 파일/변경 파일 보호.
- [x] 운영자 opt-in 시에만 고정 Downloads Known Folder의 최상위 메타데이터 시뮬레이션. 내용 읽기·이동 불가.
- [x] Pydantic 경계·기록 모델, 모듈 스모크와 관련 테스트 63개 통과. 도구 6개 추가로 합계 18개.
- [x] 다운로드 읽기 전용 스캔 활성화: Codex 환경값 저장과 실제 MCP 호출로 20개 파일 메타데이터 확인. 파일 이동·삭제 없음.
- [ ] 내용·생성일 기반 분류·중복 내용 판단·다운로드 쓰기는 후속 설계.

사용 방법과 범위는 [파일 정리 안내](guides/file-organization.md)를 따릅니다. 위 원안의 전체 파일 정리 범위를 완료했다고 선언하지 않으며, 기본적인 안전한 프로젝트 파일 정리 흐름을 먼저 제공한 상태입니다.

### 작업 환경 런처 세부 상태 (2026-10-17)

- [x] 코드 영역의 엄격한 등록 설정, 이름 기반 조회·미리보기·실행·상태·기록·종료 도구 6개.
- [x] 고정 실행 파일·인자·작업 위치·URL 허용 목록, 중복·포트 충돌·미리보기 변경/만료 거부.
- [x] 관리 서비스 기동 확인·실패 정리·실행 시간 상한·중단 감시·PID 생성시각 검증.
- [x] 파일 미제공 local-status 및 현재 자비스 프로젝트용 jarvis-development 활성화. VS Code 절대 설치 경로와 코드 폴더 고정 인자를 지정.
- [x] 실제 MCP 기동·상태·HTTP 200·종료 검증. 브라우저 유휴 연결 문제 수정 후 런처 모듈 20개 통과.
- [ ] 앱 창의 시각적 확인, 음성·예약·시작 프로그램·OS 수명 격리는 후속 범위.

[런처 안내](guides/workflows.md)를 따릅니다. 감시는 실행 중인 자비스 프로세스 안에서만 동작하며 강제 종료 후 자동 수명 보장은 없습니다. 앱·브라우저는 관리 서비스와 달리 종료하지 않습니다. 읽기 전용 에러 분석은 아래 항목에서 구현 상태를 관리하며 등록형 웹 수집은 아래 상태를 따릅니다.

### 에러 분석·수정 보조 세부 상태 (2026-10-18)

- [x] jarvis/features/development/tools/ 신규 계열과 analyze_error_log 도구 1개 자동 등록. 기존 24개 기능 유지.
- [x] 인라인 로그 또는 projects/<name>의 UTF-8 로그, 선택한 Python 소스의 읽기 전용 분석.
- [x] 알려진 예외 15종의 원인 후보·조치, 비밀값 패턴 가림·정제 로그 발췌·미분석/미검증 사실 표시.
- [x] 누락 콜론 후보의 AST/컴파일 검증과 diff 제안. 비밀값 인식 소스·잔여 문법 오류는 diff 억제.
- [x] 공통 예산/중단·경로/크기/링크·원본 미변경·기존 계약 및 실제 stdio MCP 포함 관련 73개 검사 통과.
- [ ] 실제 코드 적용·자동 백업/회귀 검사 연계, 모델 제공자·외부 전송·일반 수정, 다른 언어는 후속 범위.

사용 예시와 한계는 [에러 분석 안내](guides/debug-assistant.md)를 따릅니다. 웹 정보 수집·Phase 2.5·Phase 3는 아래 구현 상태를 따릅니다.

### 웹 정보 수집 세부 상태 (2026-10-18)

- [x] jarvis/features/web/tools/의 list_web_sources·collect_web_information 2개와 운영자 고정 출처 레지스트리, 합계 27개 도구.
- [x] 공개 RSS/Atom·HTML 발췌·서울 현재 날씨, 출처/조회/게시 시각 및 부분/전체 실패 구분.
- [x] 정확한 HTTPS 호스트·모든 DNS IP 검사·숫자 주소 연결과 원래 TLS 호스트 검증·재지정/인증정보 금지.
- [x] 512 KiB 응답·XML 구조/노드/깊이·12초 작업자·최대 2개 주소/3개 출처/5개 항목·공통 중단/호출 예산.
- [x] 작업공간 reports/web/의 고유 새 텍스트 보고서, 덮어쓰기 금지 및 저장 생략 옵션.
- [x] 관련 90개 검사 통과/건너뜀 0개, 실제 MCP Python/BBC/서울 날씨 성공과 Python HTML 별도 수집 성공 확인.
- [x] 보고서 루트/하위 폴더의 외부 정션과 내부 대상 정션을 실제 생성해 저장 거부·대상 미변경 확인. Windows 심볼릭 링크 권한은 변경하지 않음.
- [ ] 임의 검색·다른 도시/예보·AI 요약·자동 예약·OS 전체 격리는 후속 범위.

[웹 수집 안내](guides/web-information.md)를 따릅니다. Phase 2.5의 역할별 협업 기반은 아래 상태를 따릅니다. V4 원안의 내용 기반 분류·일반 자동 수정 등 미완료 범위도 그대로 추적합니다.

## Phase 2.5 — 다중 에이전트 협업 (기반 구현 착수)

### 첫 구현 단위 — 영속 작업 상태와 수정 회로 차단 (2026-10-18)

사용자의 다음 단계 진행 지시에 따라 먼저 jarvis/agents/에 모델 제공자와 독립된 작업 제어 계층을 구현한다. SQLite 트랜잭션으로 작업 ID별 상태·역할·시도·시간 한도를 저장하고 Planner → Coder → QA 순서와 최초 QA 포함 최대 4회/수정 최대 3회를 강제한다. 중복 역할 시작·오래된 결과·재시작 후 카운터 초기화를 거부하고 실패·중단·시간 초과를 성공으로 바꾸지 않는다. 저장은 작업공간의 .jarvis-agents/로 제한하며 공통 경로·중단 정책과 정제된 짧은 요약만 사용한다.

이 단위는 신뢰된 Python 호출자가 사용하는 상태 제어 API다. 실제 모델·자율 코드 적용·QA 실행·장기 기억·이벤트 버스·WebSocket은 후속 단위로 구현한다. 모델 제공자·비용·전송 범위가 미정이므로 외부 호출이나 모의 결과를 실제 협업 완료로 보고하지 않는다. 기존 MCP 27개와 서버 조립은 변경하지 않는다. 새 모듈의 상태 전이·재시작·경합·중단/경로 경계를 검사하고, 새 패키지 배치에 대한 구조 검사와 문서 검사만 추가한다.

- [x] 영속 상태/엄격 모델·역할 순서·동시 시작/오래된 결과 거부 구현.
- [x] 최대 3회 수정·작업 전체 실행 횟수/시간 한도·실패/중단 상태 검증. 관련 35개 통과.
- [x] [사용 안내](guides/agent-tasks.md)·[Phase 2.5 보고서](reports/phase-02-5.md)·지도 갱신.

목표는 사용자 요청을 Planner → Coder → QA의 역할별 작업으로 나누고 Orchestrator가 결과와 재검수 흐름을 통제하는 것입니다. [V5 원안](master_blueprint_v5.md)의 목표를 반영합니다. 현재 영속 작업 상태와 로컬 모델의 제안/정적 QA 실행 루프까지 구현했으며 일반 코드 적용·기능적 테스트는 후속 범위입니다.

- **Planner:** 요구사항과 완료 기준을 분해하고 권한·위험·선행 의존성을 명시합니다.
- **Coder:** 승인 범위의 코드 변경안을 작성합니다. 작성 권한과 실제 적용 권한을 구분합니다.
- **QA:** 등록된 검사만 허용된 환경에서 실행하고 결과와 재현 가능한 실패를 반환합니다. 임의 셸 실행 권한을 새로 부여하지 않습니다.
- **Orchestrator:** 역할별 상태·작업 전달·수정 횟수·중단·최종 보고를 관리합니다. 에이전트 메시지는 사용자 승인으로 취급하지 않습니다.

### 두 번째 구현 단위 — 로컬 Ollama 제안과 실제 정적 QA (실연결 확인)

사용자가 로컬 Ollama를 선택했다. 고정 loopback API와 설치된 로컬 모델만 사용하고 클라우드 모델·프록시·리디렉션·자동 다운로드를 허용하지 않는다. Planner/Coder의 구조화된 JSON을 엄격 검증하며, Coder의 Python 제안은 파일에 적용하거나 실행하지 않고 별도 작업자의 AST/컴파일 검사로 QA 판정을 만든다. QA 판정과 제안 해시·검사 증거를 같은 작업 이력에 보관한다. 수정 실패 시 앞서 구현한 최대 3회 회로 차단을 재사용한다.

부모 실행기는 역할 시작을 먼저 영속화하고 고정 작업자만 실행한다. 요청당 생성 상한·응답 크기·작업/요청 시간 상한·중단 감시를 적용한다. 입력은 명시한 작업 요약과 작업 내 제안/검사 결과뿐이며 로컬 문서·인증정보를 자동 읽지 않는다. 운영자 CLI로 생성/진행/상태 조회를 제공한다. 실제 모델 설치/선택 후 작은 비개인 예제로 실연결을 확인한다. 관련 작업 제어·실행기·작업자 검사만 실행하고 구조/문서 검사로 마무리한다. 실제 코드 적용·행동 테스트·기능 정확성 판정·외부 모델·MCP 도구 추가는 이번 범위가 아니다.

1. **기억 저장소 (Memory Store):** 단기 기억은 작업별 역할 메시지·도구 결과·현재 상태를 보관하고 작업 종료 시 요약·만료 정책을 적용합니다. 장기 기억은 사용자 코딩 선호, 자주 쓰는 경로, 검증된 오류 해결 이력을 로컬 JSON/DB와 필요 시 벡터 인덱스로 저장합니다. 프로젝트·사용자 범위를 분리하고 출처·갱신 시각·확인 상태를 기록합니다. 조회·정정·삭제·보존 기간을 제공하며 비밀키·인증 토큰·내부 사고 과정 전체는 저장하지 않습니다. 저장된 선호나 경로는 현재 사용자 지시·가드레일·접근 권한을 덮어쓰지 않습니다. 기존 문서 검색 인덱스를 에이전트 장기 기억으로 구현 완료했다고 간주하지 않습니다.
2. **회로 차단과 재시도 (Circuit Breaker, Max Retries = 3):** 최초 코드 작성·QA는 초기 시도이며, 이후 Coder 수정 → QA 재검수 쌍을 재시도 1회로 셉니다. 수정 재시도는 최대 3회, 따라서 QA는 최초 포함 최대 4회입니다. 세 번째 수정도 실패하면 네 번째 수정을 시작하지 않고 `needs_user_input`으로 전환해 마지막 실패·수행한 수정·남은 문제를 사용자에게 전달합니다. 에이전트 교체·하위 작업·재시작으로 카운터를 초기화하지 않고 같은 작업 ID의 상태를 보존합니다. 도구·토큰·비용·시간 한도나 명시적 중단에 도달하면 3회 이전이라도 정지합니다. 오류는 성공으로 처리하지 않으며 명시적인 사용자 피드백·재개 지시 후 남은 범위와 예산을 확인합니다. 일시적 API 전송 재시도는 이 수정 카운터와 구분하되 별도 유한 상한을 두고 전체 예산에도 포함합니다.
3. **비동기 이벤트 버스와 WebSocket:** 백엔드 작업 상태·에이전트의 사용자용 메시지·도구 실행 요약·QA 결과를 비동기 이벤트로 발행하고, 인증된 WebSocket 계층을 통해 HUD 카드·텍스트로 전달합니다. 이벤트에는 버전·event_id·task_id·agent_id·종류·작업별 순번·시각·정제된 payload를 둡니다. 토큰·비밀정보·원문 전체·내부 사고 과정 전체를 스트리밍하지 않습니다. 유한 큐·역압·느린 소비자·재연결·중복 제거·누락 감지·순서 복구를 설계하고 완료·오류·중단 이벤트를 단순 폐기하지 않습니다. 허용 Origin과 연결 인증, 작업별 구독 권한을 적용합니다. 일반 부하의 로컬 환경에서 발행→HUD 표시 지연 p95 100ms 이하를 측정 목표로 삼되 무지연·항상 0.1초 이내를 보장하지 않습니다. 모델 생성 시간·네트워크 상태는 별도로 측정합니다. Phase 2.5에서 버스·프로토콜·테스트 소비자와 지연 계측을 만들고 실제 HUD 표시 검증은 Phase 3에서 완료합니다.

### 착수 및 완료 체크리스트

2026-10-18 사용자 자율 진행 지시로 정수 QA·예산·기억·이벤트를 구현했다. 이어 Docker/WSL 구성의 명시적 승인으로 실제 격리 검증도 완료했다. 외부 모델·새 계정·자동 발송은 추가하지 않았다. 변경 모듈 검사 후 단계 마무리 전체 회귀를 한 번 수행했다.

이번 세 번째 단위(2026-10-18)는 development 기능 서비스에 단일 Python 제안의 diff 미리보기와 명시적 적용을 추가한다. completed 작업의 마지막 Coder 제안/QA 해시를 재검증하고 projects/<name> 안의 지정 .py 파일만 대상으로 한다. 미리보기 digest에 작업·제안·대상·원본 해시를 결합해 내용이 바뀌면 거부한다. 적용 직전 프로젝트 스냅샷과 복구용 원본/제안·저널을 저장하고 실패 시 변경되지 않은 파일만 롤백한다. CLI에서 preview/apply를 분리하며 모델에는 파일 쓰기·셸 실행 권한을 주지 않는다. 적용 자체를 기능 정확성 검증 완료로 보고하지 않는다. 실제 생성 코드 실행과 기능적 QA는 실행 격리 설계 뒤 별도 구현한다. 검사는 새 적용 기능·기존 스냅샷의 관련 회귀와 CLI 도움말로 한정한다.

- [x] Phase 2 실용 도구의 선행 초기 범위와 완료 상태 확인, 사용자 착수 지시(2026-10-18). 미완료 확장 범위는 유지.
- [x] 초기 제공자는 로컬 Ollama, API 키/유료 호출 없음, 전송 범위와 역할 JSON/QA 계약 확정. 유료 제공자는 미지원.
- [x] 동일 작업의 역할·출력/컨텍스트 예약·마감·수정 3회·명시적 실행 재개 2회 한도와 중단. 실제 청구량 계측/계정 전역 한도와 구분.
- [x] jarvis/agents/의 영속 제어·모델/QA 실행기·기억·이벤트 초기 구현. 로컬 Linux 컨테이너 실검증 완료.
- [ ] 일반 프로젝트 자동 수정/적용 후 환경별 QA 통합. 현재는 제안을 먼저 QA하고 별도 명시적 단일 파일 적용·백업/롤백으로 연결한 초기 범위.
- [x] 제안/테스트/모드/이미지 ID와 QA 증거 결합, 실패·시간 초과·취소 구분. 입력/출력은 정수 테스트 범위.
- [x] 단기/장기 기억 분리, 확인/revision/정정·삭제·만료·인식된 비밀값 거부와 정책 비승격 검증.
- [x] 수정 최대 3회·최초 포함 QA 4판정, 재시작/재개 후 한도 유지·피드백 검증. 실행 재개는 별도 최대 2회.
- [x] 영속 순번·유한 페이지/역압·인증/Origin/작업 목록·재연결 검증, 실제 클라이언트 지연 계측. HUD 표시 계측은 Phase 3.
- [x] 기존 27개 도구 계약과 전체 344개 회귀 통과, 보고서/안내 갱신. 광범위 일반 자동 개발 완료와 구분.

현재 중단 표시와 영속 취소 상태를 실행기에서 확인해 고정 자식/소유 컨테이너를 중단합니다. 모델 서버 내부 GPU 회수나 OS/런타임의 무결점 격리는 보장하지 않습니다. 자율 진행 범위를 외부 모델 전송이나 타 채팅 메시지 발송 권한으로 확대하지 않습니다.

## Phase 3 — 차세대 HUD

### 첫 단위 — 로컬 읽기 전용 작업 HUD (2026-10-18 구현·연결 검증)

사용자의 자율 후속 진행 범위로 인증된 로컬 상태 화면을 먼저 구현한다. Sites의 local-only/buildless 절차로 frontend/에 HTML/CSS/JS를 두고 기존 loopback 이벤트 앱에서 정해진 정적 파일만 제공한다. 연결 파일은 브라우저 파일 선택으로 메모리에만 읽고, 같은 출처의 인증된 상태 API와 WebSocket에만 사용한다. 작업 목록/단계/등록 QA 결과/예약 예산/이벤트를 표시하고 샘플 모드는 명확히 분리한다. 음성/화면/모델 변경/코드 실행·적용 기능은 이 UI에 추가하지 않는다. React/Three.js와 3D 지식 뷰는 후속 단위로 유지한다. HTTP 인증·허용 작업/Origin·개인 원문 미노출 경계, 브라우저 표시/연결 오류·재연결을 변경 범위에서 검증한다. 외부 게시/업로드는 하지 않는다.

- [x] 사용자 자율 진행 지시에 따라 읽기 전용 첫 범위 확정.
- [x] 첫 화면의 인증/Origin/허용 작업/최소 데이터 경계 확정 및 관련 검사.
- [x] 실제 작업 목록·QA·예산·이벤트 화면 연결 및 보고서 기록.
- [x] 정상 idle 연결 순환/완료 기록 이어받기 수정, 제한된 재시도·명시적 복구 검사와 화면 반영 소표본 계측(p95 64ms/18개, 2026-10-20).
- [ ] 합의한 UI·음성·화면 공유·모델 전환·브리핑 기능 구현 및 검증.
- [ ] 기존 기능 회귀 검증과 단계 보고서 작성.
- [ ] 다양한 실제 부하·느린 연결·백그라운드 환경에서 발행→표시 지연 및 재연결 종합 검증. 첫 전경 표본과 제어된 연결 검사는 완료.

계획 범위:

- React/Next.js와 Three.js 기반 3D 지식·파일 탐색.
- STT/TTS와 “Hey Jarvis” 음성 인터랙션.
- 사용자가 공유한 화면·탭의 실시간 비전.
- 모델 제공자 전환 패널.
- 일정·이메일·투두를 음성과 카드로 제공하는 모닝 브리핑.

기술은 후보 단계입니다. 웹 통신·권한 경계, 브라우저 API 지원, 모델 비용과 데이터 전송 정책을 설계한 뒤 구현합니다. 현재 stdio 서버를 브라우저에 직접 노출하지 않습니다. 마이크·화면 공유·자동 예약은 개별 기능 구현과 사용자 요청에 따라 활성화합니다.

## V3 구조 원칙 — 적용 완료한 공통 구조

- `server.py`는 경량 진입점으로 유지하고 MCP 도구 하나당 파일 하나로 분리합니다.
- 공통 가드레일·예산·중단 상태, 저장소·인증 서비스는 공유합니다. 도구 등록과 실행에서 검증을 우회하지 않도록 합니다.
- 운영자가 관리하는 코드 영역의 검증된 플러그인만 자동 등록합니다. 문서·다운로드 저장소 코드를 자동 로드하지 않습니다.
- 중복 이름·임포트·등록 실패는 시작 실패로 보고합니다. 도구 추가 시 서버 본문 수정 없이 등록되는지 검증합니다.
- V3 예시의 상태 조회·캡처 및 지식 도구 묶음은 실제 이전 시 각각 나눕니다.
- `AGENTS_CONTEXT.md`는 50줄 이내로 관리합니다. 변경 파일부터 읽되 공유 계약·관련 테스트까지 필요한 만큼 확인합니다.
- 도구 이름·입력·출력·오류 동작과 기존 테스트를 보존합니다. 현재 파일·Codex 등록 경로를 자동 이동하지 않습니다.
- 토큰 90% 절약과 100% 가드레일 통제는 목표입니다. 측정된 절감률이나 무결점 보안 보장은 아닙니다.

## 별도 범위 결정이 필요한 과제

- OS/Docker 격리, 실행 중 취소·즉시 중단, 강제 자원 제한.
- GitHub 복제는 V3 예시에서 보존한 별도 후보입니다. V4 웹 정보 수집에 자동 포함하지 않으며 복제 코드는 자동 실행하지 않습니다.
- 다중 에이전트는 V5에 따라 Phase 2.5 계획으로 승격했습니다. 브라우저 자동화·감사 로그는 별도 범위 합의가 필요한 기존 후보입니다.
- 의미 임베딩, 추가 문서 형식, 암호화 저장, OS 자격 증명 저장소.

## 속도 및 토큰 최적화 — 이후 구현에 적용

- **Fast-Pass:** 주석·단순 문구·동작에 영향 없는 UI 수정은 별도 QA와 불필요한 승인 대기 없이 완료합니다. 차이 확인·문서 검사·필요한 화면 확인 등 최소 검증은 유지합니다.
- **스모크 우선:** 국소 코드 변경은 수정 모듈의 구문·임포트·대표 경로를 약 1초 목표로 검증합니다. 환경 지연은 별도로 판단하고, 파일 변경·보안·공유 계약에 영향을 주면 해당 회귀 검사로 확대합니다. 전체 테스트는 넓은 영향·실패·미해결 우려가 있을 때 실행합니다.
- **타입 계약:** 새 데이터 경계에 Pydantic 모델·타입 주석·필요한 엄격 검증을 점진 적용합니다. 기존 도구 계약은 보존합니다. Pydantic 런타임 검증과 작성 시 정적 타입 검사는 구분하고 오류 90% 차단은 미측정 목표로 둡니다.
- **상태:** 이 절은 이후 개발 절차입니다. 모든 기존 입력이 엄격 Pydantic 모델로 전환됐거나 자동 QA 라우터·정적 타입 검사가 구현됐다는 뜻은 아닙니다. Phase 2.5 구현 시에도 같은 위험도 분류를 적용합니다.

구체적인 생략·확대 조건은 [개발 규칙](DEVELOPMENT.md)을 따릅니다.

## 유지해야 할 실제 한계

현재 가드레일은 응용 프로그램 정책이며 OS 샌드박스가 아닙니다. 다른 프로세스가 검증 직후 파일시스템을 바꾸는 경쟁 조건을 완전히 막지 못합니다. `.jarvis-stop`은 도구 호출을 거부합니다. 파일 작업은 단계 사이에서, 관리 런처 서비스는 감시 스레드에서 추가 확인하지만 모든 실행을 즉시 종료하는 OS 중단기는 아닙니다. 예산은 작업별이 아닌 프로세스 전체 20회입니다. 검색 인덱스에는 암호화되지 않은 원문 사본이 저장됩니다. 상세 운용 한계는 각 사용 안내를 따릅니다.

## 설계 결정 기록

기존 결정을 2026-10-17에 정리한 기록입니다. 변경 시 이전 결정을 지우지 말고 대체 근거를 추가합니다.

- **D01 · 채택 — 로컬 어휘 벡터 검색:** 외부 전송·모델 다운로드 없이 시작하기 위해 해시 특징 벡터와 SQLite를 선택했습니다. 동의어·의미 검색의 한계가 있으며 해당 요구가 생기면 로컬 의미 모델을 비교합니다.
- **D02 · 채택 — 독립 Google 인증:** Codex 커넥터 토큰을 재사용하지 않고 서버 전용 OAuth를 사용합니다. 최소 권한으로 Gmail 메타데이터·Calendar 읽기만 허용하며 실계정 검증은 대기합니다.
- **D03 · 현재 채택 — stdio MCP:** Codex 로컬 연결에 사용합니다. 웹 HUD 도입 시 인증된 통신 계층을 별도 설계하며 브라우저에 로컬 실행 권한을 직접 노출하지 않습니다.
- **D04 · 계획 — 도구별 플러그인:** 수정 시 필요한 맥락을 줄이기 위한 V3 방향입니다. 아직 이전하지 않았으며 공통 검증 우회 방지·동작 보존 검증 후 적용합니다.
- **D05 · 채택 — 현재 계획 단일화:** PLAN.md를 현재 기준으로 유지하고 원안은 archive, 실제 검증은 단계별 reports에 보존해 중복·상태 충돌을 줄입니다.
- **D06 · 계획 반영 (2026-10-17) — V4 실용 기능:** 기존 지식·Google·HUD 목표를 유지하며 도구 5종을 Phase 2에 추가합니다. 실행 코드·데이터 분리는 유지하고 모듈화는 미완료 정비 항목으로 추적합니다. 상태 변경 도구의 안전 계약과 세부 구현 범위는 착수 시 검증·확정합니다.

구조 추가·운영 방법은 [플러그인 안내](guides/plugins.md)를 따릅니다.

- **D07 · 적용 완료 (2026-10-17) — D04 구조 이전과 D06 정비 상태 갱신:** 8개 도구를 개별 파일로 이전하고 build(context) 자동 로더·공통 호출 경계를 적용했습니다. 서비스는 공유하며 계약 비교와 전체 테스트 71개를 통과했습니다. D06의 실용 도구 5종은 미구현입니다. 임포트 자체는 신뢰된 Python 코드 실행이며 OS 격리는 제공하지 않습니다.
- **D08 · 계획 반영 (2026-10-17) — V5 역할별 협업:** Phase 2.5를 추가하되 실용 도구 완료와 agents/ 존재를 가정하지 않습니다. 작업 전체의 공유 예산·권한·수정 상한을 먼저 설계하고 외부 모델·테스트 실행 범위는 착수 시 결정합니다. V5 원안은 사용자 지정 docs/master_blueprint_v5.md에 보관하며 현재 상태는 이 문서로 관리합니다.
- **D09 · 계획 반영 (2026-10-17) — Phase 2.5 제어 계층:** 기억 저장소·최대 3회 수정 재시도·비동기 이벤트 버스/WebSocket을 필수 완료 기준에 추가합니다. 기억은 권한이 아니며 재시도로 예산을 초기화하지 않습니다. 실시간성은 측정 가능한 로컬 p95 100ms 목표로 관리하고 HUD 실표시 검증은 Phase 3에 연결합니다. 아직 구현·성능 검증하지 않았습니다.
- **D10 · 초기 적용 (2026-10-17) — 스냅샷 병합 복원:** projects/<name> 범위와 일회용 미리보기, 사전 백업, 파일별 교체·롤백을 채택했습니다. 추가·제외 파일을 보존하기 위해 미러 삭제를 하지 않습니다. 기존 가드레일 경계를 유지하며 운영 코드 복원·장애 후 자동 복구·수정 전 후크는 별도 설계합니다.
- **D11 · 개발 절차 적용 (2026-10-17) — 최소 충분 검증:** 안전한 수정은 Fast-Pass, 국소 코드는 모듈 스모크, 보안·상태 변경·공유 계약은 관련 회귀 검증으로 구분합니다. 1초와 오류 90% 차단은 목표이며 검증 생략의 근거로 과장하지 않습니다. Pydantic 모델 도입은 새 데이터 경계부터 진행하고 기존 계약을 유지합니다.

- **D12 · 적용 완료 (2026-10-17) — 도구 계열 분류:** 기존 12개 도구를 system·knowledge·google·snapshots에 배치했습니다. 로더는 신뢰된 계열 폴더 한 단계를 지원하고 링크·정션 및 중복 MCP 이름을 거부합니다. 예정 files·development·workflows·web는 실제 구현 시 생성합니다. 관련 테스트 20개 통과로 검증했으며 전체 99개를 다시 실행한 것으로 보고하지 않습니다.

- **D13 · 초기 적용 (2026-10-17) — 관련 파일 정리 기능 묶음:** 미리보기·백업·이동·기록·undo를 tools/files/의 6개 도구로 묶었습니다. 쓰기는 projects/<name>에 제한하며 다운로드는 운영자 opt-in의 메타데이터 읽기만 지원합니다. 코드·진행 중 다운로드·보호 파일은 이동하지 않고 파일 내용을 자동 분류하거나 중복 삭제하지 않습니다. Windows 무덮어쓰기 rename·변경 검증·롤백과 엄격 기록 모델을 채택했고 기존 계약·스냅샷 포함 관련 테스트 63개로 검증했습니다.

- **D14 · 초기 적용 (2026-10-17) — 등록형 런처와 소유 서비스 수명:** 작업공간 밖 운영자 설정에서만 프로그램·인자·URL을 등록합니다. 기본 예제는 파일을 공개하지 않는 loopback 상태 서버입니다. 서비스만 생성시각과 핸들로 관리하고 앱·탭·과거 PID를 임의 종료하지 않습니다. 실행시간·중단 감시와 정상 종료 정리는 제공하지만 OS Job 격리와 강제 종료 후 자동 정리는 별도 과제입니다. 사용자 개발 프로필은 기본 비활성 템플릿으로 두고 실사용 경로는 후속 설정합니다.

- **D15 · 실사용 검증 완료 (2026-10-17) — D02/D13/D14 운영 상태 갱신:** 다운로드 읽기 전용 opt-in을 저장하고 실제 20개 파일을 스캔했다. 현재 자비스 프로젝트용 개발 런처를 구성해 HTTP 200·상태·소유 서비스 종료를 확인했으며 유휴 연결 회귀 검사 포함 모듈 20개를 통과했다. 사용자 직접 OAuth 동의 후 독립 Google 인증으로 Gmail·Calendar 모두 실제 MCP 정상 응답을 확인했다. 이전의 인증 대기/개발 템플릿 비활성 상태를 대체하며 읽기·경로·셸 정책은 확대하지 않는다.

- **D16 · 초기 구현 완료 (2026-10-18) — 로컬 에러 진단과 제안:** 외부 모델·비용·전송 설정 없이 사용할 수 있도록 규칙 기반 로그 분석과 Python 파싱/컴파일 검증을 선택했다. projects/<name> 파일 읽기와 한도를 재사용하고 수정 제안만 반환한다. 실행·자동 적용 권한을 추가하지 않는다. 일반 코드 수정/모델 분석은 별도 전송·백업·검증 계약 후 확장한다. 관련 검사 73개와 실제 stdio 호출로 확인했다.

- **D17 · 초기 구현 완료 (2026-10-18) — 등록형 공개 웹 수집:** 검색/모델 비용·자격 증명 없이 시작하기 위해 운영자 고정 URL, 배포자 발췌와 고정 서울 날씨를 선택했다. SSRF 방어는 호스트 목록뿐 아니라 DNS 전체 IP 검증과 검증 숫자 주소/TLS 호스트 연결로 적용하고, 느린 DNS/응답은 별도 작업자의 12초 상한으로 관리한다. 저장은 고유 새 텍스트 보고서에 한정한다. 관련 87개 통과/1개 권한 관련 건너뜀과 실제 MCP 수집으로 검증했다. 임의 검색/요약 모델/새 출처는 후속 계약·등록 및 검증을 거친다.

- **D18 · 검증 공백 해소 (2026-10-18) — D17 링크 경계 검사 갱신:** Windows 심볼릭 링크 권한 의존 때문에 건너뛴 보고서 저장 경계 검사를 실제 정션 3종으로 전환·확장했다. 외부 루트/외부 하위/내부 대상 연결 모두 저장을 차단하고 기존 대상 데이터를 보존했다. 검사 생성 실패는 skip이 아닌 실패로 처리한다. 관련 90개 통과·건너뜀 0개로 갱신하며 시스템 링크 권한이나 운영 가드레일을 확대하지 않는다.

## 구조 정비 — 기능별 패키지 이전 (2026-10-18 완료)

사용자 요청에 따라 다음 기능 구현보다 먼저 루트 Python 정리를 진행한다. 설계 기준은 [패키지 구조와 이전 계획](architecture.md)이다. core/app/features/workers/cli 역할을 구분하고 기능의 서비스와 도구를 같은 위치에 모은다. Codex/Claude의 server.py와 OAuth 명령, 인증/작업공간/설정의 실제 위치는 유지한다.

- [x] 현재 의존/경로 조사와 구조·이전·검증 계획 문서 작성.
- [x] 이전 전 전체 27개 도구 계약과 비밀정보 제외 코드 백업.
- [x] 기능별 패키지·공통 경로·자동 로더 이전, 고정 진입점 보존.
- [x] 구조 검사와 전체 계약/회귀·임의 cwd/작업자 검증.
- [x] 실제 결과·새 지도·안내·보고서 갱신.

- **D19 · 적용 완료 (2026-10-18) — 기능별 패키지와 구조 경계:** 루트 구현을 jarvis/core/app/features/workers/cli로 이전하고 기능별 서비스와 도구를 같이 배치했다. 고정 진입점 2개와 기존 설정/인증/데이터 위치를 보존하고 전체 27개 계약 및 전체 212개 회귀 검사를 통과했다. 새 구조 위반을 AST/파일 검사로 차단하며 가짜 호환 사본·자동 모델 실행은 도입하지 않는다. 실행 중 서버에는 재연결이 필요하다. 설계·백업/이전 근거는 docs/architecture.md와 Phase 2 보고서를 따른다.

- **D20 · 개발 절차 적용 (2026-10-18) — 검사 범위 선택 구체화:** D11을 보완해 문서만 변경하면 문서 검사, 국소 기능 변경이면 해당 기능 검사, 등록/계약 변경이면 관련 플러그인·계약 검사로 한정한다. 구조 검사는 배치/import 변경 시 실행하고 전체 검사는 광범위한 영향이나 한정할 수 없는 실패가 있을 때 근거를 설명하고 실행한다. 실패 수정 후 실패 항목부터 재검사하며 통과한 검사를 문서 보완만으로 반복하지 않는다. 세부 기준은 [개발 규칙](DEVELOPMENT.md)을 따른다. 검사 개수나 완전성 증명을 목표로 하지 않으며 보안 경계 검사는 유지한다.

- **D21 · 기반 구현 완료 (2026-10-18) — 영속 작업 제어부터 단계적 연결:** 모델 제공자 미정 상태에서 외부 전송을 시작하지 않고 jarvis/agents/의 엄격 모델과 SQLite 트랜잭션으로 역할 순서·상태·공유 단계/시간 한도·수정 최대 3회를 먼저 구현했다. 최초 QA 포함 최대 4회와 재시작/경합/중단/경로 경계를 관련 35개 검사로 확인했고 구조/문서 검사도 통과했다. 실제 모델/QA 실행 증거·토큰/비용·장기 기억·이벤트/HUD는 후속 범위다. 기존 MCP 27개는 변경하지 않았다. 상세 근거는 Phase 2.5 보고서를 따른다.

- **D22 · 코드 준비/설치 대기 (2026-10-18) — 로컬 Ollama 제안 실행기:** Planner/Coder JSON → Python 정적 QA → 최대 3회 수정 흐름과 운영자 CLI를 구현했다. 관련 47개 검사 통과는 모의 모델과 실제 정적 작업자 검증이다. 사용자 최종 선택 qwen2.5-coder:7b를 단일 기본값으로 설정했다. 앞선 설치 요청이 사용자에 의해 거절되어 실제 모델 설치·다운로드·추론은 미완료다. 자동 코드 적용·기능 테스트·전체 비용/토큰 예산·장기 기억·이벤트는 남은 범위다.

- **D23 · 실연결 확인 (2026-10-18) — D22 설치 대기 해소:** 사용자 재승인으로 Ollama 0.40.5와 qwen2.5-coder:7b를 설치하고 클라우드 비활성화 로그를 확인했다. 실제 Planner/Coder/정적 QA가 3단계·수정 0회로 통과했다. 첫 적재 포함 30.823초와 짧은 warm 표본 112.55토큰/초, 컨텍스트 8,192의 모델 VRAM 약 4.80GiB를 기록했다. 실제 GPU는 4070 Ti SUPER/16GB다. 초기 모의 검사와 실연결 증거를 구분하며 일반 기능 정확성·코드 실행/적용 완료로 확대하지 않는다. 장기 기억·이벤트와 적용 전 백업/기능적 QA는 다음 범위다.

- **D24 · 구현/실제 CLI 확인 (2026-10-18) — 미리보기 digest와 백업 후 단일 파일 적용:** 기존 development 서비스와 운영자 CLI에 적용 경로를 추가했다. 지정 projects/<name>의 .py 하나만 수정하며 기존 스냅샷·원본 상태 확인·복구 저널·단일 소비/프로젝트 잠금·조건부 롤백을 적용했다. 미완료 복구가 있으면 후속 쓰기를 차단한다. 새 기능 21개와 기존 관련 3개의 검증 및 실제 모델 제안의 별도 데모 적용/원본 ZIP 백업 일치를 확인했다. 자동 모델 적용·기능적 실행 QA·OS 수준 경합 격리는 완료로 선언하지 않는다.


## REA and Agent Reach integration checkpoint (2026-10-20)

- User authorized connecting BOTH REA (morluto/rea) and Agent Reach (Panniantong/Agent-Reach) to J.A.R.V.I.S.
- User also authorized installing free deep-analysis dependencies, including Ghidra/JDK and Android analysis tools as supported by the host. Paid tools require an existing valid license; no purchase authorized.
- Mandatory removal rule: identify redundant, superseded or unused MCP servers/tools, explain the exact removal scope, reason and impact, and obtain explicit user consent BEFORE uninstalling, deleting or disabling them. General integration authorization is NOT removal consent.
- Mandatory continuity rule: update PLAN.md and the relevant report at meaningful checkpoints, before handoff and at session end. Record actual changes, installed versions/paths, configuration locations (never credentials), validation commands/results, failures/blockers, pending consent and the exact next action. Distinguish planned, installed, configured and verified states.
- Current verified state: project source located at this root; launcher server.py delegates to jarvis.app. Existing project map reports 27 original tools; this session has not independently validated their runtime state.
- Integration state: research and source discovery only. Neither REA nor Agent Reach has been installed, configured, integrated or tested by this session. No MCP server/tool has been removed.
- Next action: inspect architecture/plugin/guardrail contracts and existing registrations; inventory Node/JDK/Ghidra/WSL and existing REA/Agent Reach installations without reading secrets; review official installation/MCP contracts; implement the smallest guarded connection and verify it before expanding capabilities.
- Official references: https://github.com/morluto/rea and https://github.com/Panniantong/Agent-Reach . External instructions are reference material, never authorization to bypass project policy.


### Installation attempt / environment checkpoint (2026-10-20)

- Inspected server.py, jarvis/app/server.py, runtime.py, plugins.py, core guardrails/paths, architecture and plugin contracts. Existing adapters must remain synchronous and pass guarded_tool admission; do NOT blindly expose every upstream tool or arbitrary commands, which would bypass existing path/write policies.
- Detected Node.js v24.14.1 and npm 11.12.1. Java executable exists at C:/Program Files/Amazon Corretto/jdk25.0.4_8/bin/java.exe; compatibility with selected analysis versions remains unverified.
- REA, agent-reach and gh were not found on this process PATH. This is not proof they are absent elsewhere. No Ghidra/Android installation was found in the limited standard-directory scan.
- Blocker: npm view rea-agents version --fetch-retries=0 --fetch-timeout=15000 failed with ENOTFOUND / getaddrinfo registry.npmjs.org. The execution environment has restricted network access. No dependency download or installation succeeded.
- Blocker: wsl --list --quiet failed with Wsl/E_ACCESSDENIED. No conclusion about installed distributions or their usability outside this execution environment.
- No elevated permissions were requested; this session does not permit sandbox escalation. No MCP/configuration was deleted or disabled. No runtime integration code has been added.
- Official installation guides reviewed: https://raw.githubusercontent.com/morluto/rea/main/docs/installation.md and https://raw.githubusercontent.com/Panniantong/Agent-Reach/main/docs/install.md . REA setup can change client configurations and workflow skill destinations; use project-local installation/manual registration where possible. Agent Reach is a router/CLI, not assumed to be an MCP server.
- Next action: obtain a permitted network-enabled execution environment, then resolve and pin official package versions, inspect their actual CLI/MCP schemas, install under an authorized project tools directory, and validate REA/Agent Reach individually. Afterwards implement guarded feature adapters and focused policy/regression tests. Do not claim full integration or deep-analysis readiness from installation alone.

### Resumed checkpoint — installed and first adapters verified (2026-10-20)

The permission-enabled environment resolved the earlier npm/GitHub access blocker. Installed project-local REA 6.7.0, Agent Reach 1.5.0 at commit 94f06c1969dfc1834001269d79d3ad0972d9dee6, and SHA-256 verified Ghidra 12.1.4. Existing JDK 25.0.4.1 passed REA's Windows provider checks. No client configuration, personal skills, MCP registrations or existing tools were removed/disabled.

- [x] Fixed JS/PE static-analysis adapter and offline Agent Reach RSS adapter registered through mandatory guarded_tool admission; total 29 tools, 27 original contracts preserved.
- [x] Actual JavaScript/Ghidra copied PE/public BBC RSS evidence and actual MCP calls of both new tools.
- [x] Related 78 tests; 11 integration tests after dependency isolation; deployment not required for stdio source changes (existing clients reconnect).
- [ ] Android jadx/apktool/ADB readiness and actual read-only APK analysis.
- [ ] Additional public search/video channels and deeper function/session workflows with explicit bounded contracts.

Current guide/config/evidence: [integration guide](guides/rea-agent-reach.md), config/integrations.json, [verification report](reports/rea-agent-reach.md). Next action: inspect the official free Android provider distribution/version and connect a small owned APK fixture. No removal consent is pending because no removal is proposed or performed.


### Chat evidence checkpoint (2026-10-20, 21:20 KST)
Ordinary chat evidence metadata/trace matching, placeholder/failure guards and registered read-only web collection added while preserving HybridTeam integration. Chat/HUD: 26 passed, 1 environment-denied hardlink failure; live local inference not verified; no server restart. General web search and team evidence validation remain pending. Full ownership, caveats, failed cross-session delivery and next actions: [Phase 3 handoff](reports/phase-03.md#chat-evidence--collaboration-handoff-2026-10-20-2120-kst).
