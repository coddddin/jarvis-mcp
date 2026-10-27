# 문서 안내

문서 체계의 목표는 자비스와 연결된 AI들이 작업·데이터·지식·결정·검증 근거를 보존하고, 필요한 순간 도구로 찾아 이어 쓰는 **제2의 두뇌**입니다. Obsidian/Markdown은 사람이 검토·정리하는 지식 원본이며, 로컬 색인과 도구는 AI의 접근 경로입니다. 폴더 정돈이나 보기 좋은 그래프 자체를 완료 목표로 삼지 않습니다.

이 페이지는 개발 문서의 유일한 목차입니다. 원문은 표준 Markdown과 상대 링크로 유지합니다.

## 제2의 두뇌 구축 순서와 완료 기준

아래는 구현 계획이며, 현재 모두 연결됐다는 뜻이 아닙니다.

1. **기록 대상을 정한다:** 진행 중 작업은 Board/실행 장부, 원문 대화는 ChatStore, 확인된 선호는 MemoryStore, 재사용 지식·결정은 Vault에 둡니다. 저장소 개발 문서는 버전 관리 원본을 유지하고 필요한 지식만 원본 링크와 함께 연결합니다. 같은 내용을 네 저장소에 복제하지 않습니다.
2. **저장 경로를 완성한다:** 작업 결과에서 근거·결정·미해결 항목을 추려 검토 가능한 지식 초안으로 남기고, 승인 후 재사용 지식으로 연결합니다. 대화 원문 보존과 지식 확정을 구분합니다. 이 초안 생성·쓰기·승인 연결은 미구현 계획이며 기존 승인 정책을 자동으로 바꾸지 않습니다.
3. **찾아 이어 쓰게 한다:** 프로젝트·주제·원본 ID/출처로 필요한 기록을 찾고 작업/실행/지식의 관계를 연결합니다. 기존 Vault 색인·검색 도구부터 재사용하며, 승인된 범위만 직원에게 전달합니다. 검색 결과에는 원본·버전·근거가 있어야 하고 관련 자료 없음도 명시합니다.
4. **변경을 반영한다:** 같은 사실의 중복·상충, 수정·철회·만료를 처리합니다. 옛 지식을 최신 사실처럼 재주입하지 않으며 검색 인덱스는 원본에서 재생성할 수 있어야 합니다.

완료 확인은 **작업 A의 검토된 결과 저장 → 다른 AI의 새 세션에서 도구로 검색 → 출처를 확인하고 다음 작업에 사용 → 원본 수정/철회가 후속 검색에 반영**되는 한 경로로 합니다. 검색 정확도·검색/인계 지연·원문 재독 및 토큰 감소를 같은 표본에서 비교합니다. 파일 수나 문서 수를 성과로 삼지 않습니다.

현재 기반은 [Vault 색인·검색](guides/obsidian.md), [기억 저장·확인](guides/agent-controls.md), [Board](guides/board.md), [실행 장부](guides/dispatcher.md), [대화·출처](guides/chat.md)입니다. Vault는 승인 검색이 구현돼 있으나 작업 결과의 자동 지식화·쓰기·직원별 조회 권한·공통 관계 조회가 모두 완성된 것은 아닙니다. 기존 Vault 메타데이터는 미등록 필드를 거부하므로 관계 필드를 임의 추가하지 않고 스키마/도구 변경으로 검토합니다.

## 문서를 찾는 순서

1. 처음 실행하거나 사용하는 사람: 루트 [시작 안내](../README.md) → 필요한 사용 가이드.
2. 개발하는 사람: [현재 계획](PLAN.md) → [개발 규칙](DEVELOPMENT.md) → [구조](architecture.md) → 변경 대상 가이드.
3. 지금 진행 중인 일: Board의 담당·claim·인계와 GitHub PR/head를 확인. 과거 보고서나 문서에 복사된 상태를 현재 진행도로 사용하지 않습니다.
4. 과거 결정·검증 근거: 아래 보관 자료에서 필요한 항목만 확인합니다.

## 저장소 문서와 개인 지식의 경계

