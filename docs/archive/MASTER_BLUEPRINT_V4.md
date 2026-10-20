# 프로젝트 J.A.R.V.I.S. 고도화 통합 계획서 (Master Blueprint V4 - Practical Utility Expanded)

2026-10-17 사용자 제공 원안. 복사 안내와 바깥 YAML 코드 펜스만 제거하고 Markdown으로 보관한다. 아래 완료 표현은 원안이며 실제 구현 상태는 `../PLAN.md`를 따른다. 요청에 제공된 범위는 Phase 2의 항목 5까지다.

## 1. 프로젝트 비전 및 목표

- 최종 목표: 사용자의 PC 환경을 수족처럼 부리며, 지식(세컨드 브레인)과 음성·시각 인터페이스를 통해 실시간으로 상호작용하는 개인용 슈퍼 AI 에이전트 시스템 구축.
- 핵심 가치:
  - 극강의 실용성 (일상의 귀찮은 작업 자동화)
  - 안전성 (화이트리스트 기반 가드레일, 예산 제한, 킬스위치)
  - 효율성 (툴별 독립 플러그인 구조 및 AGENTS_CONTEXT.md를 통한 토큰 절약)

## 2. 프로젝트 아키텍처 (Token-Saving Plugin Architecture)

새로운 도구가 추가되어도 기존 코드를 건드리지 않고, 도구 파일 1개만 전달하여 토큰을 90% 이상 절약하는 구조입니다.

```text
JarvisWorkspace/
├── .venv/                      # 가상환경
├── requirements.txt            # 의존성 패키지 목록
├── AGENTS_CONTEXT.md           # [토큰 절약] 프로젝트 전체 구조 및 현재 진행 상황 (50줄 요약 지도)
├── server.py                   # [메인] 툴 자동 로드만 수행하는 경량화된 실행 파일
├── guardrails.py               # [보안] 화이트리스트, 20회 예산, .jarvis-stop 킬스위치
│
└── tools/                      # 🔌 실용성 중심의 독립 플러그인 폴더
    ├── __init__.py             # tools 폴더 내부 툴 자동 바인딩 (Auto-loader)
    ├── shell_tool.py           # [Phase 1] 셸 제어 도구
    ├── system_tool.py          # [Phase 1] 시스템 리소스 & 스크린샷 도구
    ├── file_organizer_tool.py  # [Phase 2 신규] 파일/다운로드 폴더 자동 분류 및 정리 도구
    ├── debug_assistant_tool.py # [Phase 2 신규] 코드/에러 로그 분석 및 수정 도구
    ├── workflow_launcher_tool.py# [Phase 2 신규] 작업 환경 일괄 세팅(앱/브라우저/서버) 도구
    ├── snapshot_tool.py        # [Phase 2 신규] 프로젝트 자동 스냅샷 백업 & 원클릭 복원 도구
    └── search_tool.py          # [Phase 2] 웹 탐색 및 외부 정보 수집 도구
```

## 3. 개발 단계별 로드맵 (Phased Roadmap)

### Phase 1 — 코어 백엔드, 가드레일 및 모듈화 리팩토링 (완료 및 정비 단계)

- 목표: 안전 가드레일 기반 시스템 제어 및 `tools/` 독립 플러그인 구조 완료.
- 주요 기능:
  - FastMCP(server.py) 및 오토 로더(`tools/__init__.py`) 구축.
  - 안전 가드레일(guardrails.py): 화이트리스트 셸, 예산 제한(20회), `.jarvis-stop` 킬스위치.
  - 독립 도구: `shell_tool.py`, `system_tool.py` (psutil, 스크린샷 타임아웃 방어막).
  - 프로젝트 요약 지도(`AGENTS_CONTEXT.md`) 세팅 및 `docs/` 이력 아카이브 분리.

### Phase 2 — 실용성 특화 비서 툴 & 백엔드 확장 (진행 중 / 다음 단계)

- 목표: 사용자의 실제 일상 및 개발 업무에서 즉각적인 유용성을 제공하는 핵심 비서 기능 구축.
- 주요 기능 (실용성 대폭 보강):
  1. **스마트 다운로드 & 파일 자동 정리 (`file_organizer_tool.py`)**:
     - 지저분한 '다운로드' 폴더나 특정 경로의 파일들을 확장자, 생성 날짜, 문서 내용별로 자동 분류하여 폴더 이동.
     - 이름에 '최종', '복사본' 등이 붙은 파일 정리 및 정리 리포트 제공.
  2. **에러 분석 및 자동 수정 보조 (`debug_assistant_tool.py`)**:
     - 실행 중 발생한 에러 로그나 텍스트를 읽어 원인을 즉시 파악하고 수정 코드를 작성/보고.
  3. **원클릭 작업 공간 런처 (`workflow_launcher_tool.py`)**:
     - “자비스, 개발 시작해” 한마디에 편집기(VS Code), 개발 서버, 관련 웹사이트/문서 탭을 동시에 일괄 실행.
  4. **프로젝트 자동 백업 & 복원 (`snapshot_tool.py`)**:
     - 주요 코드 수정 전 작업 공간 전체를 ZIP 압축 백업하고, 문제 발생 시 단 한 마디로 이전 상태로 안전 복원.
  5. **웹 탐색 및 정보 수집 (`search_tool.py`)**:
     - 지정된 최신 뉴스, 기술 블로그, 날씨 등을 탐색하여 텍스트 파일로 요약 제공.
