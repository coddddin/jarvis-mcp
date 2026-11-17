# REA 도구 프리셋 (보드 204bc125 4단계)

[문서 안내](../README.md) · [도구 작업 에이전트](tool-agent.md) · [MCP 서버 연결](mcp-client.md)

채팅에서 "REA로 ..."라고 부르면 [도구 작업 에이전트](tool-agent.md)가 REA의 정적 분석 도구를 여러 번 써서 답하도록, 고정할 도구와 실행 방법을 미리 정해 둔 것입니다. 코드는 `jarvis/features/mcp_presets/service.py`, 설치는 `scripts/setup_rea_tools.py`, 평가는 `scripts/tool_eval/`입니다. 승인 화면(HUD)은 프런트엔드 작업이라 여기 없습니다.

## 버전과 실행

- `rea-agents` **6.7.0**만 받습니다. 저장소의 잠금 파일로 설치한 `.integrations/rea`(경로는 `config/integrations.json`)의 `package.json` 이름과 버전이 정확히 맞아야 합니다. 다르면 `.integrations/rea`에서 `npm ci`로 다시 설치하라고 알리고 멈춥니다. 아무것도 내려받지 않습니다.
- 실행: `node rea.mjs --mcp`. node, `rea.mjs`, Ghidra, Java 위치는 `IntegrationHub.program_paths`로 읽습니다. 정적 분석 도구(`analyze_program_static`)와 같은 검사입니다: 링크·`..`가 없고, 작업공간 밖이며, 파일은 하드 링크 없는 일반 파일이고 폴더는 실제로 있어야 합니다.
- Ghidra·Java는 고정 설정 `fixed_env`(`GHIDRA_INSTALL_DIR`, `JAVA_HOME`, `GHIDRA_HEADLESS_MAXMEM=1G`)로 넘깁니다. 검사 규칙은 [MCP 서버 연결](mcp-client.md)에 있습니다. 이 값은 모델 프롬프트·결과 기록·상태에 들어가지 않습니다. 검사는 링크와 작업공간만 막으므로, 그 밖에서는 사용자가 `integrations.json`에 적은 어느 폴더든 쓸 수 있습니다(설치 위치를 고르는 것은 사용자입니다).

## 고정하는 도구 (29개, 모두 `family=local`, `effect=read`)

- 열기·닫기: `open_binary`(경로 `path`만, 120초), `close_binary`.
- 개요·목록: `binary_overview`, `list_documents`, `current_document`, `list_segments`, `list_procedures`, `list_strings`, `list_names`.
- 찾기: `search_procedures`, `search_strings`, `xrefs`, `find_xrefs_to_name`, `get_call_graph`.
- 함수: `procedure_info`, `procedure_pseudo_code`, `procedure_assembly`, `procedure_callers`, `procedure_callees`, `procedure_references`, `batch_decompile`, `analyze_function`, `inspect_native_api`.
- 바이트·서명: `read_bytes`, `inspect_signature`.
- 파일 하나를 바로 읽는 도구: `inspect_pe_resources`, `inspect_go_binary`, `inspect_rust_binary`, `analyze_javascript_application`(`input_path`, `source_paths`).

`family=local`이라 결과는 외부 모델에 가지 않습니다. `REA`는 로컬 전용 요청 단어라 처음부터 로컬 모델이 답합니다. 경로 인자는 승인 폴더(기본 `projects`) 안의 파일만 받습니다.

### 모델이 쓰지 못하는 인자 (`blocked_args`, 스키마에서도 숨김)

- `open_binary`: `format`, `architecture`, `entry_point`, `provider_id`는 형식 없는 바이트 덩어리용입니다. PE·ELF·Mach-O는 REA가 스스로 알아냅니다. 첫 평가에서 qwen이 이 값들을 틀리게 채워 열기에 실패했습니다. `snapshot_path`는 분석 파일을 저장하거나 불러오므로 막습니다.
- `close_binary`: `snapshot_path`, `overwrite`.
- 열린 분석을 고르는 `document`: 작업 세션은 바이너리 하나만 열므로 기본값을 씁니다.
- `inspect_pe_resources`: `max_file_bytes`, `max_entries`(REA 기본값).

### 결과 줄이기 (`result_keys`)

REA 답은 증거 기록이고, 앞 약 3KB가 출처 정보입니다. 그래서 `result`와 `normalized_result`만 남긴 뒤 글자 한도로 자릅니다(예: `binary_overview` 6873자 → 1400자 남짓). 이 둘이 없는 답(오류 등)은 그대로 둡니다.

