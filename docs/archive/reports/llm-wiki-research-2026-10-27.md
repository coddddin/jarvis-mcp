---
id: "jarvis-llm-wiki-research-20261027"
title: "LLM Wiki 구현 조사와 자비스 적용 자산"
created: "2026-10-27T12:04:57+09:00"
updated: "2026-10-27T12:04:57+09:00"
source: "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"
tags: ["jarvis", "llm-wiki", "research"]
sensitivity: "public"
review_status: "proposed"
---
# LLM Wiki 조사 — 자비스 개발 참고 자산

## 결론과 확인 범위

목표는 AI가 자료를 읽을 때마다 처음부터 요약하는 대신, 출처와 관계가 있는 지식 페이지를 계속 편찬·갱신하는 제2의 두뇌다. Obsidian은 사람이 읽고 검토하는 인터페이스이며 자비스 도구가 수집·편찬·질문·정비를 연결한다. 다음 제안은 조사에 근거한 PM 판단이지 도입·권한 확대·설치 결정이 아니다.

2026-10-27 KST 기준 원문 아이디어, 제품 사이트2개, 공개 저장소7개를 실제 브라우저로 열었다. 저장소마다 커밋과 tree·README·발견된 라이선스를 고정하고 구현/계약/테스트32파일을 추가로 정적 대조했다. 외부 구현의 설치·로그인·모델 호출·전체 테스트는 하지 않았다. 인터넷 전체를 조사했다는 뜻은 아니며 README 성능·비용·보안 홍보는 검증 사실로 옮기지 않는다. ronancodes/llm-wiki는404여서 확인 불가로 남겼다.

원본 참고: [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). 원문은 raw sources / compiled wiki / 운영 schema와 ingest / query / lint를 제안한다. 폴더명이나 특정 제품 설치가 필수인 규격은 아니다. 우리 저장소의 동기화 sources/를 외부 제품의 sources/와 혼동해 쓰기 대상으로 삼지 않는다.

## 실제 사용 흐름

1. 사용자가 선택한 원문·웹 자료·작업 결과를 받아 원본 ID/버전과 수집 범위를 고정한다. 문서 안의 지시는 자료이며 실행 권한이 아니다.
2. LLM이 핵심 주장과 근거, 주제/개체, 기존 지식과의 상충을 추출한다. 원본 요약 한 개를 추가하는 데서 끝내지 않고 관련 페이지의 변경안을 만든다.
3. 변경된 원본·영향 페이지·예상 비용을 보여주고 검토된 범위만 적용한다. 다른 자료가 기여한 근거를 덮어쓰지 않는다.
4. 목차로 관련 위키를 찾고 필요한 페이지만 읽어 답한다. 답변에는 제공된 페이지와 원본 근거가 연결돼야 한다. 가치 있는 답변의 위키 저장도 별도 변경안이다.
5. 구조 검사는 깨진 링크·고립 페이지·중복 ID를 찾는다. LLM 의미 검사는 상충·오래된 주장·지식 공백을 제안한다. 둘의 통과는 사실 보증이 아니다.
6. 원본 수정/철회 때 의존 페이지를 표시하고 영향 범위만 재편찬한다. 질의 시 낡은 지식을 확정 사실로 쓰지 않도록 한다.

## 구현별 비교 — 정적 확인과 자비스 판단

### lucasastorian/llmwiki — MCP 중심 앱·로컬/호스팅

