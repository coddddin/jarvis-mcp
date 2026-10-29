# 웹 작업 벤치마크 (10과제)

웹 에이전트 PR(M2 이후)이 같은 기준으로 "N/10"을 보고하도록 만든 재실행용 벤치마크입니다. 과제는 `scripts/web_eval/tasks.json`, 채점 규칙은 `scripts/web_eval/score.py`, 실행기는 `scripts/web_eval/run.py`입니다.

## 과제
- 한국어 정보 질의 8개(그중 하나는 링크를 2번 이상 따라가야 함), 영어 1개, 작은 공개 파일 내려받기 1개.
- 기대값은 2026-10-29에 확인했습니다. 운임·금리·버전처럼 바뀌는 값은 모든 드라이버가 같은 과제에서 실패하기 시작하면 다시 확인해 고칩니다. `version`을 올리면 이전 점수와 비교하지 않습니다.

## 채점 (과제당 통과/실패)
- 답이 비어 있지 않고, `answer_all`의 정규식이 모두 답에 맞아야 합니다.
- `source_hosts`의 공식 출처가 답에 인용되었거나 도구 호출로 실제 방문되어야 합니다. 검색 결과 페이지는 출처로 치지 않습니다.
- 링크 따라가기 과제는 검색 페이지가 아닌 페이지를 `min_visited_pages`개 이상 방문해야 합니다.
- 내려받기 과제는 허용 확장자(.csv, .pdf)이고 5MB 이하인 새 파일이 있어야 하며, Microsoft Defender 검사(`MpCmdRun -Scan -ScanType 3 -File ... -DisableRemediation`)에서 깨끗해야 합니다. 내려받은 파일은 열거나 실행하지 않습니다.

## 드라이버
- `claude`: Claude Code CLI(구독)에 `--mcp-config`로 아래 두 서버만 붙입니다. 내장 도구는 없고(`--tools ""`), 단계마다 확인하지 않습니다(`--permission-mode dontAsk`). 품질 기준선입니다.
- `qwen-raw`: Ollama `qwen3.5:9b`가 최소 MCP 클라이언트로 같은 서버를 직접 씁니다. 자비스 쪽 도움(스냅숏 줄이기, 단계 계획, 재시도)은 없습니다.
- `jarvis`: 자비스 웹 작업 에이전트([웹 작업 에이전트](web-agent.md))가 qwen3.5:9b(로컬 채팅 일꾼)로 돕니다. Playwright는 자비스 MCP 클라이언트로 붙이며 HUD 승인과 같은 방식으로 도구를 고정합니다. 저장소 venv의 python으로 실행합니다.

## 서버와 경계
- `@playwright/mcp@0.0.83`: `--headless --isolated --browser msedge --no-webmcp --image-responses omit --output-dir <과제 폴더>\downloads`. 페이지 스크립트 실행·업로드·브라우저 설치 도구는 모델에게 주지 않습니다.
- `mcp-server-fetch==2026.10.10`: 별도 venv에서 `python -m mcp_server_fetch`로 실행합니다. robots.txt를 따르고 사설 주소는 기본으로 거부합니다.
- 검색은 브라우저로 Bing 결과 페이지를 엽니다. Google은 자동 조회가 막히고(첫 요청에 캡차) 이용약관·robots.txt도 금지합니다.
- 모든 실행과 결과는 저장소 밖 `%LOCALAPPDATA%\JarvisWorktrees\web-eval`에만 씁니다. 로그인·개인정보·jarvis MCP는 쓰지 않습니다.

## 실행
```
python scripts/web_eval/run.py --setup
%LOCALAPPDATA%\JarvisWorktrees\web-eval\fetch-venv\Scripts\python.exe scripts/web_eval/run.py --driver qwen-raw
python scripts/web_eval/run.py --driver claude t01 t05
<저장소>\.venv\Scripts\python.exe scripts/web_eval/run.py --driver jarvis --repeat 3
python scripts/web_eval/run.py --rescore <결과 폴더>
```
결과 폴더마다 과제별 `record.json`(답, 도구 호출, 턴, 시간, 메모, 점수)과 `summary.json`(N/총수)이 남습니다. 과제당 상한은 10분과 25턴입니다.

qwen은 실행마다 결과가 달라서(같은 과제가 한 번은 실패, 다음엔 통과), 점수를 보고할 때는 `--repeat 3`으로 세 번 돌린 중앙값을 씁니다.

## 기준 점수 (2026-10-29, 처음 측정)
- `claude`: 9/10. t10은 파일을 받았지만(Defender 통과), 그 뒤 Playwright 탭 닫기 호출이 멈춰 10분 상한에 걸렸습니다.
- `qwen-raw`: 2/10. 생각만 하고 빈 답 내기, 스냅숏 ref 없이 클릭하기, 같은 호출 반복, 거대한 스냅숏(17만~48만 자)으로 문맥이 가득 차는 문제, 공식이 아닌 출처 사용이 주요 실패입니다.
- 자세한 분석은 게시판 작업 "Web surfing: research"에 있습니다.
