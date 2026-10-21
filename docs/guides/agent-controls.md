# 에이전트 QA·예산·기억·이벤트 운영

[문서 안내](../README.md) · [작업 및 적용 안내](agent-tasks.md) · [실제 검증 기록](../archive/reports/phase-02-5.md)

## 세 가지 QA 범위

기본 run은 생성 코드를 실행하지 않고 AST/compile만 검사합니다. 명시한 테스트가 있을 때 다음 두 모드를 사용합니다. completed는 선택한 QA 범위의 통과이지 프로그램 전체의 정확성 보장이 아닙니다.

- restricted: 정수 인자 1~8개를 받는 단일 순수 함수의 return 수식을 제한된 AST 해석기로 계산합니다. Python exec/eval/import는 하지 않습니다. 사칙 중 +, -, *, //, %, 비교·조건식을 지원하고 호출·속성·반복문·데코레이터·기본 인자·외부 이름은 거부합니다. 최대 32개 테스트·수식 128노드/깊이 32·중간 정수 256비트 한도입니다.
- container: 고정된 로컬 Linux 이미지에서 실제 Python 함수를 실행합니다. 정수 인자/반환값 테스트를 최대 32개 수행합니다. 반복문·표준 라이브러리는 가능하지만 외부 패키지나 호스트 프로젝트의 실행 환경까지 동일하다고 보장하지 않습니다.

입력·기댓값은 엄격한 정수이며 ±1,000,000,000 범위입니다. QA 증거에는 제안 SHA-256, 테스트 명세 해시, 검사 종류, 통과 개수와 컨테이너 이미지 ID(해당 모드)를 저장합니다. 테스트/모드와 증거가 다르면 completed 이력을 만들지 않습니다. 모델이 보내는 qa_passed 자기 평가는 사용하지 않습니다.

```powershell
$cases = '[{"function":"add","args":[2,3],"expected":5},{"function":"add","args":[-2,2],"expected":0}]'
.\.venv\Scripts\python.exe -m jarvis.cli.agents create --summary "정수 덧셈 함수 add를 작성해줘" --cases-json $cases --qa-mode container
.\.venv\Scripts\python.exe -m jarvis.cli.agents run --task-id <생성된_ID>
```

구문 오류/테스트 오답은 최대 3회 수정 흐름으로 돌아갑니다. 엔진 불가·시간/출력 한도·잘못된 응답은 needs_user_input으로 중단합니다. 제한 모드에서 지원하지 않는 코드도 일반 Python 실행으로 우회하지 않습니다.

## 실제 설치된 격리 경계

2026-10-18 사용자 승인으로 Docker Desktop 4.95.0을 사용자 계정에 설치했습니다. CLI 29.8.2, WSL 2.7.3, 로컬 Linux 엔진을 확인했고 재부팅하지 않았습니다. 공식 python:3.13-slim 이미지를 준비한 뒤 실제 ID를 config/agent_runtime.json에 고정했습니다. 태그 최신값을 QA 때 자동 다운로드하지 않습니다.

- Windows는 고정된 로컬 Docker Desktop Linux named pipe만 사용합니다. 원격 DOCKER_HOST/DOCKER_CONTEXT/TLS 환경을 제거합니다. 실행 파일과 이미지 ID는 작업공간 밖의 신뢰된 코드 설정에서만 정합니다.
- 네트워크 없음, 호스트 볼륨/소켓/장치 공유 없음, root 파일시스템 읽기 전용, UID/GID 65534, 모든 capability 제거, no-new-privileges를 적용합니다.
- CPU 0.5개·메모리 128MiB·프로세스 32개, /tmp 임시공간 8MiB, 실행 15초, 호스트 수신 출력 64KiB로 제한합니다. 준비/정리용 엔진 명령은 각각 최대 5초입니다. 호스트 파일시스템이나 로컬 Ollama 서버에 후보 코드를 실행시키지 않습니다.
- 무작위 소유 컨테이너 이름만 정리합니다. Docker 전체 prune, 다른 컨테이너 중단, 이미지 자동 삭제는 하지 않습니다. 정리를 확인할 수 없으면 성공 대신 컨테이너 이름을 포함한 오류를 반환합니다.
- Docker/이미지가 없으면 중단하며 호스트 Python으로 대체하지 않습니다. 이는 컨테이너 기반 실행 경계이고 VM/커널/컨테이너 런타임의 무결점 격리를 보장하지 않습니다.

