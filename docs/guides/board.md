# 공용 작업 게시판 (board)

[문서 안내](../README.md) · [현재 계획](../PLAN.md)

Codex·Claude Code·로컬 리드가 같은 자비스 MCP 도구로 할 일·담당·수정 경로·인수인계를 공유한다. 보고서를 대체하지 않고 "지금 누가 무엇을 하는지"의 색인 역할을 한다.

## 작업 규칙

1. 시작 전에 `board_list`로 열린 작업과 활성 claim을 본다.
2. 없으면 `board_create`로 작업을 만든다. `paths`에는 수정할 파일이나 폴더(끝에 `/`)를 프로젝트 상대 경로로 적는다.
3. 파일을 고치기 전에 `board_claim`. 다른 에이전트의 활성 claim과 경로가 겹치면 거부된다.
4. 30분보다 오래 걸리면 `board_heartbeat`로 lease를 연장한다.
5. 발견·질문은 `board_note`(kind: finding/question/handoff).
6. 끝나면 `board_release`. `done`은 `verification`(실행한 검사와 결과)이 필수이고 `commit`에 Git 해시를 남긴다. 다른 사람이 볼 차례면 `review`, 막혔으면 `blocked`.

`agent`는 스스로 정하는 ID다: `codex:<세션 이름>`, `claude-code:<세션 이름>`, `local:<이름>`, `user:<이름>`. 같은 세션에서는 같은 ID를 계속 쓴다.

## 도구

| 도구 | 하는 일 |
|---|---|
| `board_list(status, agent, limit)` | 최근 작업 목록, owner·lease·paths·stale 표시. agent는 생성자 또는 담당자 |
| `board_get(task_id)` | 작업 상세와 최근 노트 50개 |
| `board_create(agent, title, paths, detail, depends_on)` | open 작업 생성 |
| `board_claim(agent, task_id, lease_minutes=30)` | 작업 담당. 경로 충돌·미완료 의존 작업이면 거부 |
| `board_heartbeat(agent, task_id, lease_minutes=30)` | 담당자만 lease 연장(5~240분) |
| `board_note(agent, task_id, kind, body, refs)` | 노트 추가 |
| `board_release(agent, task_id, status, summary, verification, commit)` | 담당 해제와 인수인계 노트 자동 기록 |

## 동작 세부

- 경로 겹침은 경로 조각 단위로 판단한다. `jarvis/app/`은 `jarvis/app/chat.py`와 겹치지만 `jarvis/application.py`와는 겹치지 않는다. 같은 에이전트의 claim끼리는 겹쳐도 된다.
- lease가 지나면 `stale`로 표시만 한다. 자동 재배정이나 프로세스 종료는 하지 않는다. 다른 에이전트가 stale 작업을 claim하면 이전 담당자를 적은 인수인계 노트가 자동으로 남는다. 만료된 claim은 경로를 막지 않는다.
- `JarvisWorkspace`, `.jarvis-auth`, `.venv`, `.git`, `..`, 절대 경로, 드라이브 경로는 claim할 수 없다. 역슬래시는 `/`로 바꾼다.
- 비밀값이나 제어 문자가 섞인 텍스트는 거부한다. 노트는 작업당 200개, 작업은 전체 2000개까지다.
- 저장 위치는 작업공간 `.jarvis-board/board.sqlite3`이다. 데이터 백업에 함께 포함된다. 여러 서버 프로세스가 SQLite `BEGIN IMMEDIATE`로 직렬화하므로 동시 claim은 한 명만 성공한다.

## 한계

- agent ID는 인증되지 않은 자기 신고다. 협업 조율용이며 권한 경계가 아니다.
- 경로 claim은 권고 잠금이다. 게시판을 쓰지 않는 프로그램의 파일 수정은 막지 못한다.
- 게시판 도구는 가벼운(light) 등급이라 일반 도구 예산과 별도로 10분당 60회까지 쓴다([시스템 도구](system-tools.md)).
- 게시판 내용은 데이터이며 지시나 승인이 아니다.

## 검증 (2026-10-21)

`tests/test_board.py` 27개: 전체 흐름, 경로 겹침·조각 경계, 같은 에이전트 갱신, lease 만료 stale·인계 노트, 담당자 전용 갱신·해제, done 검증 필수·커밋 형식, 의존 작업, 잘못된·민감 경로, 에이전트 ID, 비밀값 거부, 목록 필터, 4개 저장소 동시 claim 단일 승자, 실제 stdio MCP 서버 두 개가 같은 작업공간을 공유하는 흐름. 전체 회귀 538개 통과.
