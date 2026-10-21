# 로컬 리드와 외부 작업자의 협업

[문서 안내](../README.md) · [현재 계획](../PLAN.md) · [협업 검증 기록](../reports/hybrid-team.md)

사용자 지정 구성은 **설치된 로컬 AI가 리드**, 로컬 역할 작업자와 설치된 외부 에이전트가 협업하는 방식입니다. Codex와 Claude Code의 실제 제안 호출과 로컬 리드 협업 연결을 확인했습니다. Claude Code 2.1.302는 사용자 Pro 구독 로그인으로 검증했으며, 시험의 모델 검수는 보류 상태였습니다. 연결 확인은 코드 품질·실행 검증 통과와 다릅니다.

## 흐름

로컬 리드가 최대 3개 작업을 만들고 역할·실행 환경·선행 작업을 지정합니다. 배정 정책은 OpenRig의 역할 선택 소스를 Python으로 이식했습니다. 역할·사용 가능 상태·고정 주소·실행 환경을 검사하고 대기 작업 수, 주소 순으로 담당자를 선택합니다. 사용할 수 없는 환경을 다른 환경으로 조용히 바꾸지 않습니다.

선행 결과가 준비된 작업만 실행하며 독립 작업은 최대 2개를 동시에 처리합니다. 같은 GPU의 로컬 추론은 직렬화합니다. 작업 결과는 ID와 담당자에 귀속돼 다음 작업에 전달됩니다. 별도 문맥의 로컬 검수 역할이 결과를 검토하고, 로컬 리드가 최종 답변을 취합합니다. 같은 로컬 모델을 역할별로 따로 호출하는 구조이며 각 역할에 별도 모델이 설치돼 있다는 뜻은 아닙니다.

검수 판정은 **모델의 제안 검토**입니다. 코드 실행이나 기능 테스트의 증거가 아닙니다. 검수가 보류되면 로컬 리드가 수정할 기존 작업을 선택하거나 중단합니다. 수정은 같은 담당자가 수행하며 해당 결과에 의존하는 후속 작업도 다시 계산합니다. 최대 3회 수정·4회 검수 안에서 진행하되 시간·호출 예산에 먼저 도달하면 중단합니다. 미통과 결과는 `needs_user_input`, 작업자 실패는 `failed`로 남깁니다. 부정확한 검수 의견도 있을 수 있으므로 전체 결과와 오류를 확인해야 합니다.

상한이나 리드 판단으로 중단하면 최종 답변 첫머리에 서버가 중단 이유·수정 횟수·외부 호출 수를 표시합니다. 답변 전달 완료와 협업 검수 통과는 별개입니다.

검수의 `issues`는 미해결 문제 목록입니다. 모델이 `passed=true`와 비어 있지 않은 문제를 함께 반환하면 서버는 통과를 보류하고 수정 또는 사용자 확인으로 보냅니다. 원래 통과 표시도 `model_reported_passed`에 보존하며 `policy_adjustment=unresolved_issues`로 조정 이유를 남깁니다. 이 규칙은 판정 모순만 차단하며 문제 내용의 정확성까지 보장하지 않습니다.

검수 문맥에는 작업당 원문 최대 1500자가 전달됩니다. 원문이 더 길면 `policy_adjustment=review_context_truncated`, `stop_reason=review_context_limit`로 전체 검수를 보류합니다. 보지 못한 부분을 통과 처리하거나 추측으로 자동 수정하지 않습니다. 전체 원문은 작업 결과와 보존 기록에서 확인합니다.

최종 답변은 모델 요약과 각 작업 원문을 함께 제공합니다. 전체 6000자 안에서 원문에 공간을 나누며 잘린 경우 발췌 표시와 전체 기록 안내를 붙입니다. 종합 모델 호출·응답 형식만 실패하면 기존 결과를 제공하고 `synthesis_failed`로 사용자 확인 상태를 남깁니다. 중단 정책은 그대로 적용합니다. 실제 단계 실패는 `failure`에 단계·고정 안내·완료 작업 ID를 기록하며 원시 예외·외부 서비스 응답을 저장하지 않습니다.

## 운영자 실행

프로젝트 루트에서 설치된 Ollama가 실행 중이어야 합니다.

