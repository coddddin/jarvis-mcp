# Phase 3 구현·검증 기록

## 2026-10-21 — 조건부 설계 검토를 반영한 실행 관찰 첫 단위

- Claude의 설계 동의 조건에 맞춰 실행 ID·토큰 해시·세대·순번·수신 시각·영속 이력을 보존하고, 보고와 프로세스 관찰을 분리했다. 지연된 terminal 보고가 이후 heartbeat 뒤로 가려지는 문제는 독립 검토 재현 후 수정했다. 외부 협업 작업자에는 주입된 서버 singleton으로 등록·10초 생존 신호·결과/완료/실패 보고를 연결했다. 실제 검증 통과는 생성하지 않는다.
- HUD에 게시판 담당/메시지/기록과 등록 실행 관찰을 추가하고, 조회용 인증과 실행별 수신 토큰을 구분했다. 고정 Host/Origin·쿼리 거부·1KB·실행 ID 검사와 비밀값 미노출을 검사했다.
- 최신 승인 main을 검토 브랜치에 합친 뒤 전체 779개 통과(38.33초), 기존 Starlette 경고 1개. 수정 전 선별 통합 138개 통과. 이후 OpenCode 검토로 작업자 정리의 무제한 pipe 대기를 2초로 제한했고 해당 변경 영향 검사 62개가 통과했다. 전체 779개는 이 마지막 정리 수정 전 결과이며, 수정 후 전체 검사를 반복한 것으로 주장하지 않는다. ego-lite에서 실제 게시판 갱신·직원 5명·완료 작업 제외·수습/후보 표시·펼친 기록 유지·접힘 중 조회 0회·연결 해제 시 제거를 확인했다. 포인터 클릭 hit-test 오류 뒤 키보드 조작으로 검증했다. 스크린샷은 CDP timeout으로 이미지 검증을 완료하지 못했다.
- 모델 호출 없는 실제 45초 시험 자식을 등록해 생존 신호→자연 종료→완료 자기 보고를 화면까지 확인했고, 검증 근거 없음이 유지됐다. 임시 검증은 8790/8792, 지속 검토 화면은 8791이다. 원래 8788 서버와 main은 이 검토 변경으로 갱신하지 않았으며 main 병합은 사용자 승인 대기다.
- 아직 전체 데스크톱 직원 내부 상태나 일반 CLI·컨테이너·임의 pytest를 자동 추적하지 않는다. 자동 재시도·재배정·종료와 검증 결과 어댑터는 제외했다. 등록/이벤트 보존 상한 도달 시 자동 삭제 없이 거부한다.


## 2026-10-21 — 외부 작업 콘솔 관찰 준비

- 요청 ID에 결합된 고정 `worker-progress.jsonl`을 읽는 worker_view CLI와 Windows 별도 콘솔 실행 callback을 추가했다. 모델 호출·실패 재실행 기능은 없다. 경로 ID, 링크·용량·행 제한을 검사하고 비밀값과 터미널 제어문자를 제거한다.
- viewer·기존 채팅 회귀 51개, 생산자·viewer 통합 14개 통과, 구조·문서 검사 통과. 모델 호출 없는 모의 검증이다. 생산자는 8f6aa7a의 고정 상태 로그와 callback을 연결했다. 처음 통합 검사 중 하드링크 생성이 샌드박스에서 거부됐고 호스트 재검사에서 통과했다. 실제 계정 응답·기존8788 서버 반영은 별도 진행 중이다. 사용자 지정03:10 KST 이전 Claude 실제 호출은 하지 않았다. 토큰 단위 스트리밍이나 Claude 대화형 TUI 구현으로 간주하지 않는다.

## 2026-10-18 — 로컬 읽기 전용 작업 HUD

사용자의 자율 후속 진행 지시에 따라 Phase 2.5의 인증 이벤트를 실제 화면에 연결했다. 음성/3D 전체를 한 번에 추가하지 않고 기존 상태·QA 증거·예약 예산을 보여주는 첫 단위를 구현했다. 서버 데이터가 그대로 보이고 인증 범위를 검증하기 쉬운 buildless HTML/CSS/JS를 선택했으며 외부 게시나 클라우드 연결은 하지 않았다.

### 변경

- `frontend/index.html`, `hud.css`, `hud.js`: 한국어 연결/샘플 화면, 작업 목록, 단계·QA·예산 카드, 상태 이벤트 기록. 문자열은 DOM textContent로 삽입한다.
- `jarvis/app/events.py`: 정해진 정적 파일 3개와 인증된 GET 상태 API 추가. 원문 코드/기억/피드백은 제외한다. 기존 WebSocket 인증·Origin·구독 범위를 유지한다.
- `jarvis/agents/store.py`: 명시적 TaskNotFound 분류와 검증된 최근 작업 조회 추가. 손상 상태나 킬스위치를 삭제된 작업처럼 숨기지 않는다.
- `jarvis/cli/hud.py`, `scripts/start-hud.cmd`: 루프백 전용 실행기. 시작 시 최근 8개 또는 지정 ID 최대 32개로 범위를 고정하고, 바인딩 성공 후 브라우저를 연다.
- 새로고침/연결 해제, 브라우저 재로드 후 인증 초기화, 이벤트 순번·중복·유한 재연결을 적용했다. 비동기 조회 중 연결을 해제하면 늦게 도착한 응답이 작업을 다시 표시하지 않도록 확인한다.

### 검증

