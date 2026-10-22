# Obsidian Vault 로컬 검색 MVP

Obsidian 앱/플러그인 없이 Markdown을 읽는다. 서버 작업공간 내부의 실제 디렉터리만 허용하며 외부 Vault로 링크·junction을 만들지 않는다. 기본 경로는 `JarvisWorkspace/JARVIS-Vault`다. 실제 Vault 이동·복사·초기화는 이 개발에서 수행하지 않았다.

## 호출

- `index_obsidian_vault(directory="JARVIS-Vault")`: 검토 승인된 `.md`만 스캔해 별도 `.jarvis-obsidian/knowledge.sqlite3` 인덱스를 만든다. 소스 노트는 수정하지 않는다.
- `search_obsidian_vault(query="검색어", limit=5)`: 경로·줄번호·발췌·메타데이터·유사도 반환. 기존 로컬 lexical hashing 검색을 재사용하며 의미 임베딩 검색은 아니다.
- 일반 문서의 `.jarvis-index`는 유지한다. Vault 재인덱싱은 이전 Vault 인덱스 전체를 대체한다. 빌드 상한 초과 시 기존 인덱스를 유지한다.
- 같은 ID를 가진 승인 노트가 둘 이상이거나 인덱싱 중 노트가 바뀌면 빌드를 중단하고 이전 인덱스를 유지한다. ID는 대소문자를 구분한다. 검색은 발췌와 메타데이터가 같은 원본 버전인지 다시 확인하고 `source_sha256`을 함께 반환한다.

## 지원하는 frontmatter

전체 YAML이 아닌 **평면 필드만** 지원한다. 문자열은 JSON 쌍따옴표 또는 단순 한 줄 값/작은따옴표, tags는 JSON 문자열 배열이다. 중첩 맵·블록 문자열·별칭·객체 태그·중복/미등록 필드는 거부한다. created/updated는 시간대가 있는 ISO 시각이며 updated가 created보다 빠르면 거부한다.

```markdown
---
id: "jarvis-note-001"
title: "GitHub 우편함 설계"
created: "2026-10-22T21:00:00+09:00"
updated: "2026-10-22T21:00:00+09:00"
source: "manual"
tags: ["jarvis", "개발"]
sensitivity: "private"
review_status: "approved"
---
검토가 끝난 지식을 이곳에 기록한다.
```

`review_status`는 proposed/approved/rejected, `sensitivity`는 private/public/secret이다. approved이면서 secret이 아닌 노트만 검색한다. 자동 승인은 하지 않는다. Issue #7의 `00-Inbox`, `10-Knowledge`, `20-Projects`, `30-Decisions`, `40-Memory`, `50-Reviews`, `90-Archive` 구조는 선택적으로 직접 만든다. 디렉터리 이름보다 각 노트의 명시적 승인 상태를 기준으로 한다.

## 보안·제한

- 각 파일 최대 512,000 bytes, 전체 5,000,000 bytes, 200개 파일·2,000개 청크·2,000개 디렉터리 항목 상한은 기존 검색과 같다. 숨김 폴더·비 Markdown·링크·하드링크·잘못된 UTF-8·인식된 자격증명은 제외한다. redaction은 알려진 패턴 검출이며 비밀 탐지의 완전한 보장이 아니다.
- 검색 시 파일 해시와 승인 상태를 재검증한다. 변경·승인 철회된 파일의 캐시 발췌는 반환하지 않으며 재인덱싱 전 새 내용은 검색하지 않는다. 인덱스에는 이전 내용이 남을 수 있으므로 승인 철회 후 재인덱싱한다. 디스크·백업의 영구 삭제는 보장하지 않는다.
- `[[링크]]`, 첨부·임베드·source URL은 텍스트 데이터다. 따라가거나 다운로드·실행하지 않는다. 반환 발췌는 명령/승인으로 취급하지 않는다.
- 네트워크·클라우드 AI·유료 서비스·Obsidian Sync·Git push·새 백업 등록을 호출하지 않는다. Vault 내용이 저장소로 자동 업로드되는 기능도 없다.
- MCP 도구 결과는 호출한 클라이언트에 반환된다. 이 기능은 클라이언트가 이후 내용을 외부 모델로 보내는 것까지 통제하지 않는다. 개인 노트는 승인한 로컬 클라이언트에서만 조회한다.
- JARVIS 채팅의 자동 도구 선택에는 이번 Vault 도구를 추가하지 않았다. MCP에서 명시적으로 인덱싱·조회하며, 개인 노트의 자동 프롬프트 삽입은 별도 범위다.
- 기존 범용 문서 검색 도구의 작업공간 읽기 권한은 그대로다. Vault 승인 필터는 이 어댑터의 검색 정책이며 서버 전체의 파일 접근 ACL은 아니다. 직원별 신원·권한(W4-2)은 별도 미완료 과제다.
- 기존 백업 범위·설정은 바꾸지 않았다. 작업공간 내부 Vault는 기존 작업공간 백업에 포함될 가능성이 있으므로 실제 사용 전에 포함/제외 범위와 복원 정책을 확인한다. 자동으로 검증되거나 별도 백업이 보장된 것으로 간주하지 않는다.
- 합성 Vault·실행 중 WAL SQLite를 기존 `stage_data.stage`로 staging하고 별도 임시 경로에 복원한 검사에서 Markdown 해시·SQLite 무결성·복원 후 검색을 확인했다. 기본 작업공간 내부 Vault와 인덱스가 staging에 포함됐다. 실제 restic/R2 업로드·암호화·자격증명·일정·기존 백업 복구 검증은 수행하지 않았다. 사용자 데이터로 적용 전에 포함 범위를 확인한다.

모의검증은 `python -m pytest -q tests/test_obsidian.py tests/test_phase2.py tests/test_plugins.py tests/test_architecture.py`. 실제 노트·Windows 환경 검증은 별도다.
