# MCP 호출 기록과 점검 (보드 204bc125 5단계)

[문서 안내](../README.md) · [MCP 서버 연결](mcp-client.md) · [도구 작업 에이전트](tool-agent.md) · [REA 도구 프리셋](rea-preset.md)

자비스가 MCP 도구를 부를 때마다 무엇을 불렀는지 남기고(`jarvis/features/mcp_audit/service.py`), 설정된 서버가 제대로 뜨는지 확인합니다(`jarvis/features/mcp_doctor/service.py`, `scripts/mcp_doctor.py`). 화면(HUD)은 프런트엔드 작업이라 여기 없습니다.

## 호출 기록

- 위치: `JarvisWorkspace/.jarvis-mcp-audit/YYYY-MM-DD.jsonl`(UTC 날짜), 하루 한 파일. 서버 작업 폴더인 `.jarvis-mcp/<서버>` 밖이고 점으로 시작하는 폴더라서, 서버 작업 폴더나 승인 폴더·경로 인자로는 닿지 않습니다.
- 채팅 도구 작업, 웹 작업, 평가 등 모든 호출이 `McpSession.call` 한 곳을 지나므로 모두 기록됩니다.
- 실행한 호출은 두 줄입니다. 작업자에게 보내기 **전에** `attempt`(id, 서버, 도구, effect, 인자 이름, 경로 인자의 작업공간 기준 상대 경로), 끝난 **뒤에** `outcome`(`ok`, `error`(도구가 오류를 알림), `failed`(작업자 오류·세션 끊김), `timeout`, 걸린 초, 결과 글자 수, 잘림 여부)입니다. 작업자나 자비스가 도중에 죽어도 `attempt`는 남고, 점검에서 "끝나지 않은 호출"로 보입니다.
- 서버에 가기 전에 막힌 호출은 `refused` 한 줄과 이유 코드입니다: `not_attached`(승인되지 않은 도구), `confirm_required`, `arguments_too_large`, `arguments_refused`(막은 인자, 승인 폴더 밖 경로, 경로처럼 보이는 글자), `session_limit`.
- **남기지 않는 것**: 인자의 글자 값(검색어 등), 결과 본문, 고정 설정 값, 환경 변수, 작업공간 밖 경로(`<outside>`로만 씀).
- 한도: 한 줄 2KB(경로 목록부터 줄이고 `shortened` 표시), 하루 파일 5MB(넘으면 `cap` 줄 하나를 쓰고 그날은 더 쓰지 않음), 30일 지난 파일은 지웁니다.

### 기록을 못 쓸 때

- `effect=read` 고정: 호출은 그대로 실행합니다(디스크가 가득 차도 읽기 조회는 멈추지 않게). 실패 수는 세션(`audit_misses`)과 프로세스에서 세고, 다음에 기록에 성공한 줄의 `missed`에 남깁니다.
- `auto`·`confirm` 고정이나 `write` 경로 인자가 있는 도구: 실행하지 않고 "호출 기록을 남길 수 없어 이 도구를 실행하지 않았습니다. 디스크 공간과 JarvisWorkspace 쓰기 권한을 확인해 주십시오."로 거부합니다.
- `refused`·`outcome` 줄을 못 쓰면 세기만 합니다(이미 실행했거나 아무것도 실행하지 않았으므로).

## 점검 (`scripts/mcp_doctor.py`)

```
.venv\Scripts\python.exe scripts\mcp_doctor.py              승인된 서버를 한 번씩 띄워 보고
.venv\Scripts\python.exe scripts\mcp_doctor.py --no-start   설정과 호출 기록만
.venv\Scripts\python.exe scripts\mcp_doctor.py --days 30
```

도구는 하나도 부르지 않고 설정도 바꾸지 않습니다. 모델에게 주는 도구가 아닙니다. 서버마다 보여 주는 것:

- 실행 파일이 있는지, 고정 설정 폴더가 세션 시작과 같은 검사를 통과하는지(값은 보여 주지 않음), 승인 폴더가 있는지, `rea` 서버면 설치된 REA가 6.7.0인지.
- 고정 수(effect별), 승인된 서버는 고정 도구만 받는 세션 한 번: 시작 상태(`ready`/`failed`/`changed`와 바뀐 도구), 걸린 초, 제공 도구 수.
- 최근 N일 호출 기록: 호출 수, 결과별 수, 거부 이유별 수, 끝나지 않은 호출, 마지막 시각.
- 끝에 기록 실패 수: 나중 줄에 남은 `missed` 합계와 이 프로세스에서 센 수, 하루 한도에 닿은 날.