- HUD 인증/최소 데이터/허용 작업/삭제와 stop 구분/정적 경로 제한 및 기존 작업·기억·이벤트 관련 **55개 통과**(1.58초).
- 이후 실행기의 브라우저 열기 시점 검사를 추가해 HUD **6개 통과**(0.45초). 합계로 새로운 전체 회귀 검사 결과를 주장하지 않는다. Phase 2.5 전체 344개는 이전 체크포인트다.
- JavaScript 구문 검사 통과. 기존 테스트 클라이언트의 httpx 폐기 예고 경고 1개는 남아 있으며 실행 실패가 아니다.
- 실제 HTTP 루트 응답 200. 내장 브라우저에서 샘플 3개와 실제 저장 작업 4개를 연결 파일 선택으로 각각 확인했다.
- 실제 컨테이너 QA 작업 `0586787602ff4b72903a5e33d1f89645`를 선택해 4/4 통과, 예약 예산 18,432/65,536, 순서대로 기록된 이벤트가 보임을 확인했다. 취소된 작업도 취소로 표시했다.
- 브라우저 재로드 후 연결 전 화면으로 돌아와 파일을 다시 선택해야 하는 것을 확인했다. 연결 파일 내용은 출력하지 않았다.

### 남은 범위

이 단위는 작업 실행·코드 변경 UI를 제공하지 않는다. 3D 지식·음성·화면 공유·모델 전환·브리핑과 표시 지연 p95·느린 연결의 종합 계측은 미완료다. 새로운 작업 ID는 화면 서버 재시작 후 허용 목록에 반영한다. 설치된 Ollama/Docker 서비스와 화면 서버는 별개이며 검증용 화면 서버는 확인 후 종료한다.

사용법은 [로컬 HUD 안내](../../guides/local-hud.md)를 따른다.

## 2026-10-20 — 자비스 전용 채팅 추가

사용자의 전용 챗봇 필수 요청과 mvschwarz/openrig 참고 지시에 따라 기존 작업 HUD에 대화 탭을 추가했다. OpenRig commit 0b1d8b3dc1b418f1568e571b1df174041fed77f2의 대화 저장·요청 경계·실행 주체 분리를 참고하되 TypeScript 코드는 복사하지 않고 Python으로 구현했다. OpenRig daemon/팀 운영 자체를 설치·실행한 것은 아니다. LICENSE는 로컬 참고본에 유지한다.

- agents/chat.py: 경로 검증·SQLite 로컬 대화/요청/예약 예산. 중복 request ID 재실행 차단, 미완료 상태 명시.
- app/chat.py + workers/chat_step.py: 고정 로컬 모델, 3회 모델/2회 도구 상한, 기존 guarded_tool을 통한 5개 읽기 도구. 모델 출력은 권한 승인이 아니다.
- app/events.py: 선택적 채팅 API, Bearer/정확한 Origin/Host·유한 본문 검사. 기존 작업 조회와 WebSocket 계약 유지. 저장 작업이 없어도 채팅 가능.
- cli/hud.py + frontend/chat.js/index.html/hud.css: 채팅·이전 대화·새 대화·실패·도구 증거와 기존 작업 탭. HTML/모델 텍스트는 textContent로 표시한다.
- 인증/저장/멱등/중단/예산/도구 제한/실패와 기존 HUD·플러그인·구조 관련 **44개 통과**(4.12초). Starlette TestClient/httpx 폐기 경고 1개. 전체 프로젝트 회귀 검사 결과는 아니다.
- 실제 실행에서 Python -I가 스크립트 경로도 임포트 검색에서 제외하는 문제를 확인했다. 고정된 신뢰 작업자 agent_step.py만 경로로 로딩해 기존 로컬 모델 검증을 공유하도록 수정했다.
- 실제 로컬 모델 한국어 인사, 앞서 말한 프로젝트 이름 “달빛” 기억, get_system_status 호출 후 CPU·메모리·디스크 응답을 확인했다. artifacts/chat-live-verification.json에 시험 입력·응답·도구명/상태를 저장했다. 사용자 문서나 계정 데이터는 모델 시험에 넣지 않았다.
- 실제 브라우저에서 연결 파일 선택·인증·대화 생성·전송·응답 표시를 확인했다. 연결 파일 내용은 출력하지 않았다.
- 분리 작업자 실검증 후 REA/Agent Reach 관련 검사까지 **57개 통과**(4.27초). 이후 실시간 외부 정보에 조회 근거를 요구하는 보완을 추가해 채팅 관련 **13개 통과**(1.35초). 각 수치는 실행한 묶음이며 전체 검사 개수로 합산하지 않는다.
- 실제 화면에서 지원되지 않는 실시간 순위를 예시 값으로 답한 것을 확인해 조회 증거가 없으면 미확인으로 답하도록 보완했다. 보완 후 실제 모델 시험에서도 미확인 안내를 확인했다. 기능 소개를 콜론에서 끝내는 작은 모델의 응답도 확인해 실제 활성 도구 목록을 사용해 완성하도록 보완했다. artifacts/chat-grounding-verification.json은 보완 전 기능 소개/보완 후 최신 정보 안내 시험 원기록이다.
- 문서/구조 검사와 JavaScript 구문 검사 통과. 시험용 로컬 모델 서버와 8788 채팅 화면을 시작했다. 기존 사용자 서버를 종료하지 않았으며, 직접 시작한 화면 서버만 업데이트를 위해 재시작했다. 대화 DB는 유지했다.
- 기능 소개 완성 보완 후 최종 채팅 검사 **14개 통과**(1.40초). 보완 전 대화는 과거 기록으로 보존하고 이후 새 답변부터 최신 로직을 적용한다.

현재는 한 로컬 사용자의 대화와 제한된 읽기 도구다. 토큰별 응답 스트리밍, 채팅에서 개발 작업 실행/적용, OpenRig 팀 운영, 원격/다중 사용자 인증은 미구현이다. 문서 삭제나 기존 MCP 제거는 수행하지 않았다. [사용 안내](../../guides/chat.md)를 따른다.

## 2026-10-20 — 장시간 갱신·이어받기 보완