## 고정하지 않는 도구

REA가 알리는 207개 가운데 나머지는 모두 숨깁니다. 사용자 Claude Code 설정에서 거부한 46개(ADB, Frida, gdb·rizin 디버거, 프로세스·네이티브 UI·브라우저·Electron 캡처와 관찰)는 하나도 없고, 이것도 막습니다.

- 쓰는 도구: `set_*`, `unset_*`, `annotate_*`, `record_*`, `update_*`, `export_*`, `import_*`, `extract_*`, `recover_*`, `comment`, 북마크.
- 실행·명령: `execute_rizin_command`, `cutter_command`, `inspect_with_objdump`, `capture_process_scenario`.
- 브라우저·CDP·자바스크립트 런타임 연결 도구, 디바이스 도구.
- `inspect_binary_layout`: pwntools의 Linux x64 ELF용이라 PE에서 거부하고, 그 거부를 보고 모델이 "분석할 수 없다"고 답했습니다.

`tests/test_mcp_presets.py`가 거부 목록 46개와 위험한 이름 규칙(쓰기·디버그·실행·캡처·브라우저·디바이스)에 맞는 도구가 고정에 없는지 확인합니다.

## 설치

```
.venv\Scripts\python.exe scripts\setup_rea_tools.py            고정할 도구를 보여 주고 묻기
.venv\Scripts\python.exe scripts\setup_rea_tools.py --yes      묻지 않기(사용자가 직접 실행할 때)
.venv\Scripts\python.exe scripts\setup_rea_tools.py --root projects/re
```

`JarvisWorkspace/.jarvis-settings/mcp_servers.json`에 서버 `rea`(라벨 REA)를 넣고(이미 있으면 바꿈), 한 번 띄워 도구 목록을 받고, 확인 뒤 위 고정과 승인 폴더를 저장합니다. 고정할 도구가 하나라도 없거나, 서버가 시작하지 않거나, 승인하지 않으면 항목을 지웁니다. 그다음 HUD를 다시 시작하고 "REA로 projects/demo/app.exe 개요 보여줘"처럼 묻습니다.

## 평가 (`scripts/tool_eval`, N/4)

- 표본: `C:\Windows\System32\whoami.exe`를 실행마다 새 작업공간의 `projects/demo/`로 복사합니다(저장소에 넣지 않음). 기대값은 SHA-256 `23240ef9…6cd3`(2026-11-17)에서 확인했고, `summary.json`의 `sample_matches`가 다른 Windows 빌드를 알려 줍니다.
- 과제: r01 형식·아키텍처(PE, x86-64), r02 섹션 이름(.text, .rdata, .rsrc), r03 사용자 이름 API 문자열(GetUserNameExW), r04 `LookupPrivilegeNameW`를 가져오는 DLL(ADVAPI32).
- 채점(`score.py`): 답이 있고, `answer_all` 정규식이 모두 맞고(자비스가 붙이는 `사용한 도구` 줄은 빼고 봄), `open_binary`가 한 번은 성공했고, 프리셋 밖 도구를 부르지 않아야 통과입니다.
- 실행: `.integrations`가 있는 체크아웃에서 `.venv\Scripts\python.exe scripts\tool_eval\run.py [--repeat 3] [r01 ...]`. 결과는 `%LOCALAPPDATA%\JarvisWorktrees\tool-eval\results`에만 씁니다. 모델은 `qwen3.5:9b`(로컬)이고 채팅과 같은 `tool_agent.run_task`를 씁니다.

### 점수

2026-11-17, qwen3.5:9b, `--repeat 3`: **중앙값 2/4** ([2, 2, 3]). 과제당 1~3회 호출, 11~38초.

- r02(섹션 이름) 3/3, r04 2/3, r01 2/3, r03 0/3.
- 실패는 모두 모델이 틀린 것입니다. r03은 `literal` 검색에 정규식 `GetUserName|...`을 넣어 0건을 받았고, 이어서 이름이 빈 인자로 `list_strings`를 세 번 골라 거부된 뒤 "없다"고 답했습니다. r04 한 번은 도구 없이 kernel32라고 짐작했고, r01 한 번은 `open_binary`가 `x86_64`라고 했는데 x86이라고 썼습니다.
- 프리셋을 고치기 전 첫 측정은 1/4였습니다. qwen이 `open_binary`에 `format=raw-binary`나 `dos-com`을 넣어 열기에 실패했습니다. 그래서 위의 막은 인자와 승인 설명을 넣었습니다.
