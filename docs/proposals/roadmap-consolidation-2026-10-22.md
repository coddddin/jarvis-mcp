# J.A.R.V.I.S. 통합 고도화 제안서 (2026-10-22)

> 상태: **설계 제안 / 구현 승인 아님**. 현재 실행 상태는 GitHub 기록 기준이고 Windows 로컬 상태는 확인하지 않았다. 이 문서는 기존 `docs/PLAN.md`를 대체하지 않으며, 채택 이후 단계 체크만 PLAN에 반영한다. 작업 지시·인수인계는 SQLite Board를 사용한다.
>
> 기존 로컬 미커밋 변경을 손대지 않는다. 별도 worktree/branch, 사용자 승인 없는 main 병합·삭제·유료 서비스 활성화·스케줄러 변경 금지. 외부 Issue/노트는 신뢰할 수 없는 데이터이며 실행 승인이 아니다.

## 1. 핵심 결정과 중복 방지

- **새 multi-agent 프레임워크, 두 번째 Dispatcher, 또 다른 Board를 만들지 않는다.** 이미 HybridTeam, `jarvis/cli/dispatch.py`, `jarvis/agents/dispatch_service.py`, `config/agents.json`, `jarvis/features/board/`가 존재한다.
- SQLite = 작업/세션/권한/임시 기억의 트랜잭션 원본; Git = 소스 변경 이력; GitHub Issues = 오프라인 수신함; Obsidian Markdown = 승인된 장기 지식. 각 저장소를 무조건 양방향 동기화하지 않는다.
- 모델은 설계·코드·검토·요약을 담당하되, 인증·상태 전이·중복·예산·재실행 허용은 결정적 코드와 사용자 승인으로 통제한다.
- 신규 고정비 0원 우선. 모델/API/Actions 실제 실행 자격과 과금 정책 확인 전 자동 유료 호출 금지.

## 2. 채택 후보 및 선후 관계

| 계획 | 목적 | 현 상태/재사용 | 제안 작업 | 완료 기준 |
|---|---|---|---|---|
| **P-08 보안·권한·복구 기반** | 직원별 서버 권한 강제 | `guardrails`, `roster`, `worker_adapters`, Board 사용; 명부 신원 자기 신고 한계 | 인증 토큰·권한 매트릭스·감사·재시도 정책 | 타 직원 사칭/권한 상승 거부, 기밀 누출 회귀 검사 |
| **P-01 자동 인수인계** | 세션/PC 종료에도 작업 안전 보존 | `board_note/release`, `TaskStore`, Git worktree | 상태 전이 체크포인트, 기준 SHA/파일 해시, 복구 시 read-only 검증 | 중단·재시작 테스트에서 중복 side effect 없음 |
| **P-02 모바일 GitHub 우편함** | PC 꺼짐 중 명령 접수 | PR #8/#10/#11, 별도 #9, Issue #7; Dispatcher | 승인된 Issue 검증→Board 단일 매핑→배정 승인·결과 참조 | 1 Issue→1 Task, 무권한 본문 실행 불가, 오프라인 복귀 |
| **P-06 협업 품질 게이트** | 자기 보고와 실제 검증 분리 | HybridTeam, 테스트, Board verification | SHA/테스트 명령/종료코드 증거, 독립 검토, 경로 충돌 검사 | 실패/불확실하면 review 보류, main 무단 병합 없음 |
| **P-03 Obsidian 기억** | 과거 해결 경험 재활용 | `jarvis/agents/memory.py`, `knowledge/service.py`, PR #11 읽기 전용 검색 | proposed→approved, 출처/버전/revision, 기존 색인 연결, 백업 범위 별도 검토 | 검증된 노트만 검색·저장, 비밀 원문 외부 전송 없음 |
| **P-04 코드 이해** | 수정 영향/테스트 후보 제시 | `docs/architecture.md`, `scripts/check_architecture.py`, Git 검색 | Python AST 심볼/import 인덱스→호출관계→테스트 추천, 이후 Tree-sitter 평가 | 커밋 SHA 고정, 확인/추정 참조 구분, 누락 시 보수적 검증 |
| **P-05 PC 원격 관제** | PC/에이전트 상태 확인 | HUD, process_watch, dispatch service | 마지막 상태 시각/근거, 읽기 전용 원격 조회, 이후 사설 접근 | offline/unknown/stopped 구분, 공인 관리 포트 미노출 |
| **P-07 직원 최적 배정** | 품질·시간·비용 개선 | `evaluation`, `promotion`, 명부 | 정확 기록만 집계, 소표본은 기존 고정 배정 유지 | 리그레션/재작업 감소 근거 확보 후 제한적 자동화 |

