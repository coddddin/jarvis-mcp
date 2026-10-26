# Graphify 코드 분석 파일럿 — 2026-10-24

## 결정

J2 문맥 선택 / J5 AST의 보조 탐색 후보로 유지한다. 자동 수정의 유일한 영향 분석기나 기존 검색의 대체재로 활성화하지 않는다. J1 디스패처 실연동 우선순위는 유지한다. 실제 Vault, MCP 등록, 에이전트 설정, Git hooks, 스케줄러, 유료 Cloud는 변경하지 않았다.

## 재현 범위

- Graphify `graphifyy==0.9.95`, Python 3.12, Linux 격리 venv.
- 기준 원격 PR #13 head: `9316b0a1207b5b05c783e2a61f74f40baeade4fd`.
- 실행한 로컬 미러 commit: `25d8168e1b2e6cacc8b1f4325273d52be84ce23e`, tree `c77f16e340802aeec5bedc1ce87e532302494c24`.
- 원격 tree와 파일 비교: 대상 194개 중 20개 동일, 174개는 미러에 마지막 개행 하나가 추가됨. 그 외 내용 차이 없음. 원격 commit과 로컬 commit은 서로 다른 이력이다.
- `jarvis/`, `tests/`의 커밋된 일반 `.py`/`.js` 파일만 별도 디렉터리에 복사. 미커밋 파일, symlink, Vault, 인증 파일, 설정, 문서 제외.
- `GRAPHIFY_BACKEND=none`, `GRAPHIFY_QUERY_LOG_DISABLE=1`, 새 HOME, API 자격증명을 상속하지 않는 환경 사용. 애플리케이션 설정이며 OS 네트워크 샌드박스 증명은 아니다.

재현 스크립트: [scripts/graphify_pilot.py](../../../scripts/graphify_pilot.py). 다음 명령은 별도 실험 환경에서 운영자가 명시적으로 실행한다. 프로젝트 의존성에는 추가하지 않는다.

```bash
python -m venv ../graphify-venv
../graphify-venv/bin/python -m pip install graphifyy==0.9.95
../graphify-venv/bin/python scripts/graphify_pilot.py --output ../graphify-run-new
```

Windows에서는 venv의 `Scripts/python.exe` 경로를 사용한다. Windows 실실행은 미검증이다. 출력 디렉터리는 새 경로여야 하며 기존 출력을 덮어쓰거나 삭제하지 않는다. 의존성 전이 버전 전체를 잠근 환경은 아니므로 다른 환경의 속도/추출 결과는 재검증해야 한다.

## 측정 결과

단일 실행의 관측치이며 반복 벤치마크나 운영 SLA가 아니다. 설치 시간은 제외했다.

| 항목 | 결과 |
| --- | --- |
| 코드 파일 | 194 |
| 노드 / 관계 | 2,445 / 7,890 |
| 추출 시간 | 3.249초 |
| 넓은 질의 3개 | 각각 0.319 / 0.310 / 0.406초 |
| 1,200 토큰 예산에서 잘림 | 3개 모두 발생 |
| 좁은 explain 3개 | 0.371 / 0.268 / 0.260초 |
| 없는 이름 | exit 4 확인 |
| 모호한 build 이름 | exit 3, 후보 42개 확인 |
| 추출기가 보고한 입력/출력 모델 토큰 | 0 / 0 |

비교한 `rg` 세 검색은 약 0.005~0.007초, 출력 728 / 3,212 / 1,367문자였다. Graphify의 넓은 질의는 약 4,100문자씩이었다. 검색 결과 형식과 의미가 달라 이 수치를 절감률로 환산하지 않는다. 실제 에이전트가 읽는 원문, 재질의, 인덱스 갱신까지 포함한 토큰 절감은 아직 미측정이다.

## 원문 대조: 포착과 누락

| 관계 | 그래프 결과 |
| --- | --- |
| dispatch_policy.build_command → worker_adapters.adapter | calls / EXTRACTED 포착 |
| ObsidianStore → KnowledgeStore | inherits / EXTRACTED 포착 |
| board_claim → BoardStore.claim | 직접 호출 관계 누락 |
| search_obsidian_vault → ObsidianStore.search | 직접 호출 관계 누락 |

