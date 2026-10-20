# 시스템 도구 사용 안내

[문서 안내](../README.md) · [현재 계획](../PLAN.md) · [문서 검색·Google](knowledge-and-google.md)

이 문서는 기존 시스템 도구와 공통 실행 정책을 설명합니다. 모든 명령은 프로젝트 루트에서 실행합니다. 전체 현재 도구 목록은 [플러그인 안내](plugins.md)를 따르며 Google 실계정 연결 검증도 완료했습니다.

## 실행

Python 3.13 기준, 프로젝트 디렉터리에서 PowerShell로 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

기본 작업공간 `JarvisWorkspace`는 서버 파일 옆에 자동 생성됩니다. 서버는 stdio MCP 방식이므로 터미널에서 실행하면 MCP 클라이언트 요청을 기다립니다. 일반 대화형 셸은 제공하지 않습니다. 프로토콜 출력과 로그는 각각 stdout과 stderr로 분리합니다.

다른 작업공간을 쓰려면 미리 만든 디렉터리의 절대 경로를 지정합니다. 빈 값, 상대 경로, 없는 디렉터리, 링크·정션 자체를 지정하면 시작을 거부합니다.

```powershell
$env:JARVIS_WORKSPACE = 'C:\absolute\path\JarvisWorkspace'
.\.venv\Scripts\python.exe server.py
```

MCP 클라이언트에는 `.venv\Scripts\python.exe`의 절대 경로를 실행 프로그램으로, `server.py`의 절대 경로를 인자로 등록합니다. 현재 프로젝트의 jarvis 서버는 Codex에 등록했습니다. 다른 환경에서는 클라이언트에 별도 등록해야 합니다.

### 현재 PC의 Claude Desktop 연결 (2026-10-17)

Microsoft Store 설치의 실제 설정은 `C:/Users/heejun/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude_desktop_config.json`입니다. `jarvis-core`의 Python·서버 경로는 맞았으나 JARVIS_WORKSPACE가 존재하지 않는 `C:/Users/heejun/JarvisWorkspace`여서 시작 로그에 `Workspace must already exist` 오류가 기록됐습니다. 기존 설정을 같은 폴더의 날짜별 .bak 파일로 백업한 뒤 이 환경값만 현재 프로젝트의 실제 JarvisWorkspace 절대 경로로 수정했습니다. 다른 설정은 보존했습니다.

수정된 Claude 설정 그대로, 프로젝트와 무관한 cwd에서도 실제 stdio 초기화·도구 24개 목록·시스템 상태 응답을 확인했습니다. Claude 앱 안에서의 재연결 성공은 아직 확인하지 않았습니다. 앱을 완전히 종료하고 다시 열어 설정을 다시 읽게 한 뒤 “자비스로 시스템 상태 확인해줘”로 확인합니다. 이 수정에서 Claude의 다운로드 스캔 opt-in을 새로 추가하지는 않았습니다.

## 설계 근거와 정책