```powershell
# 실행 가능한 역할과 발견된 외부 CLI 확인
.\.venv\Scripts\python.exe -m jarvis.cli.team members

# 로컬 리드가 로컬/Codex 작업자에 작업을 배정
.\.venv\Scripts\python.exe -m jarvis.cli.team run --summary "로컬 조사자가 요구사항을 정리하고 Codex가 Python 함수를 제안해줘."

# 외부 에이전트 없이 로컬 역할만 사용
.\.venv\Scripts\python.exe -m jarvis.cli.team run --local-only --summary "로컬 역할끼리 작은 함수의 설계와 구현 제안을 나눠줘."

# run 시작 시 출력한 conversation_id로 상태와 결과 확인
.\.venv\Scripts\python.exe -m jarvis.cli.team status --conversation-id <ID>

# 저장한 작업 버전·검수 파일의 해시 확인 (모델 호출 없음)
.\.venv\Scripts\python.exe -m jarvis.cli.team verify --conversation-id <ID> --request-id <REQUEST_ID>
```

기존 대화에 요청을 추가하려면 run에 `--conversation-id <ID>`를 전달합니다. IDs가 실행 전에 출력돼 중단 후에도 기록을 확인할 수 있습니다. CLI 파일 존재는 발견 상태이며 계정·서비스가 실제 응답한다는 보장은 아닙니다. 로그인·설치·네트워크 문제가 있으면 실패합니다. 다른 에이전트의 최근 대화를 임의 재개하지 않습니다.

`verify`는 해당 요청의 정해진 버전·검수 경로만 읽고 저장된 SHA256과 비교합니다. `changed`·`missing`·`invalid_reference` 등이 있거나 검사할 기록이 없으면 `not_verified`와 종료 코드 1을 반환합니다. 경로 정책 위반은 검사를 거부합니다. 파일·DB를 수정하거나 모델을 호출하지 않습니다. DB와 파일을 함께 변조하는 경우의 진위 증명이나 코드 정확성 검증은 제공하지 않습니다.

## 채팅 세션과의 접점

### 무료 API 자동 배정·한도 전환 계약

`jarvis.agents.provider_routing`은 외부 모델용 별도 정책 계층입니다. 아직 기존 HybridTeam 실행기나 자동 채팅에 연결하지 않았습니다. `ProviderCandidate`는 runtime/model/capabilities/cost/available/access_verified/verified_at/authentication_required/authenticated/remaining_calls/pending/max_input_tokens/blocked를 받습니다. 설치·목록 발견만으로 access_verified를 설정하지 않습니다. 접근 확인이 24시간을 넘거나 미래 시각이면 제외하고, 무료 기본 정책에서는 유료·구독·비용 미확인 후보를 제외합니다. 이는 호출자가 제공한 확인 정보에 의존하며 현재 가격이나 계정 한도를 자동 검증하는 기능은 아닙니다.

`select_provider(..., capability, input_tokens, now, free_only=True)`는 비용·대기량·ID 순으로 적합한 후보를 선택하고 제외 이유를 반환합니다. 무작위로 모든 모델을 호출하지 않습니다. 사용자의 무료 API 경로에는 Ollama를 등록하지 않습니다. 기존 명시적 로컬 협업 경로를 제거한 것은 아닙니다.

`DispatchBudget`과 `next_provider`/`reserve_attempt`/`finish_attempt`는 요청 전체 최대 3회·기본 12000 토큰 예약을 공유합니다. 실패나 모델 전환에도 예약을 환불하지 않고 실제 토큰이 예약을 넘으면 더 큰 값을 차감합니다. 성공한 요청·진행 중 예약·응답 여부가 불명확한 timeout은 자동 반복하지 않습니다. 한도 초과로 실패가 확인되면 이미 시도한 후보를 제외한 다른 검증된 무료 모델을 선택할 수 있습니다. 무료 후보나 요청 예산이 없으면 중단하며 유료로 승격하지 않습니다.

이 상태는 JSON으로 보존·재구성할 수 있습니다. 호출자는 **요청 ID에 결합해 예약 상태를 원자적으로 저장한 다음 실행**하고, 기존 작업 결과·버전 기록을 유지해야 합니다. 현재 정책 함수는 저장소나 실제 호출을 수행하지 않습니다. 재시작·동시 요청에 대한 영속 실행 보장은 실행기 연결과 저장소 트랜잭션 구현 뒤 검증해야 합니다. 계정 전체 쿼터·제공자의 내부 재시도·실제 청구 상한을 보장하지 않습니다.

`HybridTeam(store, infer).metadata()`와 `run(conversation_id, request_id, message, external=True) -> (reply, traces)`를 제공합니다. `infer`는 로컬 모델의 임의 JSON schema 추론 함수입니다. 저장소의 `begin(..., mode='team'|'team-local')`로 요청을 먼저 승인하고 `progress()`로 역할별 진행·출력을 저장합니다. 기존 대화는 `mode='chat'`로 호환됩니다.