- 정상적인 서버의 30초 idle 종료도 실패 횟수로 누적하던 로직을 수정했다. 정상 순환은 계속 연결하며 실제 비정상 종료만 최대 3회 재시도한다.
- 완료 상태 스냅샷의 기록을 중간까지만 받다 끊겨도 최종 순번까지 재개한다. 기존에는 스냅샷이 완료라는 이유만으로 이어받기를 멈출 수 있었다.
- 기록 순서/형식 오류·인증 거부는 자동 반복을 멈춘다. 수동 새로고침은 현재 스냅샷의 순번부터 이후 변경을 구독하며, 과거 기록 건너뜀을 안내한다.
- 전송 제어를 frontend/task-stream.js로 분리하고 bounded 재연결·순서·늦은 콜백·정상 idle·불완전 완료 재생·표본 제외에 대한 Node 검사 **8개 통과**. 관련 HTTP/인증/기억/이벤트 Python **26개 통과**(1.25초). 전체 회귀 검사를 다시 수행한 것으로 보고하지 않는다.
- 실제 브라우저에서 타이머 함수를 객체 메서드로 호출할 때의 Illegal invocation을 발견해 올바른 호출 래퍼로 수정했다. 수정 후 재인증·실시간 갱신을 확인했다.
- 사용자 작업과 분리한 합성 상태 시험에서 새 이벤트 18개(순번 2~19)가 실제 화면에 반영됐다. **전경 p95 64ms**, 이벤트 발행 시각→DOM 갱신 후 두 번째 프레임 기준. 모델 추론·실제 QA 기능 시험이 아니며 일반 부하/백그라운드 보장이 아니다. 상태 제목에도 합성 시험/모델 미호출을 명시했다.
- 증거: Git 제외 artifacts/hud/live-hud-2026-10-20.json 및 PNG. 광범위한 느린 네트워크·실제 장기 부하 계측은 후속 범위다. 시험용 서버만 종료하며 운영자 서비스는 임의 종료하지 않는다.


## Chat evidence / collaboration handoff (2026-10-20, 21:20 KST)

Owner: chat-improvement session. OpenRig session owns agents/team.py, team_routing.py and test_hybrid_team.py. This checkpoint is additive; do not overwrite concurrent collaboration changes.

Changes made:
- jarvis/app/chat.py: preserved HybridTeam import/construction, metadata.team, generic _infer -> dict, _worker wrapper, send(mode) and team/team-local branch.
- ChatDecision now includes basis (general/external/live/tool) and evidence_ids. Actual inference schema requires both, and _worker refuses missing metadata. Defaults remain for older in-process callers/test doubles only.
- Added _verified_reply: declared external/live/tool evidence must match successful tool traces in THIS request; source-list calls cannot substantiate external/live answers. Failed-only calls cannot produce a success answer. Bracketed search-term placeholders are rejected independently of the request keyword trigger.
- Successful traces contain server-generated evidence_id and UTC retrieved_at. These fields are persisted via existing tools JSON, without storage migration. Frontend rendering of the new fields is NOT implemented; existing detail rendering remains unchanged.
- Enabled existing collect_web_information in chat, scoped to one registered source with max_items=3 and save_report=False. Error/partial results are rejected. This enables registered documentation/weather/feed reads; it is NOT general web search.
- workers/chat_step.py, agents/chat.py, events, HUD and collaboration engine were NOT edited by this session. No tools were removed or disabled. Server was NOT restarted.

Validation:
- Latest chat + HUD run: 26 passed, 1 failed. Failure is test_chat_storage_rejects_linked_database_siblings: os.link fails with WinError 5 before the policy assertion in this execution environment. Test was not weakened or skipped. Command: .venv/Scripts/python.exe -m pytest tests/test_chat.py tests/test_hud.py -q -p no:cacheprovider --basetemp=artifacts/test-chat-evidence-2120 --tb=short .
- Earlier broader plugin run also encountered environment-denied hardlink/junction creation. New capability-label KeyError discovered during that run was fixed and chat/HUD rerun.
- scripts/check_architecture.py passed.
- Actual local _infer probe failed with ChatError (local model unavailable or invalid response); actual model behavior, retrieval and browser end-to-end behavior remain UNVERIFIED. Do not report mock tests as live success.

Important remaining limitations:
- basis is model-declared. A current-fact request mislabeled general can evade structured evidence checks; existing keyword guard remains as fallback, not a guarantee. Matching a trace ID proves a call occurred, NOT semantic entailment, relevance or truth of every claim.
- Team-mode replies bypass ordinary chat evidence validation by design of the existing branch. Collaboration owner must define separate evidence semantics before claiming grounded research in team mode.
- No general web search, arbitrary URL retrieval or additional REA deep workflows were added. Tool budget and two-call chat loop remain unchanged.
- Sources are still visible inside tool detail JSON; explicit clickable citation/time UI remains a coordinated frontend task.

Next steps:
1. Collaboration owner should run their dedicated tests and the chat/HUD checks in a host environment supporting hardlinks, then verify the updated actual inference JSON schema with the local model.
2. Coordinate registered retrieval and evidence display in HUD, and decide how request intent requiring external evidence is classified independently of final answer generation.
3. Add a bounded real search backend as a separate reviewed tool, then validate multiple phrasings and actual failed searches. Never label RSS-only integration as full Agent Reach.
4. Restart the server only after both owners finish checks; verify normal chat and team-local separately.

Communication: user explicitly authorized messaging collaboration thread 01a15910-39df-7791-a19e-4de9cf2f0bd4. send_message_to_thread and list_threads both returned Codex app tool request failed; no direct delivery succeeded. This shared report is the fallback handoff, not proof of receipt.

## 2026-10-20 — 채팅 자동 진행과 협업 화면 검증