- `server.py`: 공식 MCP SDK 1.x의 FastMCP를 사용하며, 2.x API 변경으로 인한 예기치 않은 전환을 막기 위해 버전을 고정했습니다. 도구 실패는 MCP 오류로 전달합니다.
- `jarvis/core/guardrails.py`: 입력·명령·경로 검증을 한곳에 모았습니다. 블랙리스트는 위험 명령 안내를 돕고, 실제 허용 여부는 좁은 화이트리스트가 결정합니다.
- `run_safe_shell_command(command: str)`: `pwd`, `whoami`, `hostname`, `echo TEXT`, `ls [directory]`만 허용합니다. 임의의 운영체제 셸에 전달하지 않고 격리된 Python 모드(`-I -S`)에서 고정된 읽기 전용 작업을 수행합니다. 입력은 JSON 데이터로 전달하며 코드로 삽입하지 않습니다. 경로는 `/` 구분자와 큰따옴표를 사용하세요. 예: `ls "my folder"`.
- 셸 연산자, 리디렉션, 환경변수 확장, 스크립트 인터프리터, 파일 삭제·수정·네트워크 명령은 거부합니다. 실행 위치는 작업공간이며, 시간 제한은 5초, 목록은 최대 200개, 입력은 1024자입니다. 제한된 작업만 실행하므로 일반 명령 실행기로 확장하려면 별도 격리 설계가 필요합니다.
- `get_system_status()`: CPU 사용률, 메모리 전체·가용량·사용률, 작업공간 볼륨의 디스크 전체·여유량·사용률을 반환합니다. 바이트 단위를 사용하고 CPU는 0.1초 동안 샘플링합니다.
- `take_screenshot()`: 현재 데스크톱을 PNG로 작업공간에 저장하고 절대 경로와 크기를 반환합니다. 고유 파일명을 배타적으로 생성해 기존 파일을 덮어쓰지 않습니다. 별도 캡처 프로세스에 10초 제한을 적용하며 최대 33,554,432 픽셀까지 허용합니다. 화면에는 민감정보가 담길 수 있으며, 도구를 호출하면 실제 캡처가 수행됩니다. 화면 접근이 없는 환경에서는 명확하게 실패합니다.
- 경로 검증은 상위 경로 이동, 외부 절대 경로, UNC·장치 경로, 대체 데이터 스트림, 심볼릭 링크·정션을 차단합니다. `ls`는 항목 이름만 나열하며 파일 내용이나 링크 대상에 접근하지 않습니다.
- 호출 예산은 서버 프로세스 단위의 10분 시간창으로 계산하며, 창이 지나면 재시작 없이 회복됩니다(2026-10-21부터, 이전에는 평생 20회). 등급별 상한: 일반(standard)과 민감(sensitive) 합계 10분당 20회, 그중 민감은 5회. 가벼운(light) 게시판·목록 도구는 별도로 10분당 60회. 하루(로컬 자정 기준) 전체 600회, 민감 40회. 민감 등급은 파일 정리 적용·되돌리기, 스냅샷 복원, 런처 시작·종료, 인덱스 삭제, 화면 캡처, 메일·일정 브리핑, 외부 모델 호출(협업·무료 배정)입니다. 등급표는 `jarvis/app/runtime.py`의 `TOOL_TIERS`. 거부 메시지에 다음 회복 시각이 나오며, 예산 초과로 거부된 호출은 횟수에 넣지 않습니다. 도구 안에서 실패한 정책 검사는 횟수에 포함됩니다. 호출자가 작업 ID를 바꿔 초기화할 수 없습니다. MCP 스키마 검사에서 거부된 요청은 도구 함수가 실행되지 않아 횟수에 포함되지 않습니다.
- 작업공간에 `.jarvis-stop` 파일이 있으면 다음 도구 호출부터 거부합니다. 이미 실행 중인 작업을 즉시 취소하는 전역 중단기는 아닙니다. 명령 작업은 별도로 5초 시간 제한을 적용합니다. 모델 호출이 없어 토큰 예산은 아직 적용 대상이 없습니다.

## 안전 경계와 제한

이 구현은 응용 프로그램 수준의 경로·명령 정책입니다. Docker나 운영체제 권한 격리를 제공하지 않습니다. 신뢰하지 않는 다른 프로세스가 작업공간이나 상위 디렉터리의 링크를 검증 직후 바꾸는 경쟁 조건까지 막는 파일시스템 샌드박스는 아닙니다. 작업공간과 코드는 신뢰하는 운영자만 수정할 수 있도록 관리해야 합니다. OS 수준 격리와 실행 중 즉시 중단은 후속 보강 사항입니다.

안전 검증은 외부 콘텐츠 속 지시의 의미를 판별하는 LLM 방화벽이 아닙니다. 명령 도구의 실행 범위는 구조적으로 제한합니다. 문서 검색은 지정 작업공간을 읽고 Google 브리핑은 별도 인증 후 고정된 Google API를 호출합니다. 브라우저 자동화는 제공하지 않습니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

테스트는 실제 stdio MCP 연결, 도구 8개 등록, 허용 명령 실행과 시스템 상태, 위험 명령·잘못된 입력·외부 경로 거부, 링크 우회, 호출 예산과 중단 표시, 설정 오류, 명령 시간 초과·실패 전달, 가상 화면을 이용한 PNG 저장·캡처 실패를 확인합니다. 실제 화면 캡처는 환경 의존적이므로 별도 실행 결과를 검증 보고서에 기록합니다.

공식 SDK 참고: https://py.sdk.modelcontextprotocol.io/v1/

이번 환경의 구체적인 검증 결과는 `docs/reports/phase-01.md`에 기록했습니다. 전체 의존성을 같은 버전으로 설치하려면 `requirements-lock.txt`를 사용할 수 있습니다.
