# Phase 2 — 로컬 지식 검색과 Google 브리핑

[문서 안내](../README.md) · [현재 계획](../PLAN.md)

## 현재 상태

로컬 문서 검색과 Google 읽기 전용 브리핑을 사용할 수 있습니다. 2026-10-17 전용 Cloud 프로젝트(`global-student-511819-q6`)의 Gmail·Calendar API와 본인 테스트 계정을 설정했습니다. 사용자가 로컬 인증 JSON을 저장하고 직접 동의를 완료했으며, 실제 자비스 MCP 호출에서 두 API 모두 정상 응답했습니다. 당일 일정은 0개, 현재 읽지 않은 받은편지함 메일은 검증 상한 1개가 반환됐습니다(추가 페이지 있음). 제목·발신자·토큰은 검증 보고서에 저장하지 않았습니다. 브라우저 HUD·음성 기능은 Phase 3입니다.

## 로컬 검색 사용

1. `JarvisWorkspace/documents/`에 검색할 UTF-8 `.md`, `.txt`, `.rst` 파일을 넣습니다. 시작 안내 예제 한 개가 포함되어 있습니다.
2. Codex에서 자비스 서버를 다시 연결해 새 도구 목록을 불러옵니다. 도구가 보이지 않으면 Codex 앱을 재시작합니다.
3. “자비스로 documents 폴더를 인덱싱해줘”라고 요청합니다. `index_local_documents(directory="documents")`가 실행됩니다.
4. “자비스 문서에서 로컬 벡터 검색 방식을 찾아줘”라고 요청합니다. `search_local_documents(query="로컬 벡터 검색", limit=5)`가 출처·줄 번호·점수·발췌문을 반환합니다.
5. 파일 추가·수정·삭제 후 다시 인덱싱합니다. 인덱싱은 지정 디렉터리의 전체 스냅샷으로 이전 인덱스 전체를 교체합니다. 여러 폴더는 공통 상위 폴더를 지정해야 합니다.
6. 검색용 사본을 없애려면 `clear_local_index()`를 호출합니다. 원본 문서는 삭제하지 않습니다.

### 벡터화 방식과 한계

외부 전송이나 모델 다운로드가 없는 해시 특징 벡터(4096차원 희소 벡터)와 코사인 유사도를 사용합니다. 한글·영문 단어와 문자 bigram을 사용하며 유니코드를 정규화합니다. 의미 기반 신경망 임베딩은 아니므로 동의어·다국어 의미 검색에는 제한이 있고 해시 충돌로 관련성이 낮은 결과가 나올 수 있습니다. 점수는 관련성 참고값입니다. 결과가 없어도 관련 문서가 존재하지 않는다고 단정해서는 안 됩니다.

문서는 1200자 조각, 200자 중복으로 나눕니다. 반환 발췌문은 조각의 앞 800자이며 줄 번호는 조각 시작 기준입니다. 검색은 요약 생성이나 질의응답 모델 호출을 수행하지 않습니다. 원문 내 지시는 실행하지 않으며 모든 발췌문을 신뢰하지 않는 데이터로 표시합니다.

### 접근 범위·저장·실패 정책

- 입력 경로는 기존 `JarvisWorkspace` 내부로 제한합니다. 외부 문서는 승인된 복사본을 넣어 사용하세요.
- 숨김 파일·숨김 디렉터리는 제외하며 `.jarvis-index`는 검색 대상이 아닙니다. 링크·정션·하드링크 문서와 잘못된 인코딩은 거부합니다. PDF·DOCX·이미지는 아직 지원하지 않습니다.
- 문서별 최대 512,000바이트, 전체 5,000,000바이트, 문서 최대 200개, 조각 최대 2000개, 탐색 항목 최대 2000개입니다. 디렉터리를 열거하는 운영체제 호출 자체에 대한 하드 메모리·실행시간 격리는 아직 없습니다.
- 처리할 수 없는 파일은 `skipped`에 표시합니다. 총량 제한 초과나 순회 실패 시 이전 인덱스를 유지하며 오류를 반환합니다. 제외된 파일이 있으면 완전한 인덱싱으로 간주하지 마세요.
- SQLite 인덱스는 **원문 사본을 포함하는 암호화되지 않은 로컬 데이터**입니다. 사용자만 접근할 수 있는 작업공간에 보관하세요. 검색 시 원문의 SHA-256을 다시 확인하며 변경·삭제·접근 거부된 문서는 `stale_documents`로 알리고 옛 발췌문을 반환하지 않습니다. 새 파일은 재인덱싱 전에는 검색되지 않습니다.
- `clear_local_index`는 SQLite 삭제·재정리를 수행하지만 디스크·백업 전체에 대한 안전한 영구 삭제를 보장하지 않습니다.
- 경로 정책은 OS 샌드박스가 아니며 다른 프로세스의 동시 파일시스템 변경 공격에 대한 기존 제한도 유지됩니다.

## Google 계정 연결 준비

Codex의 Google 커넥터와 독립 자비스 서버의 인증은 별개입니다. 커넥터 토큰을 가져오거나 재사용하지 않습니다.