- 실제 파일 재확인 후 협업 담당의 수정/재검수 루프 추가 인계를 반영했다. team 엔진/전용 테스트는 이 세션에서 수정하지 않았다.
- app/chat.py: 최종 basis=general과 독립적인 요청 문구 검사를 추가했다. 검색·날씨·뉴스 등은 성공한 외부 조회 근거 ID를 요구한다. 의미적 관련성/정확성 보장은 아니며 모든 우회 표현을 탐지하지 못한다.
- 사용자 요구에 따라 선택형 진행 UI를 자동 진행으로 변경했다. frontend는 mode=auto로 전송하며 서버의 결정적 문구 규칙이 chat/team-local로 분기한다. 외부 전송은 자동 선택하지 않고 기존 명시적 team API/CLI는 보존한다. 중복 request ID는 재실행하지 않는다.
- frontend/chat.js/index.html/hud.css: 한국어 역할별 진행·출력·모델 검토·수정 횟수·상한 종료 사유, 응답 전달과 협업 결과를 별도 표시. 인증된 조회를 1.5초 간격으로 직렬 호출하며 완료/연결 변경 후 늦은 결과는 무시한다. 근거 ID/조회 시각도 표시한다.
- 최신 HTML에서 chatMode 요소가 누락되어 JS와 불일치했던 문제를 확인했다. 자동 진행 전환으로 해당 선택 요소 의존을 제거했다.
- tests/frontend/chat.test.cjs: 실제 HTML ID로 초기화, 자동 mode 전송, 인증 헤더, 완료 후 늦은 조회·연결 해제 경계 3개 통과. 관련 Python tests/test_chat.py + tests/test_hybrid_team.py + tests/test_hud.py 57개 통과(4.47초), 기존 폐기 경고 1개. 하드링크 검사도 호스트 실행에서 통과했다. 전체 회귀 검사는 아니다.
- 실제 _worker 호출은 호스트에서 정상 응답했다. 샌드박스에서는 같은 loopback 호출이 실패했으므로 모델 코드 오류로 단정하지 않고 worker를 수정하지 않았다.
- 8788 리스너가 없음을 확인하고 이 세션에서 서버를 시작했다. 실제 브라우저 연결 파일 인증→한국어 인사 응답, 명시적 team-local→로컬 작업/검토/종합 응답을 확인했다. 협업 결과 completed였지만 실행/파일 적용은 수행하지 않았다. 혼합 모드 외부 전송 안내도 자동 UI 전환 전에 확인했다.
- 설치 Agent Reach의 exa_search.py는 mcporter/Exa 설정 검사 계층이고 실제 검색 실행 경로가 아니다. 웹 reader는 Jina 경유 urllib 요청이며 기존 Jarvis의 검증 IP 고정/리디렉션 금지 경계를 그대로 제공하지 않는다. 임의 CLI forwarding으로 연결하지 않았다. 일반 웹 검색은 미연결이다.
- 다음: 최신 자동 분기 코드로 서버 반영, 자동 진행 실제 화면과 현재 협업 상태 확인, 문서 검사. 실제 검색은 고정 backend·요청 길이·응답 수/크기·시간·예산·공개 IP/리디렉션 정책을 갖춘 별도 도구로 후속 구현해야 한다.

최종 화면 반영 확인:
- 이 세션에서 시작한 8788 리스너의 실행 명령을 확인한 뒤 해당 서버만 재시작했다. 대화 DB/기존 MCP는 보존했다.
- 실제 브라우저 재인증 후 선택 메뉴가 없는 화면에서 함수 제안을 전송했다. 인증된 상태에서 mode=team-local, turn.status=completed, team.status=completed, 로컬 배정 3개를 확인했다. 실제 실행/파일 적용과 구분하는 안내 및 역할별 결과/모델 검토/수정 0회 표시를 확인했다.
- Git 제외 증거: artifacts/chat-auto-live-verification.json, artifacts/chat-auto-ui.jpg. 토큰은 증거에 포함하지 않았다. 외부 혼합 협업은 이 화면 시험에서 실행하지 않았다.
- 최종 Node 채팅3개+기존 task-stream8개 총11개 통과. 문서·구조 검사 통과. Python57개는 앞서 실행한 관련 묶음 결과다.
- 다음 범위는 모델 기반 요청 분류/관련성 검증과 제한된 실제 웹 검색이다. 현재 자동 분기를 완전한 의미 이해나 협업 엔진의 웹 근거 검증 완료로 해석하지 않는다.

## 2026-10-20 — 제한형 실제 웹 검색

변경: web/service.py에 엄격한 SearchRequest(2–200자/최대3결과), 고정 Bing RSS search(), 공통 XML/HTTPS 파서와 공개 결과 URL 필터를 추가했다. 새 tools/search_public_web_tool.py는 기존 guarded_tool 경로로만 등록한다. 기존27개 이름/스키마/설명 계약을 보존하고 현재29개에 추가되어30개다. Agent Reach 검색 채널은 설치/설정 진단 계층이므로 임의 CLI forwarding으로 사용하지 않았다.

보호: 기존 HTTPS worker의 공개 IP 검사·검증 IP 고정·리디렉션/압축 거부·512KiB/12초·중단/공통20회 예산을 재사용한다. 검색어 비밀값/제어문자와 HTML/엔티티/빈 결과/주요 검색어 미일치 발췌를 거부한다. 결과 페이지 자동 조회·쿠키·계정·보고서 저장은 없다. 결과 링크의 DNS 목적지는 조회하지 않는다(반환된 데이터이며 자동 접속하지 않음).

채팅: search_public_web을 허용하고 명시적 웹 검색의 첫 라운드 도구를 검색으로 제한한다. 실제 스키마 enum 외 행동은 거부한다. 비ASCII 검색어는 로컬 _infer의 SearchQuery로 다듬으며 별도9216 예약/동일140초 기한을 소비한다. 의사결정3회+검색어 준비최대2회=추론최대5회, 도구최대2회다. no-argument 도구의 모델 문자열 none만 빈 인자로 정규화한다. 목록에서 확인한 html/weather 출처를 명시하면 다음 라운드는 문서 수집으로 제한한다.