- 이 저장소의 `docs/`는 제품 계획·개발 계약·사용법·검증 근거입니다. 현재 기준을 개인 Vault에 복제해 두 번째 원본으로 관리하지 않습니다.
- 개인 지식은 작업공간의 `JARVIS-Vault`에서 관리합니다. 검색 승인과 민감도는 [Vault 안내](guides/obsidian.md)를 따릅니다. 저장소 문서 전체가 승인 지식으로 자동 편입되는 것은 아닙니다.
- Obsidian으로 저장소를 별도로 열어 문서 링크를 탐색할 수 있습니다. 이 문서 정리는 Vault 초기화·파일 이동·플러그인 설치·자동 색인·클라우드 동기화를 실행하지 않습니다.
- 문서 분류·상태·이관 규칙은 [개발 규칙의 문서 관리 절](DEVELOPMENT.md)을 기준으로 합니다.

## 현재 기준
- [PLAN.md](PLAN.md): V2~V5를 통합한 유일한 현재 계획·진행 상태.
- [DEVELOPMENT.md](DEVELOPMENT.md): 개발·보안·검증·문서 관리 규칙.
- [패키지 구조와 이전 계획](architecture.md): 기능별 코드 배치·의존 방향·진입점 보존·구조 검사.
- [프로젝트 지도](../AGENTS_CONTEXT.md): 에이전트가 처음 읽는 50줄 이내의 요약.

## 사용 안내
- [자비스 제2의 두뇌: Obsidian 사용법](guides/obsidian.md): Vault 열기·작업 노트·검토·색인·대화 검색·수정/철회.
- [GitHub 우편함과 선택 로그인 수신](guides/github-inbox.md): 수동 미리보기·승인 Issue 수신·선택 설치를 한 곳에서 안내.
- [로컬 리드·외부 작업자 협업](guides/hybrid-team.md): OpenRig 배정 소스 이식, 로컬/Codex 제안 협업과 운영자 CLI.
- [자비스 전용 채팅](guides/chat.md): 로컬 모델 대화·기록·읽기 도구와 인증·예산 경계.
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
- [REA · Agent Reach](guides/rea-agent-reach.md): 전용 설치, 단일 프로그램 정적 분석과 등록 RSS 읽기.

## 보관 자료 (현재 실행 지시가 아님)

현재 기준은 PLAN.md이며 실시간 진행은 Board/PR에서 확인합니다. 아래 문서는 당시 근거를 보존하는 기록입니다. 기본 온보딩·실행 순서에 넣지 않습니다.

- [2026-10-22 GitHub·Vault 통합 인계/검증](archive/reports/github-vault-review-2026-10-22.md): 원래 인계 두 건의 본문과 근거를 보존한 역사 기록.

- 단계 보고서: [계획 이력](archive/reports/plan-history.md) · [협업 엔진](archive/reports/hybrid-team.md) · [Phase 1](archive/reports/phase-01.md) · [Phase 2](archive/reports/phase-02.md) · [Phase 2.5](archive/reports/phase-02-5.md) · [Phase 3](archive/reports/phase-03.md) · [REA · Agent Reach](archive/reports/rea-agent-reach.md)
- 옛 계획 원문: [V2](archive/MASTER_BLUEPRINT_V2.md) · [V3](archive/MASTER_BLUEPRINT_V3.md) · [V4](archive/MASTER_BLUEPRINT_V4.md) · [이전 개발 지침](archive/DEVELOPMENT_GUIDELINES_ORIGINAL.md) · [V5 원안](master_blueprint_v5.md)(사용자 지정 위치의 보관본)

## 변경 후 확인

현재 마일스톤과 채택한 결정은 [현재 계획](PLAN.md)에만 반영합니다. PR·Board에는 실제 검사와 인계를 기록하며 같은 내용을 새 보고서로 복제하지 않습니다. 문서를 바꾼 뒤 프로젝트 루트에서 `python scripts/check_docs.py`를 실행합니다. 필수 파일·로컬 파일 링크·문서 목록 누락·지도 길이를 검사하며 저장 시 자동 실행되는 설정은 아닙니다. 검사 범위는 [개발 규칙](DEVELOPMENT.md)을 참조하세요.


## 문서별 정리 판정 — 2026-10-27

범위는 기준 main84d24c1과 이 문서 브랜치의 `docs/` Markdown 46개입니다. 목적·제목/절·참조와 통합 대상 본문을 대조한 구조 판정이며, 모든 가이드의 명령을 재실행하거나 전 문장의 최신성을 검증했다는 뜻은 아닙니다. 개인 Vault·동기화 sources·제3자 문서는 대상 밖입니다.