1. Google Cloud 프로젝트에서 Gmail API와 Google Calendar API를 활성화합니다.
2. OAuth 동의 화면을 구성하고 테스트 모드라면 본인 Google 계정을 테스트 사용자로 추가합니다.
3. OAuth 클라이언트 유형을 **Desktop app**으로 생성하고 JSON 파일을 내려받습니다. 비밀정보를 채팅에 붙여넣지 마세요.
4. 프로젝트 루트에서 다음을 실행합니다. 인증 파일 경로는 실제 경로로 바꿉니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe google_auth_setup.py --client-secrets 'C:\private\client_secret.json'
```

5. 직접 열린 Google 로그인 화면에서 계정과 읽기 전용 권한을 확인하고 동의합니다. 이 과정은 사용자가 위 명령을 실행할 때만 시작됩니다. localhost 콜백은 180초 동안 기다립니다.
6. 서버에서 `get_google_connection_status()`로 인증 파일 존재 여부를 확인하고 `get_daily_briefing(day="2026-10-17", limit=10)`처럼 호출해 실제 API 연결을 검증합니다. 날짜 생략 시 한국 시간의 오늘을 사용합니다.

### 인증 및 브리핑 정책

- 권한은 `gmail.metadata`, `calendar.events.readonly` 두 가지입니다. Gmail 본문·첨부파일 열람, 발송·수정·삭제, 일정 생성·수정 기능은 제공하지 않습니다. Gmail metadata 범위는 Google의 제한된 권한 정책 적용을 받을 수 있으므로 배포 전 별도 검토가 필요합니다.
- 토큰은 기본적으로 프로젝트의 `.jarvis-auth/google-token.json`에 저장하며 기본 문서 작업공간 밖에 둡니다. `.jarvis-auth`와 `client_secret*.json`은 Git에서 제외합니다. 다른 경로는 운영자가 `JARVIS_GOOGLE_TOKEN_FILE` 절대 경로로 지정할 수 있습니다.
- Windows에서는 저장 디렉터리의 기존 접근 권한을 상속합니다. 디스크 암호화나 Windows 자격 증명 저장소 연동은 이번 범위에 포함되지 않았습니다. 실제 사용 전 해당 디렉터리에 다른 사용자가 접근할 수 없는지 확인하세요.
- 토큰이 만료되면 Google 인증 라이브러리가 갱신합니다. 갱신된 access token은 이번 구현에서 디스크에 다시 저장하지 않으므로 다음 브리핑에서 재갱신할 수 있습니다. 동의 철회·refresh token 만료 시 로그인 도우미를 다시 실행합니다.
- `get_google_connection_status`는 파일 존재만 확인하며 실제 계정 연결 성공을 뜻하지 않습니다. 실연결은 브리핑 호출 결과로 확인해야 합니다.
- 일정은 한국 시간 기준 지정 날짜의 기본 캘린더 일정입니다. 종일·반복·날짜 경계를 넘는 일정은 Calendar API의 시간 범위 규칙을 따릅니다.
- 메일은 지정 날짜와 무관하게 **현재 받은편지함에서 읽지 않은 메일**의 제목·발신자·날짜를 최대 10개 가져옵니다. 일정도 최대 10개이며 추가 페이지가 있으면 `has_more`로 표시합니다. 메일 수는 근삿값입니다.
- API 응답·제목은 신뢰하지 않는 데이터입니다. 메일·일정 내용을 로컬 문서 인덱스에 자동 저장하거나 제3의 모델에 자동 전송하지 않습니다.
- 요청은 고정된 Google HTTPS API에 GET으로만 수행하며 리디렉션은 따르지 않습니다. 각 요청 및 토큰 갱신은 10초 제한을 적용합니다. 메일별 헤더 조회로 여러 요청을 수행하므로 전체 브리핑은 10초보다 오래 걸릴 수 있습니다.
- API별 오류는 `status=error`, 전체 결과에는 `partial=true`로 표시합니다. 두 API 모두 실패해도 빈 성공으로 표시하지 않습니다. 인증 자체가 없거나 잘못된 경우에는 MCP 오류를 반환합니다. 로그·오류 메시지에 토큰이나 응답 본문을 노출하지 않습니다.
- 모닝 브리핑 예약·음성 출력은 아직 활성화하지 않았습니다.

## 공통 실행 제한 및 검증

새 도구도 공통 시간창 호출 예산([시스템 도구](system-tools.md))과 `.jarvis-stop` 정책을 따릅니다. 인덱싱 작업은 원문을 변경하지 않습니다. 기존 명령·상태·스크린샷 도구는 유지했습니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

공식 참고: [Gmail 메시지 조회](https://developers.google.com/gmail/api/reference/rest/v1/users.messages.get), [Gmail 목록 조회](https://developers.google.com/gmail/api/reference/rest/v1/users.messages.list), [Calendar 일정 조회](https://developers.google.com/workspace/calendar/api/v3/reference/events/list), [Google OAuth Python 시작 안내](https://developers.google.com/workspace/calendar/api/quickstart/python).