실패에서 배운 점: 첫 실제 한국어 검색은 Bing이 YouTube 등 무관한 결과를 반환했고 모델이 검색 성공처럼 소개했다. 어휘 관련성 차단을 추가했다. 이후에는 모델이 검색에 없는 docs.python.org 링크를 덧붙였다. 따라서 검색 최종 응답은 모델 생성 서술을 그대로 내보내지 않고, 선택한 성공 근거의 실제 제목·발췌·URL·UTC 시각으로 서버가 구성한다. 실시간 basis는 검색 발췌만으로 승인하지 않는다. 이 두 초기 시도를 성공 사례로 간주하지 않는다.

검사: tests/test_chat.py + test_web_information.py + test_plugins.py + test_hybrid_team.py + test_hud.py 호스트136개 통과(6.07초), 기존 Starlette/httpx 폐기 경고1개. 입력/비밀/관련성/차단페이지/로컬주소/중단/호출예산/기존계약/허구URL 억제/한국어 변환 예약을 포함한다. 전체 회귀 검사는 아니다.

실제 연결: guarded 도구로 Bing 검색 응답을 확인했다. 최종 로컬 모델 시험에서 한국어·영어 질문 모두 search_public_web 성공2회 후 실제 발췌/주소/시각 응답, 미지원 네이버 실시간 순위는 도구근거 없이 값을 만들지 않는 안내, 출처목록→collect_web_information 성공 연속 호출을 확인했다. 등록 문서에 대한 최종 모델 답변은 여전히 구체성이 부족했다. 검색 원문/정확성/최신성이나 모든 표현의 의미 관련성 검증은 미완료다. 증거는 Git 제외 artifacts/public-search-live.json, artifacts/public-search-chat-live.json, 전 단계 결과 public-search-chat-live-before-final.json이다. 사용자 파일·인증정보를 모델 시험에 수집하지 않았다.

다음: 진행 중 요청 없는지 확인 후8788 반영·실제 브라우저 검색. 별도 stdio MCP 클라이언트는 새 도구 발견을 위해 재연결이 필요하다. 고품질 요약/원문 확인·의미 기반 요청 분류·협업의 웹 근거 검증은 별도 남은 범위다.

운영 화면 최종 확인:
- 진행 중 대화0개를 내용 없이 확인한 뒤 이 세션의8788 서버만 재시작했다. 실제 브라우저에서 새 대화에 한국어 웹 검색을 보내 search_public_web 1회 성공, 실제 발췌/HTTPS 주소/UTC 조회 시각 표시를 확인했다. 새 도구를 설치만 한 상태와 구분한다. 증거 artifacts/public-search-ui.jpg.
- 마지막으로 로컬 문서만 요청한 경우 웹 전송을 거부하는 경계를 추가했다. 해당 targeted 검사1개 통과(0.94초), 기존136개 묶음 결과와 별도 실행이다. 최종 경계도8788 서버에 반영했다. Node 화면/작업 스트림11개 통과. 문서/구조 검사 통과.
- 검색 발췌가 영어로 나올 수 있고 공식 문서 요청에도 홈페이지 등 넓은 결과가 나올 수 있다. 관련성 필터/검색어 변환은 보조 정책이며 정확한 문서 발견이나 의미적 정답 보장이 아니다. 등록 문서 최종 답변의 구체성 개선과 검색 원문 검증은 남았다.

## 2026-10-20 — 검색 결과의 실제 페이지 확인

- web/service.py의 PageRequest/read_page와 web/tools/read_public_web_page_tool.py 추가. 입력은 공개 HTTPS URL이며 기존 Registry/_fetch 공개 IP/DNS 고정/512KiB/12초/리디렉션·압축 거부를 재사용한다. HTML/plain text만 정적으로 추출하고 최대3000자/잘림 표시/출처/UTC 조회 시각을 반환한다. JS/로그인/쿠키/파일 저장은 없다. 차단·CAPTCHA·빈본문·PDF 등 미지원 형식은 실패다. 신규1개로총31개, 기존27개계약은 보존했다.
- app/chat.py는 현재 요청의 성공 search_public_web trace.sources URL과 정확히 일치하는 경우만 페이지 도구에 전달한다. 이전 기록·미조회·실패 검색의 URL은 거부한다. 원문을 요청하면 검색 다음 라운드의 도구/인자 schema를 해당 페이지들로 제한하고 최종 근거는 페이지 기록을 사용한다. 검색1+페이지1로 기존 도구2회 한도 유지. 페이지 응답도 모델 생성 주장 대신 실제 텍스트 발췌로 구성한다. 최대800자 채팅 인자 제한은 유지했다.
- 관련 검사118개 초기 통과 후 최종 chat/web/plugins/hybrid/HUD 묶음155개 통과(7.03초), 기존 폐기 경고1개. 정확한 현재 검색 범위·잘림·script/nav 제거·비공개주소/인증URL·빈본문/차단/미지원형식·허구답변 억제·기존계약을 포함한다. 기존 네트워크 worker 검사도 포함되며 전체 회귀 검사는 아니다.
- 실제 로컬 모델 한국어 요청에서 search_public_web→read_public_web_page 모두 성공하고 Python 공식 사이트의 정적 텍스트·주소·시각을 반환했다. 증거 artifacts/page-reader-live.json. 페이지에 스크립트 미실행 대체 안내와 메뉴가 섞였으며 전체 문서나 사실 정확성 검증으로 보지 않는다. 검색 결과 관련성 및 모델 기반 요청 판단은 후속 개선이다.
- 브리핑 담당과 사용자 직접 소통 지시를 원 대화에서 확인하고 역할을 조율했다. Google presentation/CLI는 상대 담당, 채팅/검색/HUD/8788은 이 세션 담당. 상대 파일은 수정하지 않았다. 운영진단 담당 생성 여부는 미확인이다.
- 다음: 진행 중 요청 확인 후8788 반영과 실제 브라우저 검색→페이지 흐름, 문서/구조 검사.

