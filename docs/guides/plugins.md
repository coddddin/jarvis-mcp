# 도구 플러그인 추가와 공통 실행 경계

[문서 안내](../README.md) · [현재 계획](../PLAN.md)

## 구조와 파일

루트 `server.py`는 고정 실행 진입점이며 구현은 `jarvis/app/server.py`입니다. `jarvis/app/runtime.py`는 공유 ToolContext와 공통 호출 경계를 조립하고 `jarvis/app/plugins.py`는 `jarvis/features/<기능>/tools/*_tool.py`만 상대 경로순으로 등록합니다. 각 기능의 서비스와 도구를 같은 폴더에 모읍니다. 모듈명은 기능·tools 경로를 포함하므로 다른 기능의 같은 파일명은 충돌하지 않으며 MCP 이름은 전체에서 고유해야 합니다. 전체 구조는 [설계 문서](../architecture.md)를 따릅니다.

- `jarvis/features/system/tools/shell_tool.py`: `run_safe_shell_command`
- `jarvis/features/system/tools/system_status_tool.py`: `get_system_status`
- `jarvis/features/system/tools/screenshot_tool.py`: `take_screenshot`
- `jarvis/features/knowledge/tools/index_documents_tool.py`: `index_local_documents`
- `jarvis/features/knowledge/tools/search_documents_tool.py`: `search_local_documents`
- `jarvis/features/knowledge/tools/clear_index_tool.py`: `clear_local_index`
- `jarvis/features/google/tools/google_status_tool.py`: `get_google_connection_status`
- `jarvis/features/google/tools/daily_briefing_tool.py`: `get_daily_briefing`
- `jarvis/features/snapshots/tools/create_snapshot_tool.py`: `create_project_snapshot`
- `jarvis/features/snapshots/tools/list_snapshots_tool.py`: `list_project_snapshots`
- `jarvis/features/snapshots/tools/preview_restore_tool.py`: `preview_project_restore`
- `jarvis/features/snapshots/tools/restore_snapshot_tool.py`: `restore_project_snapshot`
- `jarvis/features/files/tools/preview_organization_tool.py`: `preview_file_organization`
- `jarvis/features/files/tools/apply_organization_tool.py`: `apply_file_organization`
- `jarvis/features/files/tools/list_organization_tool.py`: `list_file_organization_operations`
- `jarvis/features/files/tools/preview_undo_organization_tool.py`: `preview_file_organization_undo`
- `jarvis/features/files/tools/undo_organization_tool.py`: `undo_file_organization`
- `jarvis/features/files/tools/preview_downloads_tool.py`: `preview_downloads_organization`
- `jarvis/features/workflows/tools/list_workflows_tool.py`: `list_workflows`
- `jarvis/features/workflows/tools/preview_workflow_tool.py`: `preview_workflow`
- `jarvis/features/workflows/tools/start_workflow_tool.py`: `start_workflow`
- `jarvis/features/workflows/tools/workflow_status_tool.py`: `get_workflow_status`
- `jarvis/features/workflows/tools/list_runs_tool.py`: `list_workflow_runs`
- `jarvis/features/workflows/tools/stop_workflow_tool.py`: `stop_workflow`
- `jarvis/features/development/tools/debug_assistant_tool.py`: `analyze_error_log`
- `jarvis/features/web/tools/list_sources_tool.py`: `list_web_sources`
- `jarvis/features/web/tools/collect_information_tool.py`: `collect_web_information`
- `jarvis/features/board/tools/*_tool.py`: `board_list`, `board_get`, `board_create`, `board_claim`, `board_heartbeat`, `board_note`, `board_release` ([게시판 안내](board.md), 2026-10-21 추가)

저장소 `jarvis/features/knowledge/service.py`, Google `jarvis/features/google/service.py`, 스냅샷 `jarvis/features/snapshots/service.py`, 파일 정리 `jarvis/features/files/service.py`, 런처 `jarvis/features/workflows/service.py`, 진단 `jarvis/features/development/service.py`, 웹 수집 `jarvis/features/web/service.py`/`jarvis/workers/web_https.py`는 공유합니다. 위 목록은 패키지 이전 당시 27개이며 이후 web·integrations·board 도구가 추가되어 2026-10-21 기준 38개입니다. 실행 파일·가상환경·데이터·인증정보 위치와 Codex 등록 경로는 변경하지 않았습니다.

## 계열별 배치 규칙