실제 테스트에서 무권한 UID·네트워크 차단·루트 쓰기 차단·무한 출력/시간 상한·소유 컨테이너 잔여 0개를 확인했습니다. 실제 로컬 모델의 반복문 함수도 4개 사례를 통과했습니다. 소스/인자만 컨테이너에 보내고 기댓값과 최종 판정 계산은 호스트에 둡니다. 알려진 테스트를 통과한 후보가 모든 입력에 맞는다는 뜻은 아닙니다.

근거: [Docker 실행 설정](https://docs.docker.com/engine/containers/run/), [Windows 설치](https://docs.docker.com/desktop/setup/install/windows-install/).

## 공유 예약 예산과 중단/재개

모델 호출 시작 전에 출력 1,024토큰과 컨텍스트 8,192토큰을 보수적으로 예약합니다. output 예약값 ×9가 컨텍스트+출력 합산 상한 예약입니다. 서버 옵션과 함께 적용하는 한도이며 실제 tokenizer 청구량 측정치는 아닙니다.

- 기본 출력 예약 한도 8,192, 합산 예약 한도 65,536토큰입니다. create의 --max-generation-tokens / --max-model-tokens로 최초 한도를 설정합니다. 같은 작업 ID에서 늘리거나 초기화하지 않습니다.
- 기존 역할 시작/수정 횟수·작업 마감과 함께 검사합니다. 실패한 호출도 예약량을 환불하지 않습니다. 로컬 Ollama만 지원하며 유료 외부 API 비용 한도를 구현한 것으로 보고하지 않습니다.
- 다른 프로세스의 cancel 상태를 고정 작업자는 약 0.2초 간격, 컨테이너 실행은 약 0.02초 간격으로 확인합니다. 엔진 준비 명령이나 DB 경합 때문에 더 늦어질 수 있습니다. 모델 서버 내부 GPU 작업의 즉시 중단/메모리 회수는 보장하지 않습니다.
- 명시적 CLI cancel은 최종 취소입니다. 프로세스가 죽어 running으로 남았거나 일시적 실패를 복구하려면 운영자가 interrupt/resume으로 같은 작업을 다룹니다. 모델이 이 명령을 실행할 수는 없습니다.

```powershell
.\.venv\Scripts\python.exe -m jarvis.cli.agents interrupt --task-id <ID> --feedback "이전 실행 프로세스가 종료된 것을 확인함"
.\.venv\Scripts\python.exe -m jarvis.cli.agents resume --task-id <ID> --feedback "로컬 모델 실행을 복구했으므로 같은 요구사항으로 재시도"
.\.venv\Scripts\python.exe -m jarvis.cli.agents run --task-id <ID>
```

명시적 실행 재시도는 작업 전체 최대 2회이며 resume 승인 시 횟수를 차감합니다. 새 실행 토큰은 다음 admission에서 추가로 예약합니다. 수정 3회 한도·마감·원래 토큰 한도는 유지합니다. 최종 QA 3회 수정 실패나 admission 전 예산 고갈은 재개로 초기화할 수 없습니다. 예산이 모자라 실행되지 못한 재개 승인도 재시도 횟수를 환불하지 않습니다.

## 기억과 보존

장기 기억은 같은 보호 SQLite의 별도 memories 테이블에 둡니다. owner/project/kind/key, 출처·생성/갱신/만료 시각·revision·확인 여부를 저장합니다. owner/project는 로컬 네임스페이스이며 다중 사용자 로그인 인증이 아닙니다.

```powershell
.\.venv\Scripts\python.exe -m jarvis.cli.memory put --project projects/demo --kind preference --key style --value "짧은 함수와 명시적인 타입 표기를 선호" --days 30
.\.venv\Scripts\python.exe -m jarvis.cli.memory list --project projects/demo
.\.venv\Scripts\python.exe -m jarvis.cli.memory confirm --project projects/demo --id <ID> --revision <현재_revision>
.\.venv\Scripts\python.exe -m jarvis.cli.memory delete --project projects/demo --id <ID> --revision <현재_revision>
```

종류는 preference/path/error_solution입니다. 값은 1,000자, 보존 기간은 기본 30일/최대 365일, 전체 1,000개 한도입니다. 인식된 비밀값은 저장 전 거부합니다. 같은 키를 수정하면 확인 상태가 해제되고 revision이 바뀝니다. 지난 revision으로 확인/삭제하는 요청은 거부합니다. 만료 항목은 읽기/쓰기 때 정리합니다.

create에 --memory-project projects/demo (필요 시 --memory-owner)를 명시한 작업에만 같은 범위의 확인된 기억 최대 8개를 넣습니다. 기억은 신뢰되지 않은 선호 데이터로 표시하며 경로나 정책을 실행 권한으로 바꾸지 않습니다. 모델에는 기억 확인/정책 수정 도구를 제공하지 않습니다.

단기 기억은 작업 이력과 결과 요약입니다. 종료 후 기본 7일, 최초 --retention-days로 1~90일을 정합니다. 새 작업 생성 또는 prune에서 만료 종료 작업과 이벤트를 제거합니다. 전체 작업은 1,000개로 제한합니다.

```powershell
.\.venv\Scripts\python.exe -m jarvis.cli.agents forget --task-id <종료된_ID>
.\.venv\Scripts\python.exe -m jarvis.cli.agents prune
```

진행 중 작업은 먼저 끝내거나 취소해야 forget할 수 있습니다. 작업 삭제는 적용된 파일·스냅샷·복구 자료나 별도 장기 기억을 삭제하지 않습니다. SQLite 논리 삭제는 디스크의 암호학적 완전 삭제가 아닙니다. 비밀값 탐지는 최선 노력 방식이며 자격 증명·개인 원문·내부 사고 과정을 넣지 않습니다.

## 비동기 이벤트와 인증된 WebSocket

상태 변경과 이벤트를 같은 SQLite 트랜잭션에 저장합니다. 버전·event_id·task_id·역할·순번·시각·최소 상태 payload만 전송하고 요청 원문·소스·기억 값·인증정보는 스트리밍하지 않습니다. 과거 이벤트 도입 전 작업에 허구의 타임라인을 재구성하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m jarvis.cli.events --task-id <ID> --origin http://127.0.0.1:3000 --port 8787
```

- 서버는 loopback만 사용하며, 지정한 작업 ID와 정확한 로컬 Origin만 허용합니다. 임의 명령/모델/파일 쓰기 엔드포인트는 없습니다.
- 연결 파일은 .jarvis-auth/agent-events-token.txt입니다. 채팅·로그·URL에 내용을 넣지 않습니다. 브라우저 이외 클라이언트는 Authorization: Bearer 헤더, 브라우저는 연결 후 3초 이내 첫 JSON 프레임의 token 필드로 인증합니다. 인증 전 이벤트는 보내지 않습니다.
- 경로는 /tasks/<ID>/events?after=<마지막_순번>입니다. 토큰을 query에 넣으면 거부합니다. 마지막 받은 순번 이후부터 재연결하고 event_id/sequence로 중복 제거합니다. 커서 누락·앞선 커서·마지막 이벤트/현재 상태 불일치는 stream.error 후 연결을 닫으며 명시적 snapshot 복구가 필요합니다.
- 소비자당 최대 32개 페이지를 pull 방식으로 유지합니다. 무제한 메모리 큐를 만들지 않고 느린 소비자는 영속 로그에서 재생합니다. 최대 연결 16개, 입력 프레임 1KiB, 인증 3초·전송 3초·유휴 30초 상한입니다. 유휴 종료 후 마지막 순번으로 재연결할 수 있습니다.
- 종료 이벤트도 보존 기간 동안 저장합니다. 작업 보존 기간이 끝나거나 명시적으로 forget하면 재생할 수 없습니다. 클라이언트는 state snapshot과 보존 정책을 함께 처리해야 합니다.

실제 로컬 모델 작업에서 이벤트 7개와 after=3 재연결 시 4~7 순번을 확인했습니다. 살아 있는 6개 이벤트의 발행→Python WebSocket 클라이언트 지연 p95는 62.485ms였고, 작은 표본입니다. 일반 부하·동시 소비자·실제 HUD 표시 지연 측정은 별도입니다.

근거: [Starlette WebSocket](https://starlette.dev/websockets/), [websockets](https://pypi.org/project/websockets/). 고정 버전 websockets 17.2만 추가했고 기존 전이 의존성은 업그레이드하지 않았습니다.
