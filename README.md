# J.A.R.V.I.S. MCP

대화하고, 허용한 자료와 화면을 이해하고, 일을 수행하며 중요한 내용을 기억하는 개인 비서를 만드는 프로젝트입니다. 현재는 로컬 대화·읽기 도구·작업 관찰과 제한된 직원 실행 기반이 있고, 개인 비서 경험을 단계적으로 연결합니다.

## 시작하기

- [자비스 대화·음성 사용](docs/guides/chat.md)
- [목표·개발 순서·현재 상태](docs/PLAN.md)
- [전체 사용 안내](docs/README.md)
- [개발 규칙](docs/DEVELOPMENT.md) · [프로젝트 지도](AGENTS_CONTEXT.md)

## 실행

Python 3.13 기준입니다. 채팅 화면은 `scripts/start-hud.cmd`로 시작하고 안내에 따라 연결합니다. 로컬 모델 준비와 화면 서버 실행은 별개입니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

위 `server.py`는 stdio MCP 진입점으로 클라이언트 요청을 기다립니다. 기본 데이터는 `JarvisWorkspace/`, 인증은 별도 위치에 둡니다.

## 현재 제공 범위

채팅·기록·제한 읽기 도구, 승인 Obsidian 검색(MCP), Google 읽기 브리핑, 파일/스냅샷/등록 실행, Board·Dispatcher·직원 협업과 HUD를 재사용합니다. 이번 브랜치에는 수동 음성 입력 초안과 기기 한국어 답변 읽기를 추가했습니다. 실제 브라우저 확인과 배포는 별도입니다.

기억 쓰기·채팅의 승인 Vault 검색·화면 질문·모바일·전화·상시 무인 운영이 모두 완성된 상태는 아닙니다. 구현/자동 검사/현장 확인은 [PLAN](docs/PLAN.md)에서 구분합니다. 과거 단계 시험 숫자는 [보관 기록](docs/README.md)에서 확인합니다.

## 개발 확인

```powershell
python scripts/check_docs.py
python scripts/check_architecture.py
node --test tests/frontend/*.cjs
```

Python 검사는 `requirements-dev.txt`를 설치한 뒤 변경 기능에 해당하는 pytest부터 실행합니다. 기존 MCP 진입점과 사용자 데이터는 유지합니다.