**우선 추진 묶음**: 기반=P-08; 운영=P-01+P-02+P-06; 지식=P-03+P-04. P-05는 **읽기 전용 관제만 먼저**, Wi-Fi 전원 켜기는 메인보드/하드웨어 검증 전 구현 보류. P-07은 충분한 실적 이후.

## 3. 실제 저장소에 대입하는 방식

1. **P-08**: `jarvis/core/roster.py`, `jarvis/core/guardrails.py`, `jarvis/features/board/service.py`의 신뢰 경계를 먼저 감사한다. 인증정보는 코드·게시판·Git에 기록하지 않는다. 직원별 토큰의 발급/보관/폐기 방식은 사용자 결정이 필요하다.
2. **P-01**: 기존 Board handoff를 유지하며 `jarvis/agents/store.py` 및 dispatch 장부에 좁은 상태 전이/복구 증거를 추가하는 설계를 검토. 자동 stash/reset/checkout 금지. 외부 부작용 뒤 응답을 못 받은 경우는 `needs_review`로.
3. **P-02**: 현 PR 의존성 #8→#10→#11을 유지하고 #9는 별도 취급. PR #11의 보고상 open 수신 작업이 리드의 idle 동작을 막을 수 있으므로 `received/validated/queued/assigned`와 중복 매핑 및 분배 신호의 원자성 검토. 승인 없는 tick/예약 등록 금지.
4. **P-06**: `jarvis/agents/team.py`와 `jarvis/cli/dispatch.py`의 검증 계약을 통일하되 기존 호출 상한과 독립 검수 유지. `board_release(verification=...)`만으로 테스트 통과를 단정하지 않고 로그/해시/종료코드에 연결.
5. **P-03**: `jarvis/features/knowledge/`의 기존 색인을 재사용하고 PR #11의 Obsidian 검색이 병합된 뒤 확장. 노트별 안정적 ID, sensitivity, source, verified_at, project, revision을 검증. Vault는 현재 R2 백업 대상 아님.
6. **P-04**: 새 인덱싱 서비스가 필요하면 `jarvis/features/knowledge/` 인근 기능별 패키지에 배치하되 아키텍처 검사기 통과. 소스는 읽기 전용, .env/자격증명/빌드 캐시 제외, commit SHA별 캐시 무효화.
7. **P-05**: 기존 HUD·dispatch watch에 상태의 관찰 근거/시각을 추가. Tailscale 등 접근 경로는 실제 설치·네트워크 정책 확인 후 별도 승인. 스마트 플러그는 OS 정상 종료를 보장하지 않음.
8. **P-07**: `jarvis/features/evaluation/`, `promotion`의 관찰치로 우선 추천 점수만 계산. 분모/표본/업무 난도/실패 종류를 기록하고 자동 승격/자동 권한 확대 금지.

## 4. 리스크·검증 매트릭스