누락 두 건은 원문에 실제 호출이 존재한다. context 속성/클로저를 통한 객체 메서드 호출의 정적 연결 한계로 판단하며, 상세 upstream 원인은 확정하지 않았다. 특히 `search_obsidian_vault` explain은 build의 contains만 보여 준다. 결과가 짧다는 이유로 필요한 문맥을 모두 확보했다고 판정하면 안 된다.

`--no-cluster` 출력은 nodes/edges/hyperedges/input_tokens/output_tokens 구조였고 README의 graph.schema_version 메타데이터가 없었다. 향후 어댑터는 이 원시 형식을 명시적으로 검증하고 버전/원본 commit/파일 digest를 별도 보관해야 한다.

## 검증과 보안 한계

- 추출 + 질의 8개가 기대 종료 코드로 완료. 질의 3개의 잘림은 성공으로 숨기지 않고 metrics에 기록한다.
- 출력 경로의 저장소 내부 지정 거부, 기존 출력 경로 재사용 거부: 2개 검사 통과.
- architecture 검사, 문서 검사, diff 공백 검사 수행. 운영 코드 변경이 없어 전체 자비스 회귀 테스트는 재실행하지 않는다.
- 코드 스냅샷 정책은 비밀 탐지기가 아니다. 이미 소스에 커밋된 비밀까지 제거하지 않는다. 그래프/출력은 소스와 같은 접근 제한으로 보관해야 한다.
- Graphify query 로그 비활성화와 별개로 파일럿은 실험 질의 결과를 지정 출력에 저장한다. 사용자 자유 질의나 Vault 내용을 수집하는 서비스는 아니다.
- 다운로드/설치는 네트워크를 사용했다. backend none과 토큰 0은 코드가 외부로 절대 전송되지 않았다는 네트워크 감사 결과가 아니다.
- 실제 Board 연결 없음. 직원 발송/claim/착수, 데스크톱 설치/미커밋 상태, Windows, 실제 Vault, 실행 추적은 검증하지 않았다.

## 다음 적용 조건

### 2026-10-26 보존·실패 기록 보완

현재 main에 실험 도구와 이 과거 보고서만 보존한다. 위 수치는 2026-10-24 환경의 기록이며 최신 main에서 Graphify를 다시 실행한 결과가 아니다. 운영 MCP, Dispatcher, 프로젝트 의존성에 연결하지 않는다.

추출·질의 시간초과와 실행 오류는 종료 코드·소요시간·고정 오류 분류로 남긴다. 그래프 파일 누락·잘못된 JSON/구조도 실패로 기록하고, 출력 폴더에 쓸 수 있는 한 `metrics.json`을 보존한다. 출력 저장 자체가 실패하면 성공을 주장하지 않는다. commit과 tree는 동일 고정 commit에서 읽는다. 검증은 Graphify를 설치하거나 실행하지 않는 합성 회귀 검사로 별도 수행한다.

1. 2개 누락 관계를 회귀 사례로 유지하고 원문 검색 fallback을 필수로 한다.
2. 그래프 결과는 후보 파일을 찾는 읽기 전용 참고 자료로만 사용한다. 추론 관계는 별도 표시하고, 원본이 바뀌거나 결과가 잘렸으면 완전한 영향 목록으로 제시하지 않는다.
3. 작은 작업 표본에서 정답/누락, 사용자 재지시, 총 토큰 및 갱신 비용을 비교한 뒤 J2 조회 어댑터 승격을 판단한다.
4. 승인된 Obsidian 문서 확장은 그 뒤 별도 평가한다. 이번 단계에서 Vault 읽기/내보내기/쓰기 권한을 추가하지 않는다.

기준 소스: [Graphify 공식 저장소](https://github.com/Graphify-Labs/graphify). 외부 문서의 자동 설치 지시를 실행 권한으로 취급하지 않았다.