판정: 유지 29개, 통합 3개, 보관 14개. 무근거 폐기 0개. 통합으로 기존 파일 3개를 제거하고 보관 파일 1개를 만들어 **46개 → 44개**입니다. 과거 원안과 검증 근거는 보존합니다.

<details>
<summary>46개 문서 각각의 판정과 이유</summary>

- `agents/domain.md` — **유지**: 도메인 용어·ADR 탐색 계약.
- `agents/issue-tracker.md` — **유지**: Board 작업 운영.
- `agents/triage-labels.md` — **유지**: CLAUDE/이슈 안내가 참조하는 분류 어휘.
- `architecture.md` — **유지**: 코드 경계·의존 방향.
- `archive/DEVELOPMENT_GUIDELINES_ORIGINAL.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/MASTER_BLUEPRINT_V2.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/MASTER_BLUEPRINT_V3.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/MASTER_BLUEPRINT_V4.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/plan-before-restructure-2026-10-23.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/graphify-pilot-2026-10-24.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/hybrid-team.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/phase-01.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/phase-02-5.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/phase-02.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/phase-03.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/plan-history.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `archive/reports/rea-agent-reach.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외.
- `DEVELOPMENT.md` — **유지**: 개발·검증·문서 규칙 원본.
- `guides/agent-controls.md` — **유지**: QA·예산·확인된 기억·이벤트.
- `guides/agent-tasks.md` — **유지**: 작업 상태·재시도·코드 제안 경계.
- `guides/agents-roster.md` — **유지**: 직원 등록·등급·온보딩.
- `guides/backup.md` — **유지**: 개인 데이터의 암호화 원격 백업.
- `guides/board.md` — **유지**: 협업 기록·claim·인계 계약.
- `guides/chat.md` — **유지**: 대화 저장·검색 출처·실행 구분.
- `guides/claude-session-hooks.md` — **유지**: 세션 관찰 신호·설치 계약.
- `guides/debug-assistant.md` — **유지**: 로그 진단·읽기 전용 제안.
- `guides/dispatcher.md` — **유지**: 자동 배정 장부·예산·중단 계약.
- `guides/file-organization.md` — **유지**: 사용자 파일 이동·복구 계약.
- `guides/github-inbox-logon.md` — **통합**: guides/github-inbox.md 로그인 수신 절로 통합; 기존 파일 제거.
- `guides/github-inbox.md` — **유지**: Issue 수신 및 로그인 수신 통합 원본.
- `guides/hybrid-team.md` — **유지**: 로컬 리드·외부 작업자 협업.
- `guides/knowledge-and-google.md` — **유지**: 일반 문서 검색과 Google OAuth; Vault 검색과 구별.
- `guides/local-hud.md` — **유지**: HUD 연결·관찰 API; 채팅 가이드와 구별.
- `guides/obsidian.md` — **유지**: 제2의 두뇌 실제 사용 경로로 재작성.
- `guides/plugins.md` — **유지**: 도구 추가·자동 로더·보안 경계.
- `guides/rea-agent-reach.md` — **유지**: REA 전용 설치·분석 범위.
- `guides/snapshots.md` — **유지**: 프로젝트 스냅샷·복구; 원격 암호화 백업과 구별.
- `guides/system-tools.md` — **유지**: 시스템 조회·명령 공통 제한.
- `guides/telegram.md` — **유지**: 휴대폰 조회·페어링·토큰 경계.
- `guides/web-information.md` — **유지**: 웹 수집·검색·출처 계약.
- `guides/workflows.md` — **유지**: 등록 프로세스 실행·종료·제약.
- `handoffs/work-2026-10-22.md` — **통합**: archive/reports/github-vault-review-2026-10-22.md에 본문 보존; 기존 파일 제거.
- `handoffs/work-review-2026-10-22.md` — **통합**: archive/reports/github-vault-review-2026-10-22.md에 본문 보존; 기존 파일 제거.
- `master_blueprint_v5.md` — **보관**: 고유한 과거 계획/결정/검증 근거; 현재 지시에서 제외·사용자 지정 경로 유지.
- `PLAN.md` — **유지**: 현재 목표·마일스톤 원본.
- `README.md` — **유지**: 목차·기억 구축 순서 원본.

</details>