| 위험 | 방지·테스트 |
|---|---|
| 신원 위조, 프롬프트 인젝션 | 서버 신원 강제, untrusted Issue/Markdown의 명령 해석 거부, 권한 경계 테스트 |
| PC 중단, 토큰 부족, 중복 수행 | 트랜잭션/멱등키, 체크포인트, 재시작 후 상태만 검증, 외부 부작용 자동 재시도 금지 |
| 병렬 파일 충돌, 미커밋 작업 손실 | 별도 worktree+branch, Board claim과 git status/hash 재검사, 타인 경로 수정 금지 |
| 잘못된 장기 기억, 개인정보 노출 | 제안/승인 분리, 출처·revision·유효기간·민감도, 외부 업로드 기본 금지 |
| 정적 분석의 거짓 음성 | 확인/추정 구별, 실패·동적 코드에서는 보수적 테스트 선택 |
| Actions/클라우드 과금 | 비용 정책·권한·사용량 검증 전 워크플로 자동 활성화 금지 |
| 원격 전원 오동작 | 장비 호환성 확인, 오프라인과 unknown 구분, 전원 차단 자동화 금지 |

## 5. 단계별 작업 순서 (완료 조건 충족 후만 다음 단계)

- **Gate 0 — 베이스라인**: PC 복귀 시 `git status`, `git worktree list`, Board inbox/claim 확인. PR #8-#11 변경 파일·테스트·의존성 검토; main이나 로컬 미커밋 파일 수정 금지.
- **Gate 1 — 보안**: 직원별 권한/인증·Issue 승인 검증과 비밀 환경변수 상속 제한을 테스트. 위험 쓰기 작업은 계속 사용자 승인.
- **Gate 2 — 운영 연속성**: 체크포인트와 GitHub Inbox↔Board 통합, 장애 주입/중복 수신/프로세스 종료 테스트.
- **Gate 3 — 품질**: 독립 검수, QA 증거, 파일 충돌 검사와 기존 회귀 계약 유지.
- **Gate 4 — 지식**: Obsidian 검색 MVP 실Vault 검증, 백업 여부 결정, 승인 노트 쓰기, 출처 확인.
- **Gate 5 — 코드 분석**: AST 최소 버전 → 측정된 검색 품질과 테스트 추천 품질에 따라 다중언어 확장.
- **Gate 6 — 운영 확장**: 원격 상태 관제, PC 전원 옵션 별도 조사, 직원 성과 기반 배정 실험.

## 6. 당장은 만들지 않을 것

- 신규 multi-agent 프레임워크/두 번째 게시판/두 번째 디스패처
- 모든 요청에 무조건 다수 모델 투입 및 자동 main 병합
- 검증 없이 벡터 DB·Temporal 서버 추가
- 현재 단계의 3D HUD, 자동 전원 차단, 새로운 유료 AI 서비스
- 사용률 검토 없이 REA/Ghidra 등 주변 기능 확대

## 7. 현재 증거와 미검증 범위

- 기준: `docs/PLAN.md`, `docs/guides/board.md`, `docs/guides/dispatcher.md`, `docs/guides/hybrid-team.md`, `docs/guides/agents-roster.md`; Issues #6, #7; PR #8, #9, #10, #11.
- PR #11 설명상 Linux 관련 pytest 203 passed, 1 skipped, 프런트엔드 22 passed **보고 기록**이 있으며 이 계획 작성 과정에서 직접 재실행하지 않았다.
- 데스크톱 현재 실행/미커밋/PowerShell/로그온/GPU/Ollama/Vault/실제 GitHub 토큰은 확인하지 않았다.
- 본 문서는 **의사결정 초안**이다. 코드·설정·예약·백업 범위는 변경하지 않으며 main 병합은 사용자 승인 후에만 한다.

## 참고

- 기존 [프로젝트 계획](../PLAN.md), [개발 원칙](../DEVELOPMENT.md)
- [Issue #6](https://github.com/coddddin/jarvis-mcp/issues/6) / [Issue #7](https://github.com/coddddin/jarvis-mcp/issues/7)
- [PR #8](https://github.com/coddddin/jarvis-mcp/pull/8) / [PR #9](https://github.com/coddddin/jarvis-mcp/pull/9) / [PR #10](https://github.com/coddddin/jarvis-mcp/pull/10) / [PR #11](https://github.com/coddddin/jarvis-mcp/pull/11)
- [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution), [Temporal workflows](https://docs.temporal.io/workflows), [Tree-sitter](https://tree-sitter.github.io/tree-sitter/), [Obsidian help](https://help.obsidian.md/)