운영 반영: 진행 중 대화0개 확인 후이 세션의8788서버만 재시작했다. 기존 DB와 MCP 기능을 보존했다. 실제 브라우저 최종 검색→페이지 확인을 진행한다. 페이지 출력은 영어 원문/메뉴/JS 미실행 대체 문구를 포함할 수 있음을 화면 문구에 명시했다.

최종 브라우저 확인: 최신8788서버에서 새 대화의 한국어 검색→실제 페이지 읽기2도구가 성공했고, 페이지 발췌·최대3000자/JS미실행/전체검토아님 안내·URL·UTC시각이 표시됐다. artifacts/page-reader-ui.jpg에 화면을 보존했다. 모델/시험 코드 결과와 실제 브라우저 확인을 구분한다. 문서/구조 검사는 통과했고 안내 문서에 범위와 제약을 기록했다.

## 2026-10-20 — 답변 전 독립 요청 판단

- app/chat.py의 RequestPlan(strict mode/evidence/web_action/unsupported)으로 자동 요청을 최종 답변 이전에 로컬 분류한다. 최근2개대화의각user/assistant500자 이내를 로컬에만 제공한다. 외부AI 모드는 스키마에 없으며 조회필요판정은 chat으로 고정한다. 미지원은 도구 없이 서버의 실행불가안내를 반환한다. 사전external/local근거 요구는 최종general답변도 해제하지 못한다. 로컬관찰에는 로컬도구만 허용한다. 외부검색과 원문필요판정은 도구schema에 반영한다. 기존키워드검사는 보조로 남긴다.
- agents/chat.py: requested_mode와 request_plan을 추가하는 additive SQLite migration. 기존mode/team/응답은 보존한다. auto 요청을 begin한 후분류→resolve_auto 단일갱신으로 chat/team-local을 확정한다. 중복요청은 분류 전후/동시인스턴스에서도재실행하지 않는다. 명시적chat/team/team-local 및 HybridTeam 접점은 그대로다. 이전버전 자동요청의 원래방식정보는 복구할수없어 같은ID재전송이 거부될수있다(재실행은안함).
- 자동분류25초/예약9216, 일반대화의사결정3+검색어준비최대2에분류1추가=최대6추론. 자동일반요청은입장후180초기한, 협업660초는유지한다. 분류실패는failed이며가짜성공/자동재시도없다.
- 검사: chat/hybrid/HUD/web/plugins163개통과(8.04초), 기존폐기경고1개. 저장마이그레이션/동시인스턴스중복/외부모드거부/분류실패/독립근거/미지원무실행을포함한다. 전체회귀검사는아니다.
- 실제합성6유형: 인사·함수개념설명은chat/general, 표현이다른PC상태질문은local요구후 get_system_status 성공, 홈페이지찾기는external분류후검색→페이지성공, 함수제안은team-local(외부0회), 삭제요청은unsupported로도구없이거부했다. 함수협업의최종판정은수정3회상한후needs_user_input이며전체성공아님. 각요청중복재전송시예약증가없음을확인했다. artifacts/request-planner-live.json 및 초기실패기록 request-planner-live-initial.json에보존한다.
- 초기PC질문은분류만맞고도구를생략해미확인안내가나왔다. local조회용첫라운드schema와최종근거제약을추가해실조회로보완했다. 미지원삭제도분류flag로명시해불필요한조회가일어나지않게했다.
- 모델분류의오류가능성과의미적정확성/보안불변식의완전성은보장하지않는다. 협업엔진웹근거검증을완료했다고주장하지않는다. Claude준비보고는별도담당이수행했으며자동외부AI정책은변경하지않았다.
- 다음: 진행중요청없음확인→최신서버반영/DB마이그레이션→보이는브라우저에서표현이다른PC조회검증. 상대담당의브리핑/외부CLI파일은수정하지않았다.

최종 반영 확인:
- 운영 DB의 기존 기록을 보존하는 추가 마이그레이션 및 진행 중 요청 0개를 확인한 뒤, 이 세션의 8788 서버만 재시작했다. 최신 Claude 발견 코드도 로딩되지만 외부 AI 자동 선택/호출은 변경하지 않았다.
- 보이는 브라우저의 새 대화에서 “내 컴퓨터 지금 얼마나 바빠?”를 보내 실제 PC 도구 1회와 CPU/메모리/디스크 응답을 확인했다. 증거 artifacts/request-planner-ui.jpg. 테스트 종료 시 사용자가 입력 중인 내용은 변경하지 않았다.
- 최종 unsupported 필수 필드와 자동 분류 관련 targeted 10개 검사 통과(2.19초). 앞선 163개 묶음과 별도 실행이며 합산하지 않는다. 문서/구조 검사도 통과했다.
- 다음은 분류의 오판 사례 보완과 다른 담당의 검증된 외부 제공자/브리핑 계약 연결이다. OpenCode 무료 API 경로는 별도 담당이며 이 작업에서 추가 외부 계정·과금·모델 호출을 수행하지 않았다.

## 2026-10-20 — 무료 제공자 실행 연결부와 상태 표시