- `system/`: 셸·시스템 상태·화면 캡처 등 PC 기본 제어 (현재 3개).
- `knowledge/`: 문서 인덱싱·검색·인덱스 삭제 (현재 3개).
- `google/`: Google 계정 상태·일정/메일 브리핑 (현재 2개).
- `snapshots/`: 백업·목록·복원 미리보기·적용 (현재 4개).
- `files/`: 파일 정리·미리보기·이동·기록·되돌리기·다운로드 메타데이터 시뮬레이션 (현재 6개).
- `development/`: 로그·Python 문법 분석·읽기 전용 수정 제안 (현재 1개). 실제 수정 적용은 후속 범위.
- `workflows/`: 등록형 작업 환경 런처·실행 기록·상태·종료 (현재 6개).
- `web/`: 등록된 공개 출처 목록·웹 수집/보고서·공개 검색 발췌·페이지 읽기 (현재 4개).
- `board/`: 에이전트 공용 작업 게시판 (현재 7개).

예정 계열은 구현할 때 만들며 이번에 빈 폴더나 미구현 도구를 추가하지 않았습니다. 기존 계열에 맞으면 그곳에 추가하고 새 기능군일 때만 새 계열을 만듭니다. 도구 한 개당 파일 한 개 원칙을 유지합니다.

기능명은 유효한 Python 식별자이고 `__init__.py`는 패키지 표시·설명만 둡니다. 로더는 초기화 파일을 직접 실행하지 않으며 기능 폴더와 tools 하위 폴더의 링크·정션을 거부합니다. 숨김·밑줄 시작 기능은 검색하지 않습니다. tools 아래 추가 중첩은 자동 검색하지 않습니다. features 루트나 기능 바로 아래에 남은 옛 *_tool.py는 무시하지 않고 잘못된 배치로 시작을 거부합니다. 내부 Python import 경로는 새 패키지 경로를 사용하며 옛 루트 모듈별 사본은 제공하지 않습니다.

## 추가 계약

운영자가 검토한 파일을 `jarvis/features/<기능>/tools/<이름>_tool.py`에 추가합니다. 다음은 설명용 예제이며 기본 도구로 추가하지 않았습니다.

```python
def build(context):
    def example_tool(message: str = 'hello') -> dict:
        """Return an example response."""
        return {'message': message}
    return example_tool
```

- 각 파일의 `build(context)`는 같은 모듈이 정의한 동기 함수 하나를 반환합니다. 함수 이름이 MCP 도구 이름이며 타입·설명을 작성해야 합니다. 비동기 함수는 현재 지원하지 않습니다.
- `context.guard`, `context.knowledge`, `context.google`, `context.snapshots`, `context.organizer`, `context.workflows`, `context.debug`, `context.web`는 공통 조립부에서 공유합니다. 파일·명령 접근 정책과 새 기능의 입력 검증은 그대로 필요합니다.
- `guard.enter_call()`은 플러그인에서 호출하지 않습니다. 로더의 공통 래퍼가 예산을 한 번 차감하고 중단 표시를 확인합니다.
- FastMCP 객체는 전달하지 않습니다. 플러그인에서 별도 서버 생성이나 직접 도구 등록을 하지 않습니다.
- 추가 후 테스트·문서 검사를 실행하고 서버를 재연결합니다. server.py 수정은 필요하지 않으며 핫 리로드는 제공하지 않습니다.

## 오류와 신뢰 경계

등록 함수 누락·잘못된 계약·중복 이름·임포트 오류·빈 플러그인 폴더는 시작 실패로 처리합니다. 링크·정션·하드링크 파일, 데이터 작업공간 안의 플러그인 디렉터리를 거부합니다. 도구 실행 오류는 MCP 오류로 전달하고 예상하지 못한 예외의 상세 내용은 응답에서 가립니다.

Python 임포트와 build는 신뢰된 운영자 코드를 실행합니다. 이 구조는 악성 Python 플러그인을 격리하는 샌드박스가 아니며 공통 호출 경계가 임포트 시 부작용을 통제하지 않습니다. 다운로드·문서 코드에서 플러그인을 자동 로드하지 않습니다.

## 검증

프로젝트 루트에서 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
python scripts/check_docs.py
python scripts/check_architecture.py
```

`tests/fixtures/tool_contract.json`은 최초 8개, `tool_contract_pre_layout.json`은 패키지 이전 전 전체 27개 이름·설명·입력 계약입니다. 신규 도구는 새 검사를 추가하며 기존 계약은 보존합니다. 구조 검사기는 실제 파일 배치와 AST import 방향을 검사하며 동적 코드 실행의 보안 격리를 제공하지 않습니다.
