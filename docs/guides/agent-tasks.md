# 에이전트 작업 상태와 재시도 제어

[문서 안내](../README.md) · [현재 계획](../PLAN.md)

## 현재 사용할 수 있는 범위

Phase 2.5의 첫 구현 단위는 신뢰된 Python 코드가 사용하는 작업 제어 API입니다. jarvis/agents/models.py의 엄격한 Pydantic 계약과 jarvis/agents/store.py의 SQLite 저장소를 제공합니다. 저장소 자체는 모델·네트워크를 호출하지 않습니다. 두 번째 단위로 로컬 실행기/CLI를 추가했으며 기존 MCP 도구는 27개 그대로입니다.

## 로컬 Ollama 실행기 (설치·실연결 확인)

사용자 선택에 따라 기본 모델을 **qwen2.5-coder:7b**로 설정했습니다. [공식 기본 모델](https://ollama.com/library/qwen2.5-coder:7b)의 Q4_K_M 파일은 약 4.7GB입니다. 2026-10-18 사용자 재승인 후 Ollama 0.40.5 설치·모델 다운로드·실제 추론과 정적 QA를 확인했습니다. PC는 RTX 4070 Ti SUPER/VRAM 16GB이며 컨텍스트 8,192에서 모델 VRAM 적재량은 5,148,633,988바이트(약 4.80GiB)였습니다. GPU 전체 사용량과는 구분합니다.

비개인 덧셈 함수 예제로 Planner → Coder → 정적 QA가 3단계/수정 0회로 completed에 도달했습니다. 첫 실행은 모델 적재 포함 30.823초, 별도의 짧은 준비된 상태 표본은 73토큰/0.648604초로 112.55토큰/초였습니다. 이는 짧은 생성 구간 표본이며 요청 전체 속도·큰 작업 처리량 보장은 아닙니다. 서버 로그에서 OLLAMA_NO_CLOUD=true와 cloud disabled=true를 확인했습니다.

프로젝트 루트에서 사용하는 명령(현재 모델은 이미 다운로드됨):

```powershell
ollama pull qwen2.5-coder:7b
.\.venv\Scripts\python.exe -m jarvis.cli.agents create --summary "두 수를 더하는 Python 함수를 제안해줘"
.\.venv\Scripts\python.exe -m jarvis.cli.agents run --task-id <생성된_ID>
.\.venv\Scripts\python.exe -m jarvis.cli.agents status --task-id <생성된_ID>
.\.venv\Scripts\python.exe -m jarvis.cli.agents cancel --task-id <진행_중_ID>
```

- Planner/Coder는 엄격한 JSON 응답만 받습니다. CLI --model로 설치된 다른 로컬 태그를 선택할 수 있습니다. 기본값은 LocalConfig 한 곳에서 가져옵니다.
- 127.0.0.1:11434의 tags/show/chat API만 사용합니다. 설치 목록·GGUF 메타데이터를 확인하고 cloud 태그/원격 모델 메타데이터를 거부합니다. 프록시·리디렉션·도구 호출·자동 다운로드는 없습니다. 운영 Ollama도 [로컬 전용 설정](https://docs.ollama.com/faq)인 OLLAMA_NO_CLOUD=1로 실행합니다. 악성 로컬 서버를 인증하는 기능은 아닙니다.
- 명시한 작업 요약·계획·직전 제안·마지막 결과만 전달합니다. 파일·메일·인증정보를 자동 읽지 않습니다. 제안은 12,000자까지이며 인식된 비밀값이 있으면 저장을 거부합니다. 탐지는 완전하지 않으므로 민감한 원문을 입력하지 않습니다.
- 기본 QA는 AST/compile이며, 등록한 테스트가 있으면 제한된 정수 해석기 또는 명시적 로컬 컨테이너에서 검사합니다. run은 소스를 호스트 Python에서 실행하거나 자동 적용하지 않습니다. completed는 선택한 범위의 검사 통과입니다. [QA·예산·기억·이벤트 안내](agent-controls.md)를 따릅니다.
- 요청당 출력 1,024/컨텍스트 8,192토큰을 예약하고 작업별 출력/합산 예약 한도를 영속화합니다. 입력 64KiB/응답 256KiB, 모델 120초·제한 QA 10초·컨테이너 실행 15초와 작업 마감을 적용합니다. 취소를 감시해 소유 자식/컨테이너를 종료하지만 Ollama 내부 GPU의 즉시 회수나 실제 청구 토큰 계측은 보장하지 않습니다.
- 구문 오류는 기존 최대 3회 수정 흐름으로 연결합니다. 전송 실패·잘못된 JSON·증거 해시 불일치는 needs_user_input으로 중단하며 자동 전송 재시도하지 않습니다. running 상태는 재시작 후 자동 재실행하지 않습니다. CLI 작업 마감은 생성 시점부터 600초입니다.

API 근거: [Ollama chat](https://docs.ollama.com/api/chat), [구조화된 응답](https://docs.ollama.com/capabilities/structured-outputs). 기존 47개 관련 검사는 모의 모델+실제 정적 QA의 경계 검사이며, 별도의 실제 모델 연결 결과와 구분해 [보고서](../reports/phase-02-5.md)에 기록합니다.

작업마다 Planner → Coder → QA 순서를 강제합니다. QA 실패 후 Coder 수정 → QA를 최대 3번 허용하므로 QA는 최초 포함 최대 4회입니다. 세 번째 수정도 실패하면 needs_user_input으로 끝나며 다음 수정은 거부합니다. 재시도는 Coder 수정 시작 시 차감합니다. 도중에 프로세스가 죽어도 이미 시작한 수정 횟수를 돌려주지 않습니다.

## 코드 제안 미리보기와 백업 후 적용

정적 QA를 통과한 completed 작업의 마지막 Coder 제안을, 사용자가 지정한 projects/<name> 안의 .py 파일 하나에 적용할 수 있습니다. 대상 프로젝트와 부모 폴더는 이미 있어야 합니다. 서버 코드 루트·문서·다운로드·숨김/보호 파일은 대상이 아닙니다. CLI의 preview/apply는 신뢰된 로컬 운영자용이며 MCP 새 도구는 추가하지 않았습니다.

```powershell
.\.venv\Scripts\python.exe -m jarvis.cli.agents preview --task-id <작업_ID> --target projects/demo/app.py
# diff와 대상/원본/제안 해시를 검토한 뒤 반환된 review_digest를 넣는다.
.\.venv\Scripts\python.exe -m jarvis.cli.agents apply --task-id <작업_ID> --target projects/demo/app.py --review-digest <미리보기_digest>
```

- preview는 원본을 수정하지 않고 diff, create/replace/unchanged, review_digest를 반환합니다. digest는 작업·대상·원본/제안 해시를 결합한 내용 식별값이며 사용자 인증이나 실행 권한을 대신하지 않습니다. 모델 출력의 경로나 지시를 적용 명령으로 해석하지 않습니다.
- apply는 같은 내용을 재검증하고 프로젝트 ZIP 스냅샷을 만든 뒤 파일 하나만 교체합니다. 대상·원본·제안이 달라지면 거부합니다. 완료 결과에는 backup_id와 recovery_id가 있습니다. 파일이 이미 같은 내용이면 불필요한 쓰기/백업을 하지 않고 거부합니다.
- 원본은 UTF-8 최대 64KiB, 제안은 기존 12,000자 한도를 따릅니다. 인식된 비밀값이 있는 원본/제안은 거부합니다. 링크·정션·하드링크도 기존 경로 정책으로 거부합니다. 인식되지 않은 민감한 내용은 검토자가 확인해야 합니다.
- 프로젝트 단위 잠금과 한 번 소비하는 적용 식별자는 .jarvis-agent-changes/에 저장됩니다. 재시작 후 같은 적용을 조용히 반복하지 않습니다. 복구 기록은 최대 100개를 기준으로 제한하며 보존 정리는 운영자 확인 후 별도 진행합니다.
- 변경 전 스냅샷과 복구용 원본을 남깁니다. 실패 후 현재 파일이 적용한 내용과 같을 때만 롤백하고, 다른 프로그램이 수정한 파일은 덮어쓰지 않습니다. 롤백 실패·저널 저장 실패도 오류에 명시합니다. 불완전한 preparing/applying/recovery_required 기록이 있는 프로젝트는 후속 적용을 차단합니다. 읽을 수 없는 저널은 범위를 신뢰할 수 없어 전체 신규 적용을 차단합니다.
- 복구 자료는 .jarvis-agent-changes/<recovery_id>/, 스냅샷은 기존 [복원 절차](snapshots.md)로 확인합니다. 강제 종료 후 잠금/복구 상태는 자동 해제하지 않으며 운영자가 원본·백업을 검토해야 합니다. .jarvis-stop은 롤백/저널 쓰기도 막을 수 있으므로 복구 미완료를 성공으로 보고하지 않습니다.
- 기존 파일 교체는 OS 원자적 replace를 사용하고 전후 내용을 확인합니다. 다른 프로그램과의 최종 확인→교체 사이 경합을 완전히 막는 OS 격리/CAS는 아닙니다. 새 파일은 원자적 무덮어쓰기 생성으로 동시 생성 파일을 보호합니다.

적용 상태는 별도 저널의 applied이며 작업의 completed를 기능 검증 완료로 승격하지 않습니다. 생성 코드는 실행하지 않습니다. 일반 기능 테스트·격리 실행·모델의 자동 적용 권한은 아직 제공하지 않습니다.

## Python 상태 API 예제

프로젝트 루트와 현재 가상환경에서 호출합니다. 아래 코드는 작업 등록과 Planner 단계의 결과 기록 예이며 실제 모델 실행 예제가 아닙니다.

```python
from jarvis.agents.models import StepResult, TaskRequest
from jarvis.agents.store import TaskStore
from jarvis.core.guardrails import Guardrails
from jarvis.core.paths import DEFAULT_WORKSPACE

store = TaskStore(Guardrails(DEFAULT_WORKSPACE))
task = store.create(TaskRequest(summary='로컬 수정안 검토', max_steps=12, timeout_seconds=300))
step = store.begin_step(task.task_id, 'planner')
# 호출자가 실제로 수행한 단계의 짧은 결과만 기록한다.
task = store.finish_step(task.task_id, StepResult(
    step_id=step.active_step, role='planner',
    summary='수정 범위와 완료 기준을 정리했다.',
))
print(task.task_id, task.status, task.expected_role)  # ready, coder
# 작업 ID를 보관하면 재시작 후 store.get(task_id)로 같은 상태를 조회한다.
```

- create: 새 작업 ID와 변경할 수 없는 최초 한도를 저장합니다. 동일 작업의 자동 재생성·작업 분할로 예산을 회피하는 실행기는 제공하지 않습니다.
- begin_step: 허용 순서의 역할 하나만 시작하고 고유 step_id를 발급합니다. 실행 횟수·수정 횟수는 이때 원자적으로 저장합니다. 같은 작업에 동시에 시작 요청이 들어오면 한 요청만 성공합니다.
- finish_step: 같은 작업·현재 역할·현재 step_id의 결과만 받아들입니다. 중복·오래된 결과는 거부합니다. QA는 엄격한 boolean qa_passed가 필수이고 다른 역할에는 판정을 넣을 수 없습니다.
- get: 상태·원래 요청·횟수·요약 기록을 조회합니다. 시간 한도를 지난 작업은 timed_out으로 영속화합니다.
- interrupt: 역할 실행 오류는 needs_user_input, 명시적 취소는 cancelled로 기록합니다. 종료된 작업의 상태·예산을 초기화하지 않습니다.

상태는 ready / running / completed / needs_user_input / cancelled / timed_out입니다. completed는 호출자가 제출한 QA 통과 판정을 받은 상태입니다. 저장소가 QA 검사 명령을 직접 실행하거나 그 판정의 진위를 독립 검증한다는 뜻은 아닙니다. 실제 검수 실행과 증거 연결은 다음 구현 단위에서 처리합니다.

## 저장·보안·재시작

- 저장 위치는 작업공간의 .jarvis-agents/tasks.sqlite3입니다. 기존 documents/ 검색 인덱스나 .jarvis-auth/와 분리합니다. 새 패키지를 import하는 것만으로 DB를 만들지 않습니다.
- 각 상태 변경은 SQLite 트랜잭션으로 처리합니다. 저장소 오류·잘못된 영속 상태·동시 실행 충돌은 성공으로 숨기지 않습니다. 상태의 역할 이력과 수정 횟수도 일치해야 합니다.
- 저장 디렉터리와 SQLite 본체·journal/wal/shm 파일의 경로를 검사합니다. 링크·정션·파일 하드링크와 작업공간 밖 경로는 거부합니다. 현재 가드레일과 같은 애플리케이션 경계이며 파일 교체 경합까지 OS 수준으로 격리하는 기능은 아닙니다.
- .jarvis-stop이 있으면 생성·조회·변경을 거부합니다. 각 변경의 커밋 전에도 확인합니다. 중단 표시가 running 작업을 자동 cancelled로 바꾸거나 외부 프로세스를 강제 종료하지는 않습니다.
- 요청·결과·중단 사유는 각 2,000자 이내의 정제된 요약입니다. 기존 비밀값 가림은 최선 노력 방식이므로 인증정보·개인 원문·내부 사고 과정은 애초에 넘기지 않습니다. 저장된 문구는 권한이나 실행 지시가 아닙니다.
- 기본 역할 시작 예산은 12회(3~20), API 기본 시간은 300초(1~3,600)입니다. 공통 MCP 호출 예산([시스템 도구](system-tools.md))을 초기화하지 않습니다. 출력/컨텍스트 예약과 별도 재개 한도는 [공유 제어](agent-controls.md)를 따릅니다. 유료 외부 모델 비용 정책은 미지원입니다.
- 시간은 재시작 후에도 유지되는 시스템 시각의 절대 마감입니다. 시스템 시계 변경의 영향을 받습니다. 저장소는 API 진입 시, 실행기는 실행 중에도 확인해 소유 자식/컨테이너를 정리합니다. 모델 서버 내부 GPU의 즉시 회수는 보장하지 않습니다.
- running 상태에서 재시작하면 차감된 예산이 유지됩니다. 이전 실행 종료를 운영자가 확인한 뒤 interrupt/resume할 수 있습니다. 자동 재실행·카운터 초기화는 없으며 마감/예산/최종 수정 한도를 재개로 우회할 수 없습니다.

작업 ID는 인증 수단이 아닙니다. 변경 API는 신뢰된 로컬 운영자용이고, WebSocket에는 인증된 최소 상태 이벤트만 노출합니다. 작업/기억의 보존·정정/삭제·확인과 이벤트 재연결은 [공유 제어 안내](agent-controls.md)를 따릅니다. 유료 제공자·다중 사용자 로그인은 별도 범위입니다.