- 협업 담당의 provider_routing 계약을 재사용한 app/dispatch.py FreeDispatcher를 추가했다. trusted 후보 snapshot/callback 및 `executors[id](message,deadline)`만 주입하며 기본 등록 목록은 비어 있다. 모의 실행기 성공과 실제 무료 API 가용성은 구분한다. OpenCode 403/Gemini 미검증을 준비 완료로 바꾸지 않았다.
- agents/chat.py에 dispatch JSON을 추가하는 additive migration과 원자적 이전 상태 비교 갱신을 추가했다. 실행 전 예약을 기록하고 완료 결과도 dispatch.reply에 보존한다. 요청 전체3회/12000 예약 토큰을 공유하며 실패·전환에서 예약을 환불하지 않는다. 확인된 한도 실패만 다른 무료 후보로 전환한다. timeout/일반 예외는 unknown으로 멈추고 원시 오류·인증정보를 기록하지 않는다. 예약/완료/미확인 기록이 있으면 새 실행을 거부한다. 재시작 후 자동 복구를 구현한 것은 아니다.
- app/chat.py에 mode=free와 명시적 자연어 무료 API 요청 연결을 추가했다. 이 직접 경로는 Ollama를 호출하지 않는다. 분류 모델의 agent_policy는 별도 요청 문구 조건과 일반 제안 범위에서만 free로 연결한다. 기존 chat/team/team-local와 HybridTeam 접점은 유지했다. 무료 후보 없음은 응답 전달 완료/dispatch 사용자 확인 필요로 기록한다.
- frontend/chat.js: 무료 배정 상태와 응답 전달을 분리하고 후보 제외 이유·예약/unknown·보존 응답을 표시한다. 선택 UI를 추가하지 않았다.
- 검사: free_dispatch/chat/provider_routing/hybrid/HUD111개 통과(7.01초), 기존 폐기 경고1개. Node 채팅/작업 스트림12개 통과. 마지막 직접 무료 경계9개 통과(1.25초)는 별도 실행이다. 실제 무료 API 호출 성공이 아니라 모의 어댑터·인증 HTTP·저장/CAS·정책 회귀 검사다. 첫 HTTP 인증 검사는 Origin 누락으로403을 받아 기대값과 불일치했으며 올바른 Origin을 주고 인증 없음401을 검증하도록 수정했다. 보안 검사를 약화하지 않았다.
- 사용자 요청으로 Claude 완료 검토 파일을 옆 패널에 열고, 보이는 PowerShell 상태 창을 추가했다(artifacts/watch-claude.ps1). 실제 Claude CLI 프로세스 유무/PID와 이전 완료 결과를 구분한다. 이 창이 Claude 대화/실시간 모델 출력 자체인 것은 아니다. 이후 실제 Claude 호출은 보이는 창/로그를 제공하도록 담당에게 전달했다.
- 실제 한도 회복을 확인한 다음 신규 작업부터 원래 Codex 역할로 복귀하고, 진행 중 작업/결과는 유지하는 조건을 workload 정책 담당에게 전달했다. 날짜 변경만으로 리셋을 확정하지 않는다. 이 배정 연결부 자체가 계정 한도 모니터를 구현한 것은 아니다.
- 다음: 진행 중 요청 확인 후8788 반영 및 후보 없음의 실제 화면 확인. 검증된 무료 후보/실행 어댑터의 실제 연결은 gateway 담당의 접근 검증 후 진행한다. 현재 무료 실제 모델 호출은 미완료다.


## 2026-10-21 — 내용 없는 Codex 세션 상태 수집과 HUD

- `codex/session-status`, 기준 main `b88e900`: session_status 저장소, 인증 GET /api/sessions, HUD의 외부 세션 보고를 추가했다. 다른 담당의 session_signals/session_signal 정규화 CLI를 통합했다. 공개 쓰기 API·프로세스 제어·모델 내용 수집은 추가하지 않았다. 기존 도구와 협업 접점은 보존했다.
- 멱등 이벤트, 턴 종료/교체 후 늦은 이벤트 차단, 조회 전용 DB, 파일 경계·용량 제한, 고정 오류 응답을 검사했다. 보고는 자기 선언, 수신만 관찰이며 검증 근거는 빈 목록이다. 순번 없는 새로운 턴의 순서, 로컬 동일 사용자에 의한 위조, 파일 검사와 사용 사이 경쟁 조건은 보장 범위 밖이다.
- 실제 CLI 시험에서 문자열 workspace가 Guardrails에 전달돼 조용히 종료하고 저장하지 못하는 결함을 발견했다. Path 변환과 실제 subprocess 영속성 검사로 수정했다. 초기 exit=0만으로 성공을 판단하지 않았다.
- 통합 검사 `tests/test_session_status.py tests/test_session_signals.py tests/test_hud.py`: 69 passed, 기존 Starlette 경고 1개. 저장소 잠금 시 이전 상태 보존과 해제 후 재저장을 포함한다. 전체 회귀 검사는 아니다. 이후 CLI 짧은 대기 연결은 별도 검증한다.
- ego-lite의 합성 전용 workspace/8793에서 실제 CLI→DB→인증 HUD의 승인 대기 표시를 확인했다. Stop 다음 PreToolUse도 응답 종료 표시를 유지했고 연결 해제 시 카드 제거·패널 숨김을 확인했다. 테스트 창은 종료했다. 프롬프트와 명령의 시험 문자열은 정규화 경계에서 제외했다. 실제 desktop hook은 설치/신뢰/자동 활성화하지 않았고, 8788/8791 운영 서버는 이 작업에서 변경하지 않았다.
- 설치됨 / 연결됨 / 실제 검증됨: hook 기능을 가진 Codex가 설치돼 있다는 다른 담당의 진단과, 이 CLI/저장/API의 연결 및 합성 입력 화면 확인은 구분한다. 실제 플랫폼의 이벤트 자동 수신은 아직 미검증이다. 다음은 독립 검토 및 PR 승인 후 반영, 별도 신뢰 절차를 거친 실제 세션 확인이다.

최종 통합: main `2d01d09`와 CLI의 `busy_timeout=0.2` 연결을 합친 뒤 위 세 묶음과 process_watch를 포함해 81 passed(기존 경고 1개, 3.18초). 두 저장 트랜잭션의 잠금 대기를 각각 제한하되 전체 전달 시간 보장은 아니다. PR #2는 독립 검토 대기 draft이며 운영 반영과 실제 hook 신뢰는 미완료다.


## 2026-10-22 — PR #2 독립 검토 보완