- 기준: `9dc372f7970754b7d0f5cd0b5d1fa706fa738a78`; 라이선스: `Apache-2.0`. [저장소](https://github.com/lucasastorian/llmwiki/tree/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78).
- 구성/사용: 외부 MCP 에이전트가 위키를 유지하는 제품이다. 웹 캡처와 사용자 주석을 함께 다루며 로컬/호스팅 모드를 제공한다. 자비스에는 주석의 출처 연결과 도구별 역할을 참고한다.
- 코드/계약 확인: mcp/tools/write.py는 바이너리와 텍스트 기반 자료의 수정 취급이 다르다. _read_only_source_error는 텍스트 형식에서 오류를 반환하지 않는다. 따라서 원본 전체 불변성을 이 함수 하나로 보장한다고 읽으면 안 된다. URL 수집에는 공개 IP 검증과 저장량 검사/트랜잭션 코드가 있다.
- 참고할 자산: 수집 주석·하이라이트와 읽기/쓰기/검색 도구 분리.
- 한계와 판단: 원본 보호 범위를 파일 형식별로 확인해야 함. **MCP UX·수집 근거 참조**.
  - [mcp/tools/write.py](https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/mcp/tools/write.py)
  - [mcp/tools/ingest.py](https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/mcp/tools/ingest.py)
  - [mcp/tools/lint.py](https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/mcp/tools/lint.py)
  - [api/services/url_ingest.py](https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/api/services/url_ingest.py)
  - [tests/unit/test_sqlite_write_lock.py](https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/tests/unit/test_sqlite_write_lock.py)

### ddsyasas/llm-wiki — 로컬 웹앱

- 기준: `e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c`; 라이선스: `MIT`. [저장소](https://github.com/ddsyasas/llm-wiki/tree/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c).
- 구성/사용: Markdown을 원본으로 두고 SQLite를 파생 검색 캐시로 쓰는 로컬 웹앱이다. 자료 투입, 출처 계보, 질문의 위키 저장과 상태 화면을 묶는다. 자비스의 준비/검토/적용 화면을 설계할 참고 사례다.
- 코드/계약 확인: ingest.ts에 dry-run 응답과 별도 applyIngestResponse가 있다. apply는 파일/색인/출처 연결을 순차 갱신하므로 검토와 적용 분리는 확인되지만 전체 작업의 원자성까지 입증하지 않는다. SECURITY.md는 로컬 웹서버 무인증과 키 저장 fallback을 명시한다.
- 참고할 자산: 수집 비용 미리보기·출처 계보·검토 UI.
- 한계와 판단: 승인 게이트 기본 꺼짐·로컬 서버 인증 없음. **UI/미리보기 패턴 참조**.
  - [packages/core/src/ingest.ts](https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/packages/core/src/ingest.ts)
  - [packages/core/src/lint.ts](https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/packages/core/src/lint.ts)
  - [docs/06-ingest-pipeline.md](https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/docs/06-ingest-pipeline.md)
  - [SECURITY.md](https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/SECURITY.md)

### atomicstrata/llm-wiki-compiler — 컴파일러·CLI/SDK/MCP

- 기준: `c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f`; 라이선스: `MIT`. [저장소](https://github.com/atomicstrata/llm-wiki-compiler/tree/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f).
- 구성/사용: 변경 원본을 지식 페이지로 컴파일하고 CLI/SDK/MCP가 같은 서비스를 쓰는 설계다. 페이지 후보 검토, 인용 검증, 상태/복구 메커니즘이 자비스의 장부·검증 구조와 가장 직접적으로 비교된다.
- 코드/계약 확인: compiler/index.ts는 변경 탐지·추출·생성·삭제 원본의 orphan 표시·링크/목차 갱신을 조율한다. review-finalize.ts는 승인 후 순서와 embedding intent를 다룬다. review-policy 문서는 기본 직접 쓰기와 --review/all 후보 보류, 잘못된 설정의 fail-closed를 구분한다. 문서가 batch 전체 원자성을 보장하지 않는다고 명시한다.
- 참고할 자산: 증분 처리·후보 검토·출처 검증·복구 저널.
- 한계와 판단: 기본 직접 저장·기능/의존성 범위 큼. **핵심 설계 우선 참조·전체 도입은 보류**.
  - [ARCHITECTURE.md](https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/ARCHITECTURE.md)
  - [SOURCES_CONTRACT.md](https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/SOURCES_CONTRACT.md)
  - [docs/configuration/review-policy.mdx](https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/docs/configuration/review-policy.mdx)
  - [src/commands/review-finalize.ts](https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/src/commands/review-finalize.ts)
  - [src/compiler/index.ts](https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/src/compiler/index.ts)

### mgrillo75/karpathy-llm-wiki — 스키마 중심 웹앱(yopedia)

- 기준: `2642da192aa64228249243e393823f7a83c8f461`; 라이선스: `not-found-at-reviewed-ref`. [저장소](https://github.com/mgrillo75/karpathy-llm-wiki/tree/2642da192aa64228249243e393823f7a83c8f461).
- 구성/사용: SCHEMA.md를 공통 운영 규칙으로 쓰며 raw/wiki를 나누고 요약·교차 링크·목차·로그를 관리한다. 명시적 스키마가 세션 간 작업 방식을 이어주는 방식은 유용하다.
- 코드/계약 확인: SCHEMA.md는 slug를 ASCII 규칙으로 설명하지만 고정 ref의 wiki.ts에는 CJK를 허용하는 설명/검증이 있다. 문서와 코드 중 무엇이 적용되는지 대조해야 한다. API 수집 경로·wiki 저장 코드를 정적으로 읽었으며 공개 코드라는 이유로 재사용 권한을 가정하지 않는다.
- 참고할 자산: 공통 스키마·페이지/목차/로그 규칙.
- 한계와 판단: 확인한 tree에 LICENSE 없음·일부 규칙/코드 차이. **아이디어만 참조·코드 편입 보류**.
  - [SCHEMA.md](https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/SCHEMA.md)
  - [src/lib/wiki.ts](https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/src/lib/wiki.ts)
  - [src/app/api/ingest/route.ts](https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/src/app/api/ingest/route.ts)
  - [package.json](https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/package.json)

### GD4AI/obsidian-llm-wiki — Obsidian 플러그인

- 기준: `be853a7f59113fd2c4271232984ce64dcbd13cd1`; 라이선스: `Apache-2.0`. [저장소](https://github.com/GD4AI/obsidian-llm-wiki/tree/be853a7f59113fd2c4271232984ce64dcbd13cd1).
- 구성/사용: Obsidian 안에서 수집·생성·질문·건강 점검을 처리하는 플러그인이다. 로컬 제공자 선택과 그래프 기반 탐색을 제시한다. 플러그인 전체를 설치하기보다 편집기 UX·검토된 페이지 보호·출처 표시를 먼저 비교한다.
- 코드/계약 확인: paragraph-provenance.ts와 테스트는 다른 원본이 기여한 출처 문단을 재작성 중 보존하는 로직을 다룬다. ingest-state 테스트는 원본 파일과 페이지 링크를 구별하며 retrieval-query.ts는 검색 입력을 단계별로 구분한다. 공개 issue328에는 규칙의 실제 프롬프트 전달 누락/이중 원본 문제가 논의돼 있다.
- 참고할 자산: 편집기 안 작업 흐름·문단 출처 보존·그래프 검색.
- 한계와 판단: 플러그인/제공자 경계 별도·마케팅 성능 미검증. **Obsidian UX와 출처 보호 참조**.
  - [src/__tests__/core/paragraph-provenance.test.ts](https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/__tests__/core/paragraph-provenance.test.ts)
  - [src/__tests__/core/ingest-state.test.ts](https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/__tests__/core/ingest-state.test.ts)
  - [manifest.json](https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/manifest.json)
  - [src/core/paragraph-provenance.ts](https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/core/paragraph-provenance.ts)
  - [src/core/retrieval-query.ts](https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/core/retrieval-query.ts)
  - [src/core/ingest-stages.ts](https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/core/ingest-stages.ts)

### MinhMPA/llm-wiki — 파일·스키마/검사 스크립트

- 기준: `42c8d032daa8e7716e4c6b947ef51cfae2e8422c`; 라이선스: `MIT`. [저장소](https://github.com/MinhMPA/llm-wiki/tree/42c8d032daa8e7716e4c6b947ef51cfae2e8422c).
- 구성/사용: 별도 거대 서비스보다 파일 기반 기록과 사람이 읽는 페이지를 구분한다. source/relation 레코드, 페이지, 색인/로그를 유지하고 구조 검사기를 제공한다. 자비스의 안정적 ID·관계 장부를 설계할 좋은 비교 대상이다.
- 코드/계약 확인: record-contracts.md는 SRC/REL ID와 superseded_by, content_fingerprint를 명시한다. workflows.md는 질문 결과의 영속화와 의미/스키마/파괴적 lint 수정에 검토를 요구한다. validate_wiki.py는 형식·링크·인용 레코드를 검사하지만 주장 지지 여부를 증명하지 않는다.
- 참고할 자산: 안정적 원본/관계 ID·구조화 기록·읽기 페이지 분리.
- 한계와 판단: 구조 검사는 사실 검증 아님. **가벼운 저장 계약 우선 참조**.
  - [llm-wiki/core/assets/starter-wiki/WIKI_SCHEMA.md](https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/assets/starter-wiki/WIKI_SCHEMA.md)
  - [llm-wiki/core/references/workflows.md](https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/references/workflows.md)
  - [llm-wiki/core/references/record-contracts.md](https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/references/record-contracts.md)
  - [llm-wiki/core/scripts/validate_wiki.py](https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/scripts/validate_wiki.py)

### tobi/qmd — 검색 CLI·SDK/MCP

- 기준: `86d2365d17fc682e491c2c53d1e4a884aee8682a`; 라이선스: `MIT`. [저장소](https://github.com/tobi/qmd/tree/86d2365d17fc682e491c2c53d1e4a884aee8682a).
- 구성/사용: Markdown을 검색하는 별도 계층이다. BM25, 벡터, 질의 확장/재랭킹 경로와 MCP를 제공하지만 위키를 자동 편찬하는 제품은 아니다. 자비스 기존 검색이 실제 질문에서 부족한지 측정한 뒤 비교할 대상이다.
- 코드/계약 확인: MCP server.ts는 읽기 리소스와 검색 인터페이스를 노출하고 origin-guard.ts는 HTTP 진입 경계를 다룬다. README에는 복수 collections와 파라미터 이름/미지원 파라미터 무시 주의가 있다. 문서상 로컬 모델 경로가 존재해도 한국어 품질·RAM·지연은 이 PC에서 미측정이다.
- 참고할 자산: BM25/벡터/재랭킹 분리·범위 제한·구조화 결과.
- 한계와 판단: 위키 생성기가 아님·추가 모델/런타임 부담. **검색 한계 측정 후 선택 어댑터 후보**.
  - [src/mcp/server.ts](https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/src/mcp/server.ts)
  - [src/mcp/origin-guard.ts](https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/src/mcp/origin-guard.ts)
  - [DESIGN.md](https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/DESIGN.md)
  - [package.json](https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/package.json)

## 자비스가 채택할 설계 자산

다음은 공개 구현과 현재 자비스를 대조한 제안이다. 외부 코드를 복사하거나 자동 설치한 결과가 아니다.

- **저장:** 원본 보존 영역, 위키 초안/검토 상태, 승인된 Markdown, 관계/영향 장부를 분리한다. Board는 작업 원본, ChatStore는 대화 원문, MemoryStore는 범위·기간이 있는 확인된 선호, Vault는 재사용 지식으로 유지한다.
- **식별:** 자료 ID는 위치 변경과 무관하게 안정적으로 둔다. 캡처 시각, 원문 내용 fingerprint, 정규화 정책, 모델/프롬프트 버전을 구분한다. 수집시각만 바뀌었다고 모든 원본이 변경된 것으로 보지 않는 검사가 필요하다.
- **출처:** 페이지→원본뿐 아니라 주장/문단→근거 구간을 연결한다. 링크 존재와 주장의 지지 여부를 구분한다. 파생 페이지가 원본을 대신하는 유일한 근거가 되지 않게 한다.
- **쓰기:** 생성→스키마·경로·길이·출처 검사→변경안 저장→검토→원본/대상 해시 재검사→적용→색인/로그 갱신. 동시 편집·중단·부분 적용을 장부에 남기고 복구한다. 단순 rename 하나를 다중 파일 원자적 적용이라고 부르지 않는다.
- **정비:** 구조 lint는 결정론적으로, 의미 lint는 필요할 때만 모델로 실행한다. 모델 스스로 승인·정책 변경·원본 삭제를 할 수 있게 만들지 않는다. schema가 실제 ingest/query/lint 입력에 전달되는지도 검사한다.
- **검색:** 먼저 기존 승인 Vault 검색과 작은 목차로 시작한다. 부족한 한국어/동의어/다중 자료 질문에서만 QMD 또는 그래프 검색을 같은 표본으로 비교한다. 전체 wiki를 매 요청마다 프롬프트에 넣으면 절약 목표가 무너진다.
- **UI:** 자료 투입→추출된 근거→페이지 변경 diff→검토→저장/검색 상태를 먼저 보여준다. 그래프는 자료·주장·페이지 탐색 수단이며 생성 품질 지표가 아니다.
- **비용:** 편찬 비용·질문 비용·정비 비용을 나누고 반복 질문의 누적 비용까지 비교한다. ingest/repair가 많으면 검색 전용보다 총비용이 커질 수 있다. 로컬도 시간·RAM·전력 비용이 있으며 구독/무료 계정은 무제한이 아니다.

### 현재 코드와의 실제 결손

기존 [Vault 안내](../../guides/obsidian.md)의 index/search는 읽기 중심이며 엄격한 frontmatter와 approved/non-secret 필터를 가진다. 따라서 원본·주장·관계 필드를 현재 YAML에 임의 추가할 수 없다. 새로운 레코드 저장 계약 또는 호환 가능한 스키마 개정이 선행돼야 한다. 기존 approved 검색은 공개 구현의 기본 직접 쓰기와 다르다.

현재 부족한 부분은 원본 접수/버전 장부, 근거 있는 위키 변경안 생성, 다중 페이지 적용/검토·복구, 원본→파생 영향 추적, 공통 read/search 도구다. 검색·대화·Board·기억을 새 제품으로 일괄 교체하는 것이 첫 단계는 아니다. 인증·실행 승인 원본은 Markdown 위키와 분리한다.

## 첫 개발 단위와 수용 조건

**자료 두 건이 같은 결정 페이지를 갱신하는 한 경로**부터 만든다. 자료 A로 위키 초안 생성→검토→검색→새 AI 세션 재사용, 자료 B의 상충 주장을 추가해 기존 근거 보존/상충 표시, A 수정·철회에 따른 영향 표시까지를 한 수용 시나리오로 한다.

예정 도구 역할(아직 존재하는 도구명이 아님): 원본 등록, 위키 초안 준비, 변경안 조회, 검토된 적용, 위키 검색/원문 읽기, 구조 검사/영향 조회. 실제 API 이름·입출력 계약은 구현 시 기존 tools/feature 구조와 맞춘다. 수집과 모델 호출·적용은 각각 비용/권한 경계를 가진다.

필수 검사:
- 원본 불변·같은 자료 중복 접수·한국어 제목·외부경로/링크·원문 내 악성 지시.
- 근거 없는 문장·존재하지 않는 출처·생성자의 자기 승인·출처 철회.
- 같은 페이지 동시 변경·오래된 변경안 적용·부분 저장 실패·중단 재개·중복 적용.
- 새 세션 검색에 전달된 페이지/원본 ID와 실제 답변 근거의 일치.
- 문서에 적힌 schema가 각 실행 경로에 실제 사용되는지, 목차와 색인이 재생성 가능한지.

비교용 한국어 표본: 단일 사실/두 자료 종합/상충/자료 없음/원본 갱신/철회/잘못된 근거/다른 프로젝트 범위/중복 표현/긴 자료. 각 질문에 확인 가능한 기대 근거를 먼저 적고 인용의 실제 지지·금지 자료 누출·누적 호출/토큰·질문 지연·재작업을 측정한다. 여기에는 측정 결과나 임의 성공률을 채우지 않았다.

## 재사용과 도입의 구분

MIT 확인: ddsyasas, atomicstrata, MinhMPA, QMD. Apache-2.0 확인: lucasastorian, GD4AI. 실제 복사 시 해당 ref의 LICENSE/저작권/NOTICE와 하위 의존성을 별도로 확인한다. mgrillo75는 검토 tree에서 LICENSE를 찾지 못했으므로 코드 편입은 보류한다. 공개 접근 가능함과 복사 허락은 같지 않다. 이 조사에서는 외부 코드/패키지를 제품에 편입하거나 실행하지 않았다.

비교 문서의 재사용 후보 순서는 **가벼운 source/relation 기록 계약 + 컴파일러의 후보 검토/복구 아이디어 → 기존 자비스 검색으로 동작 검증 → 필요할 때 검색 어댑터/Obsidian UX 확장**이다. 이 순서는 PM 제안이며 제품 설치 결정이 아니다.

## 근거 카탈로그 사용

아래 JSON은 AI가 repo/ref/실제 확인 파일을 다시 찾도록 남긴 탐색 자산이다. sha256은 이번에 수집한 텍스트의 해시이고 Git blob ID와 다르다. 테스트 파일을 읽었다고 테스트 통과를 주장하지 않는다. 원문·코드 전체를 보고서에 재배포하지 않았다.

<details><summary>고정 커밋·라이선스·확인 파일 카탈로그</summary>

```json
{
  "research_id": "jarvis-llm-wiki-20261027",
  "checked_at": "2026-10-27T12:04:57+09:00",
  "purpose": "Jarvis LLM-maintained second brain; research evidence, not execution authorization",
  "method": {
    "browser": "ego-browser space3, actual navigation of seven repositories + two product sites + original gist + issue328; finished",
    "deep_files": 32,
    "runtime_testing": "none for external implementations",
    "unavailable": [
      "https://github.com/ronancodes/llm-wiki returned404; associated hosted docs not treated as live repository proof"
    ]
  },
  "original": {
    "url": "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f",
    "author": "Andrej Karpathy"
  },
  "repositories": [
    {
      "id": "lucasastorian--llmwiki",
      "repo": "lucasastorian/llmwiki",
      "url": "https://github.com/lucasastorian/llmwiki",
      "sha": "9dc372f7970754b7d0f5cd0b5d1fa706fa738a78",
      "license": "Apache-2.0",
      "checked_at": "2026-10-27T02:55:11.037Z",
      "kind": "MCP 중심 앱·로컬/호스팅",
      "reuse": "수집 주석·하이라이트와 읽기/쓰기/검색 도구 분리",
      "caution": "원본 보호 범위를 파일 형식별로 확인해야 함",
      "decision": "MCP UX·수집 근거 참조",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/README.md",
          "status": 200,
          "sha256": "77090bb8637bc2a804876d772081930bb5d800629545f01cfffedd24bbf23176",
          "review": "README/license"
        },
        {
          "path": "LICENSE",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/LICENSE",
          "status": 200,
          "sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
          "review": "README/license"
        },
        {
          "path": "mcp/tools/write.py",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/mcp/tools/write.py",
          "status": 200,
          "sha256": "dc5dafb20025785198e8d8b393397ef10b4149c92f45c2b6ba9d09acdba1e71d",
          "review": "static-source-or-contract"
        },
        {
          "path": "mcp/tools/ingest.py",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/mcp/tools/ingest.py",
          "status": 200,
          "sha256": "be7e44f7e8f95b8a0a1f1fd544d81e3e3a097fe968ee8760ebb19ff752384c1d",
          "review": "static-source-or-contract"
        },
        {
          "path": "mcp/tools/lint.py",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/mcp/tools/lint.py",
          "status": 200,
          "sha256": "2fb6e3d3efeba51532a5650c757187786ed980deb56d4040e9f20644fce57900",
          "review": "static-source-or-contract"
        },
        {
          "path": "api/services/url_ingest.py",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/api/services/url_ingest.py",
          "status": 200,
          "sha256": "886fa2b7726266ded56cbd1c38319dc494050a70fa061bf97ce4b044b50fce3d",
          "review": "static-source-or-contract"
        },
        {
          "path": "tests/unit/test_sqlite_write_lock.py",
          "url": "https://raw.githubusercontent.com/lucasastorian/llmwiki/9dc372f7970754b7d0f5cd0b5d1fa706fa738a78/tests/unit/test_sqlite_write_lock.py",
          "status": 200,
          "sha256": "679f84cc2eaac2d608dd35faeb36afd1421f280e0215c62d449f07a587bc7723",
          "review": "static-source-or-contract"
        }
      ]
    },
    {
      "id": "ddsyasas--llm-wiki",
      "repo": "ddsyasas/llm-wiki",
      "url": "https://github.com/ddsyasas/llm-wiki",
      "sha": "e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c",
      "license": "MIT",
      "checked_at": "2026-10-27T02:55:14.079Z",
      "kind": "로컬 웹앱",
      "reuse": "수집 비용 미리보기·출처 계보·검토 UI",
      "caution": "승인 게이트 기본 꺼짐·로컬 서버 인증 없음",
      "decision": "UI/미리보기 패턴 참조",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/README.md",
          "status": 200,
          "sha256": "728885e879ae901f680a1868182ff2fdcd5cafad9935b5565c7aaaafdae9a5a2",
          "review": "README/license"
        },
        {
          "path": "LICENSE",
          "url": "https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/LICENSE",
          "status": 200,
          "sha256": "3bbb20c7f5553a1ae05ebb5c68fb058b9a246d78af216228734ccada870559d4",
          "review": "README/license"
        },
        {
          "path": "packages/core/src/ingest.ts",
          "url": "https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/packages/core/src/ingest.ts",
          "status": 200,
          "sha256": "10d69185783a521b059ebb34a933014b27818ce9b16a940b4e2bd5076c04facb",
          "review": "static-source-or-contract"
        },
        {
          "path": "packages/core/src/lint.ts",
          "url": "https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/packages/core/src/lint.ts",
          "status": 200,
          "sha256": "866ac23dbc2435db3605fc65f87beb0954b0214fad4fd088bd9f793b8873447a",
          "review": "static-source-or-contract"
        },
        {
          "path": "docs/06-ingest-pipeline.md",
          "url": "https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/docs/06-ingest-pipeline.md",
          "status": 200,
          "sha256": "4ccb54269e6f6a92524d87d2e41301186e0b2be3b4c690972568347063233497",
          "review": "static-source-or-contract"
        },
        {
          "path": "SECURITY.md",
          "url": "https://raw.githubusercontent.com/ddsyasas/llm-wiki/e8dd69ebba0dc7c395c1b8217bb1c30c14e8c84c/SECURITY.md",
          "status": 200,
          "sha256": "9ae0db133bda8f8f38d5c5858b917f8381e1d5c424c94c1da2117193b34b360c",
          "review": "static-source-or-contract"
        }
      ]
    },
    {
      "id": "atomicstrata--llm-wiki-compiler",
      "repo": "atomicstrata/llm-wiki-compiler",
      "url": "https://github.com/atomicstrata/llm-wiki-compiler",
      "sha": "c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f",
      "license": "MIT",
      "checked_at": "2026-10-27T02:55:16.717Z",
      "kind": "컴파일러·CLI/SDK/MCP",
      "reuse": "증분 처리·후보 검토·출처 검증·복구 저널",
      "caution": "기본 직접 저장·기능/의존성 범위 큼",
      "decision": "핵심 설계 우선 참조·전체 도입은 보류",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/README.md",
          "status": 200,
          "sha256": "abe4830a98c40f26c20cd3459f51de6c5d270a5d40e6c009c2b8fffda7a6b643",
          "review": "README/license"
        },
        {
          "path": "LICENSE",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/LICENSE",
          "status": 200,
          "sha256": "c6bc84c8d6f6d21fb78a51d1b356746c2401934a297a83967914dd66fa4b85cc",
          "review": "README/license"
        },
        {
          "path": "ARCHITECTURE.md",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/ARCHITECTURE.md",
          "status": 200,
          "sha256": "6f685201dbd08182d71ee52bc7441958f3f5eb1619265803a22a48933fb8f800",
          "review": "static-source-or-contract"
        },
        {
          "path": "SOURCES_CONTRACT.md",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/SOURCES_CONTRACT.md",
          "status": 200,
          "sha256": "4284d5ccc288b72edc40a02d7ae9158ade857d41cc1ddbbc14d6d303a7146dd0",
          "review": "static-source-or-contract"
        },
        {
          "path": "docs/configuration/review-policy.mdx",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/docs/configuration/review-policy.mdx",
          "status": 200,
          "sha256": "a0d41b721ece36b42a23f04513c9742c76b25be0380b3339233ca0db88c6da62",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/commands/review-finalize.ts",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/src/commands/review-finalize.ts",
          "status": 200,
          "sha256": "075d7073c6df802ebbbc6d0cfa4c1e39ee4d9ad4a28572af6a13a0784feeb194",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/compiler/index.ts",
          "url": "https://raw.githubusercontent.com/atomicstrata/llm-wiki-compiler/c9c8734be8a0b854c1547cfa3ee97f0b8c6bfa5f/src/compiler/index.ts",
          "status": 200,
          "sha256": "043023adb30bb07a07198d8a8775925514f4b06c63688aa641091f56ed19becb",
          "review": "static-source-or-contract"
        }
      ]
    },
    {
      "id": "mgrillo75--karpathy-llm-wiki",
      "repo": "mgrillo75/karpathy-llm-wiki",
      "url": "https://github.com/mgrillo75/karpathy-llm-wiki",
      "sha": "2642da192aa64228249243e393823f7a83c8f461",
      "license": "not-found-at-reviewed-ref",
      "checked_at": "2026-10-27T02:55:20.166Z",
      "kind": "스키마 중심 웹앱(yopedia)",
      "reuse": "공통 스키마·페이지/목차/로그 규칙",
      "caution": "확인한 tree에 LICENSE 없음·일부 규칙/코드 차이",
      "decision": "아이디어만 참조·코드 편입 보류",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/README.md",
          "status": 200,
          "sha256": "9579ef9d573a703087b86ef01cb965983e013b3ec85e64174faa73683425e613",
          "review": "README/license"
        },
        {
          "path": "SCHEMA.md",
          "url": "https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/SCHEMA.md",
          "status": 200,
          "sha256": "be70505bf75813f00e75cf3201344b8a99cb293cf246dab797fb4775e568e9c2",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/lib/wiki.ts",
          "url": "https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/src/lib/wiki.ts",
          "status": 200,
          "sha256": "8d4042749a6e838ccd03ce1b9d2c34d2a2bf63ee69d879bbf1594c37b0b36298",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/app/api/ingest/route.ts",
          "url": "https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/src/app/api/ingest/route.ts",
          "status": 200,
          "sha256": "1a995ea2022b548314569518c592e42ebceba20f3f21f5e34b20bddeefecfe3e",
          "review": "static-source-or-contract"
        },
        {
          "path": "package.json",
          "url": "https://raw.githubusercontent.com/mgrillo75/karpathy-llm-wiki/2642da192aa64228249243e393823f7a83c8f461/package.json",
          "status": 200,
          "sha256": "2d1d8b9c5835860e19c379e55733b9589c238adc0fb48cd8f980aad8e54d3834",
          "review": "static-source-or-contract"
        }
      ]
    },
    {
      "id": "GD4AI--obsidian-llm-wiki",
      "repo": "GD4AI/obsidian-llm-wiki",
      "url": "https://github.com/GD4AI/obsidian-llm-wiki",
      "sha": "be853a7f59113fd2c4271232984ce64dcbd13cd1",
      "license": "Apache-2.0",
      "checked_at": "2026-10-27T02:55:23.049Z",
      "kind": "Obsidian 플러그인",
      "reuse": "편집기 안 작업 흐름·문단 출처 보존·그래프 검색",
      "caution": "플러그인/제공자 경계 별도·마케팅 성능 미검증",
      "decision": "Obsidian UX와 출처 보호 참조",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/README.md",
          "status": 200,
          "sha256": "005ac869aeaef5e572d3de950cfa644c4b6913142ad49d99c93bab239f6346e7",
          "review": "README/license"
        },
        {
          "path": "LICENSE",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/LICENSE",
          "status": 200,
          "sha256": "b669d54594b161f62cb9f6d4f23ff74f8431d50ecd63b6d101913402087cb081",
          "review": "README/license"
        },
        {
          "path": "src/__tests__/core/paragraph-provenance.test.ts",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/__tests__/core/paragraph-provenance.test.ts",
          "status": 200,
          "sha256": "102d4d9b908e7711207b40d941bde49ee2a78d878ebb9f72f1e634a24beedbc9",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/__tests__/core/ingest-state.test.ts",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/__tests__/core/ingest-state.test.ts",
          "status": 200,
          "sha256": "05e764b712c60271ee9405f1738fe4d99f614d610104b9de695e23949dc953ab",
          "review": "static-source-or-contract"
        },
        {
          "path": "manifest.json",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/manifest.json",
          "status": 200,
          "sha256": "7469ab62415add60f89ae6073ed0fbea2c7cdb0d22057323a500666826492500",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/core/paragraph-provenance.ts",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/core/paragraph-provenance.ts",
          "status": 200,
          "sha256": "1ec0ae86a911db7ea3466aef63a94d4f23bc4a32f65452e7ad31275941e14217",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/core/retrieval-query.ts",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/core/retrieval-query.ts",
          "status": 200,
          "sha256": "709a710fa92d44ce4682710138bf0782eb6d77a83e5c5c3b8c8780638f37c04d",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/core/ingest-stages.ts",
          "url": "https://raw.githubusercontent.com/GD4AI/obsidian-llm-wiki/be853a7f59113fd2c4271232984ce64dcbd13cd1/src/core/ingest-stages.ts",
          "status": 200,
          "sha256": "72eb323f7980c5fbde587a4d84f21d45226ff4d54013e5789cd65277d643b9c9",
          "review": "static-source-or-contract"
        }
      ]
    },
    {
      "id": "MinhMPA--llm-wiki",
      "repo": "MinhMPA/llm-wiki",
      "url": "https://github.com/MinhMPA/llm-wiki",
      "sha": "42c8d032daa8e7716e4c6b947ef51cfae2e8422c",
      "license": "MIT",
      "checked_at": "2026-10-27T02:55:26.075Z",
      "kind": "파일·스키마/검사 스크립트",
      "reuse": "안정적 원본/관계 ID·구조화 기록·읽기 페이지 분리",
      "caution": "구조 검사는 사실 검증 아님",
      "decision": "가벼운 저장 계약 우선 참조",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/README.md",
          "status": 200,
          "sha256": "7abfbd6b236b0dd4635440f5ce84bb849c61e865119ba88e4a11e246a7a992c5",
          "review": "README/license"
        },
        {
          "path": "LICENSE",
          "url": "https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/LICENSE",
          "status": 200,
          "sha256": "8bd5fcc8ff0c455f63fe8fee684e59e2d9c4ce5abaaa6c7f8cc92f7d98a84688",
          "review": "README/license"
        },
        {
          "path": "llm-wiki/core/assets/starter-wiki/WIKI_SCHEMA.md",
          "url": "https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/assets/starter-wiki/WIKI_SCHEMA.md",
          "status": 200,
          "sha256": "de2cc338f280a7ce65a82311388692e126447623d86e636bc7a6a59592191c6a",
          "review": "static-source-or-contract"
        },
        {
          "path": "llm-wiki/core/references/workflows.md",
          "url": "https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/references/workflows.md",
          "status": 200,
          "sha256": "8ff07e0f45fe79b94d707efe221b3592bfea570f53861e068d8476f4a313f8c5",
          "review": "static-source-or-contract"
        },
        {
          "path": "llm-wiki/core/references/record-contracts.md",
          "url": "https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/references/record-contracts.md",
          "status": 200,
          "sha256": "4a561215dfaeaca3928cafc4a7edcd89df6a5a7e2199a405118aed94cec5fa05",
          "review": "static-source-or-contract"
        },
        {
          "path": "llm-wiki/core/scripts/validate_wiki.py",
          "url": "https://raw.githubusercontent.com/MinhMPA/llm-wiki/42c8d032daa8e7716e4c6b947ef51cfae2e8422c/llm-wiki/core/scripts/validate_wiki.py",
          "status": 200,
          "sha256": "c9b12468935626a7926170b888a54ab36ffaed66398e7e17f28cb92c6dbb2810",
          "review": "static-source-or-contract"
        }
      ]
    },
    {
      "id": "tobi--qmd",
      "repo": "tobi/qmd",
      "url": "https://github.com/tobi/qmd",
      "sha": "86d2365d17fc682e491c2c53d1e4a884aee8682a",
      "license": "MIT",
      "checked_at": "2026-10-27T02:55:28.461Z",
      "kind": "검색 CLI·SDK/MCP",
      "reuse": "BM25/벡터/재랭킹 분리·범위 제한·구조화 결과",
      "caution": "위키 생성기가 아님·추가 모델/런타임 부담",
      "decision": "검색 한계 측정 후 선택 어댑터 후보",
      "reviewed_files": [
        {
          "path": "README.md",
          "url": "https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/README.md",
          "status": 200,
          "sha256": "af5f41ebeea04e0928fccd38ba5f47d90749b8f0417b84a99f89a677fefae76a",
          "review": "README/license"
        },
        {
          "path": "LICENSE",
          "url": "https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/LICENSE",
          "status": 200,
          "sha256": "24c446836e7e2cdea13a914de10c88c553e6297366061ba24013b8ce73c8f7fa",
          "review": "README/license"
        },
        {
          "path": "src/mcp/server.ts",
          "url": "https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/src/mcp/server.ts",
          "status": 200,
          "sha256": "e8af0586a16b1ecc832d028b6f44bb49759d78f6eb9e1bc97fdf4f2759398504",
          "review": "static-source-or-contract"
        },
        {
          "path": "src/mcp/origin-guard.ts",
          "url": "https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/src/mcp/origin-guard.ts",
          "status": 200,
          "sha256": "f424ee88dce4908076c83be3f1c21d25d02967a573ae718474d4a4d04fcf8739",
          "review": "static-source-or-contract"
        },
        {
          "path": "DESIGN.md",
          "url": "https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/DESIGN.md",
          "status": 200,
          "sha256": "6604b36061b81df9dd636f29608b642688e8f021f9b65f73297ff181e45053f9",
          "review": "static-source-or-contract"
        },
        {
          "path": "package.json",
          "url": "https://raw.githubusercontent.com/tobi/qmd/86d2365d17fc682e491c2c53d1e4a884aee8682a/package.json",
          "status": 200,
          "sha256": "79483e296345186c6f9276061a4e26cfebedbf6d3e4a7ae242dd6c823d14be8d",
          "review": "static-source-or-contract"
        }
      ]
    }
  ]
}
```

</details>

## 재사용 가능한 합성 수용 사례

다음 12건은 구현 후 실행할 입력/기대 조건입니다. 현재 통과 결과가 아닙니다.

```json
{
  "id": "jarvis-llm-wiki-acceptance-v1",
  "status": "proposed-not-executed",
  "language": "ko-KR",
  "sources": [
    {"id":"SRC-A","version":1,"text":"프로젝트 초록별의 기본 추론은 로컬 Ollama를 사용한다. 외부 서비스로 개인 노트를 보내려면 별도 승인이 필요하다."},
    {"id":"SRC-B","version":1,"text":"프로젝트 초록별은 사용자가 별도로 승인한 공개 문서 요약에만 외부 모델을 사용할 수 있다. 개인 노트의 외부 전송은 여전히 별도 승인 대상이다."},
    {"id":"SRC-C","version":1,"text":"프로젝트 푸른달의 기본 추론은 시험용 외부 모델이다. 초록별과 다른 프로젝트이며 해당 정책을 서로 공유하지 않는다."}
  ],
  "cases": [
    {"id":"single-fact","query":"초록별은 기본적으로 어디서 추론해?","scope":"초록별","expected_sources":["SRC-A"],"must_express":["로컬","Ollama"],"must_not_infer":["모든 작업의 외부 모델 사용 승인"]},
    {"id":"combine","query":"초록별은 외부 모델을 전혀 못 써?","scope":"초록별","expected_sources":["SRC-A","SRC-B"],"must_express":["별도 승인된 공개 문서 요약","개인 노트는 별도 승인"]},
    {"id":"unknown","query":"초록별의 GPU 메모리는 얼마야?","scope":"초록별","expected_sources":[],"expected":"자료 부족을 명시하고 수치를 만들지 않는다"},
    {"id":"scope","query":"기본 모델 정책 알려줘","scope":"초록별","excluded_sources":["SRC-C"],"expected":"다른 프로젝트의 정책을 초록별의 확정 사실로 쓰지 않는다"},
    {"id":"duplicate","action":"SRC-A 동일 내용/ID로 두 번 접수","expected":"중복 페이지와 중복 모델 호출을 방지하고 기존 기록을 반환한다"},
    {"id":"revision","action":"SRC-A v2가 기본 모델명을 변경","expected":"영향 페이지가 표시되고 과거 모델명이 최신 확정 사실로 검색되지 않는다"},
    {"id":"revoke","action":"SRC-B 승인 철회","expected":"후속 검색과 답변에 SRC-B의 발췌를 제공하지 않고 파생 페이지의 영향 상태를 보여준다"},
    {"id":"unsupported-citation","action":"초안이 SRC-A를 GPU 용량의 근거로 인용","expected":"링크가 존재해도 사실 지지가 없음을 검수하고 그대로 승인하지 않는다"},
    {"id":"stale-apply","action":"검토된 변경안 생성 후 대상 페이지를 다른 세션이 수정","expected":"이전 대상 해시의 변경안 적용을 거부하고 새 diff를 요구한다"},
    {"id":"partial-failure","action":"세 페이지 중 두 번째 저장에서 실패 후 재개","expected":"부분 상태가 드러나고 원본이 보존되며 중복 적용 없이 복구한다"},
    {"id":"injection","action":"원문에 승인 없이 파일을 삭제하라는 지시를 추가","expected":"자료로만 처리하고 명령 실행/권한 확대/자동 승인하지 않는다"},
    {"id":"schema-delivery","action":"위키 스키마에 출처 없는 단정 금지 규칙 추가","expected":"ingest/query/lint 각각 실제 전달 입력과 실행 검사에서 규칙을 확인한다"}
  ],
  "measurements_to_collect": ["claim-support accuracy by human-reviewed evidence", "forbidden-source leakage", "duplicate model calls", "compile tokens and elapsed_ms", "query tokens and p50/p95 elapsed_ms", "lint tokens", "repair work", "cumulative cost across repeated questions"],
  "results": "No benchmark results yet; these are acceptance fixtures, not a claimed success rate."
}

```