채팅 개선 세션은 app/chat.py·workers/chat_step.py·채팅 테스트를 담당합니다. 이 협업 작업에서는 해당 파일을 추가 수정하지 않았습니다. HUD·이벤트·기존 작업 제어 연결과 운영 중 화면 재시작은 담당 세션과 합의 후 수행합니다. 현재 운영자 CLI로 실행할 수 있으며, 화면에 협업 모드 선택이 제공된다고 주장하지 않습니다.

## 범위와 제한

- 요청 전체 기한 600초, 전체 단계 최대 20회, 수정 포함 외부 호출 최대 3회, 외부 호출당 최대 180초입니다. 통신 실패를 자동 재시도하지 않습니다. 검수에 따른 내용 수정도 이 공유 상한을 소비합니다.
- 로컬 추론 예약 예산은 기존 대화 저장소에서 영속 차감합니다. 외부 호출은 Guardrails 시간창 예산의 민감 등급도 소비합니다([시스템 도구](system-tools.md)). 외부 서비스의 토큰·금액을 계측하거나 금액 상한을 보장하는 기능은 없습니다. 기존 계정 사용량이 적용됩니다.
- 외부에 전달하는 것은 현재 요청과 해당 작업에 필요한 생성 결과뿐입니다. 프로젝트 파일을 자동 수집하거나 인증 파일을 프롬프트에 포함하지 않습니다. 인식된 비밀값을 가리지만 탐지는 완전하지 않으므로 민감정보를 입력하지 않습니다.
- Codex는 고정 native 실행 파일, 사용자 설정 미로딩, ephemeral, 읽기 전용 sandbox, 웹/셸/앱/플러그인/후크/자체 하위 에이전트 기능 비활성 옵션으로 제안만 요청합니다. 사용자 전역 설정·MCP 등록을 변경하지 않습니다.
- Claude 어댑터는 도구 없음·빈 MCP 구성·사용자 설정 미로딩·단일 print 요청을 사용합니다. 실제 설치·로그인·지원 옵션은 환경별 검증이 필요합니다.

Windows Claude 발견은 PATH와 사용자 `.local/bin/claude.exe`, 고정 WinGet 패키지의 native 실행 파일을 확인합니다. PATH가 아직 갱신되지 않아도 발견할 수 있습니다. 링크 실행 파일은 허용하지 않으며 작업공간 내부 실행 파일 거부 등 기존 실행 경계를 유지합니다. 외부 계정 인증과 실제 접근 성공은 단순 발견 목록과 별도입니다.
- 경로 가드레일과 stop 검사, 요청의 영속 단일 소유권을 적용합니다. 같은 요청은 다시 실행하지 않습니다. 실패해도 성공한 형제 작업 결과는 남깁니다. 장애 후 자동 재개는 없습니다.
- 현재 작업·결과는 기존 `.jarvis-chat/conversations.sqlite3`에 저장됩니다. 요청별 폴더에는 각 작업 버전·검수 원문과 native 작업자의 schema/answer 자료를 덮어쓰지 않고 보존합니다. `assignments[].versions`와 `review_history`에 경로·SHA256을 남기며 자동 삭제하지 않습니다.
- 시간 초과/중단 시 직접 시작한 작업자 프로세스를 종료합니다. 외부 서버의 즉시 취소·과금 중단이나 모든 OS 자손 프로세스 격리를 보장하지 않습니다.
- 검수·취합에는 문맥 크기에 맞춘 결과 발췌를 전달하고 잘림 여부를 명시합니다. 전체 출력은 협업 기록에 보존합니다. 작은 로컬 모델의 계획·검수 정확성에는 한계가 있습니다.

파일 적용·프로그램 실행·사용자 브라우저 로그인·쿠키 접근 권한을 이 엔진에 주지 않았습니다. 기존 개발 작업의 백업/적용 CLI와 QA 실행 경로는 유지합니다.

## 참고 소스

[mvschwarz/openrig](https://github.com/mvschwarz/openrig) commit `0b1d8b3dc1b418f1568e571b1df174041fed77f2`의 workflow-role-resolver.ts를 team_routing.py로 이식했습니다. queue-repository.ts·workflow-types.ts·workflow-runtime.ts·독립 QA 역할 문서는 작업 소유권·선행 조건·검수 분리 설계에 참고했습니다. Apache-2.0 원문과 변경 고지는 프로젝트 third_party/openrig에 보존합니다. OpenRig의 tmux daemon 전체를 설치한 것은 아닙니다.

Codex 실행 계약은 [공식 비대화형 실행 문서](https://learn.chatgpt.com/docs/non-interactive-mode), Claude 옵션은 [공식 CLI 문서](https://code.claude.com/docs/en/cli-reference)를 확인했습니다. 실제 설치된 Codex 도움말에 있는 옵션을 사용합니다.