- 실제 fetch 시 main은 `78f9448`이었다. 디스패처 인자/조회 경로/스크립트와 세션 조회를 함께 보존해 events.py·hud.py·test_hud.py·PLAN의 충돌을 해결했다. 보고서의 archive 이동 및 communication→board 링크를 유지했다.
- 독립 정적 검토 D1은 수정 전 3개 검사에서 실패로 재현됐다: Stop/Interrupt의 순서가 바뀌면 sessions.state와 turns.terminal 불일치, SessionEnd→SessionStart→옛 Stop의 재반영. 첫 terminal을 유지하고 재개 후 옛 종료를 무시하도록 수정했다. 반복 turn_started가 도구 단계를 되돌리는 문제도 차단했다.
- D2는 자동 삭제 없는 보존 계약을 유지했다. inspect에 이벤트/세션 사용량·상한·도달 여부를 추가하고 HUD 경고를 연결했다. D3의 unknown 세션은 관측 부족을 드러내는 보수 설계로 유지했으며 실제 온라인으로 표시하지 않는다. employee는 codex/claude-code만 명시적으로 허용하고 동명 ID를 직원별로 분리한다.
- 8793 합성 전용 서버와 ego-lite에서 Claude 카드, 이벤트·세션 한도 경고, 자동 삭제 없음 안내, dispatcher 스크립트 보존을 확인했다. 처음 연결은 서버 준비 전 거부됐고 준비 후 같은 작업 공간에서 성공했다. 이 시험은 실제 Claude hook 설치나 실제 플랫폼 이벤트 검증이 아니다. 운영 서버와 사용자 설정은 변경하지 않았다.

검증 결과: main `78f9448` 통합과 최종 반복 시작 보완을 포함한 전체 pytest 990 passed, 기존 폐기 경고 1개(40.58초). 디스패처 프런트 검사 9개, collaboration.js 구문, 문서·구조 검사 통과. D1 수정 전에는 추가 재현 검사 3개가 실제 실패했다. Claude 수신기 브랜치의 subprocess 저장 검사는 별도 통합 담당에게 요청했으며 이 PR에서 완료했다고 주장하지 않는다. PR #2의 병합과 native hook 설치/신뢰는 별도 승인 단계다.

추가 main 갱신: 전체 검사 중 main이 `5117a76`으로 전진해 `ecdf866`에 추가 통합했다. 이 기준에서 세션/HUD/프로세스/사용량/배정/승격 관련 178 passed, 1 skipped(기존 usage 수집기의 심볼릭 링크 권한 조건), 경고 1개(6.25초). 디스패처 화면 10개 및 문서·구조 검사 통과. 이는 이전 기준 전체 990개 검사와 별도이며 최신 기준 전체 실행으로 합산하지 않는다. Claude 한도 소진 안내를 받아 실제 Claude 추가 호출이나 깨우기는 하지 않았다.


## 2026-10-22 — 운영 전환 준비: 서비스 관찰과 설정 구분

- 기존 인증 `GET /api/dispatcher`를 재사용한다. config_enabled/paused/effective_enabled는 설정상 허용이며 실행 증거가 아니다. service.running/pid/started 호환 필드를 보존하고 status/reason/source를 추가했다. status는 running(기록 PID의 생성 시각 조건 일치), stopped(기록 PID 없음), not_registered(기록 없음), identity_mismatch(PID 재사용 가능성), unknown(기록 오류·읽기/프로세스 조회 거부)다. 원시 오류·명령줄·경로는 반환하지 않는다. 등록 파일은 인증된 프로세스 신원 증명이 아니다.
- process_watch도 not_registered/unknown을 서비스 종료로 단정하지 않도록 맞췄다. 실제 종료/재사용 기록과 기존 직원 실행 감시는 유지한다. 조회 미확인은 error=service_observation_unavailable로 전달하며 다른 감시 결과를 보존한다. 기존 프런트가 running=false를 무조건 꺼짐으로 표시한다면 새 status에 맞춘 후속 소비 변경이 필요하며, 이 브랜치에서 프런트를 수정하지 않았다.
- 채팅 metadata.model은 선택 모델 설정이다. 기존 API는 Ollama 추론 가용성을 관찰하지 않는다. 새 네트워크 probe API를 추가하지 않고 관찰이 없는 화면은 미확인으로 표시하도록 프런트 담당에게 계약을 전달했다. HTTP/포트 가용성과 모델 추론 성공은 구분한다.
- 별도 검토용 설정 후보를 artifacts/service-readiness-20261022에 작성하고 스키마를 검증했다. 최상위 enabled=false, 직원 후보 codex/opencode만 enabled, claude-code/cursor는 false, 고정 무료 모델만 지정했다. 로컬 리드 Ollama는 유지한다. 이 준비에서 기존 config/dispatcher.json과 명부를 변경하지 않았고 실제 서비스를 시작하지 않았다. OpenCode는 메인의 최신 합성 호출이 provider.auth 403 FreeTierError였다는 보고가 있어 실제 가용성 미확인으로 명시한다. 권한 완화·유료 대체·반복 호출은 하지 않았다.
- 읽기 전용 실제 관찰: 등록 service.json 없음(not_registered), 수신 포트는 11434만 확인됐고 8766/8787/8788/8791은 확인되지 않았다. 포트만으로 프로세스 정체나 추론 성공을 확인한 것은 아니다. 준비 체크에는 기본 HUD8787과 이전 시험 포트들, 실제 코드/작업공간 일치, 진행 중 작업/잠금/중단, 사용량의 신선도, 승인 경계를 구분했다.
- 검증: PR #2가 병합된 main32765d0 통합 후 dispatch_control/process_watch/dispatch_service/HUD/session_status 113 passed, 기존 경고 1개(3.06초). 실제 서비스 시작·예약 등록·모델 호출·hook 설치·운영 설정 변경·main 병합·공유 WIP 정리는 수행하지 않았다. 다음은 독립 검토와 프런트의 추가 필드 소비 연결이다.
