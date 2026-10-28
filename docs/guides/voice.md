# 로컬 음성 (hud --voice)

[문서 안내](../README.md) · [자비스 전용 채팅](chat.md) · [로컬 작업 HUD](local-hud.md)

어시스턴트 화면(`/assistant`)에서 Space를 누른 채 말하면 자비스가 받아 적어 질문하고, 답변을 문장 단위로 소리 내어 읽습니다. 마이크·받아쓰기·음성 합성은 모두 이 PC에서만 실행합니다. 음성은 저장하지 않고, 받아 적은 글과 읽는 답변을 로그에 남기지 않습니다.

## 준비

1. 받아쓰기·마이크 패키지를 설치합니다. GPU가 없거나 CUDA를 쓸 수 없으면 자동으로 CPU 모델(small, int8)을 씁니다.

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
   ```

2. (선택) 자비스 목소리 CosyVoice 3. VoiceStudio가 설치한 CosyVoice를 자비스 전용 사본으로 하드 링크합니다. 디스크를 더 쓰지 않고, 내려받지 않으며, VoiceStudio를 지우거나 업데이트해도 사본은 남습니다. 사본에만 upstream이 고정한 transformers를 설치합니다.

   ```powershell
   .\.venv\Scripts\python.exe scripts\voice\link_cosyvoice.py
   $py = "$env:LOCALAPPDATA\Jarvis\voice\cosyvoice\venv\Scripts\python.exe"
   & $py -I -m ensurepip
   & $py -I -m pip install transformers==4.51.3
   ```

   사본의 패키지 목록은 `scripts/voice/cosyvoice-lock.txt`에 있습니다. 참조 목소리 `%LOCALAPPDATA%\Jarvis\voice\jarvis-ref-en.wav`(실제 배우가 아닌 새로 설계한 목소리)가 필요합니다. 사본이나 참조 파일이 없으면 Windows 음성 Heami로 읽습니다.

3. HUD를 음성과 함께 켭니다. 패키지가 없으면 안내 한 줄을 출력하고 음성 없이 계속 실행합니다.

   ```powershell
   .\.venv\Scripts\python.exe -m jarvis.cli.hud --voice --open
   ```

   [데스크톱 앱](desktop-app.md)은 트레이 메뉴의 **음성**이 켜져 있으면(기본) 서버를 `--voice`로 실행합니다.

## 사용

- **Space를 누른 채 말하기**: 놓으면 받아 적어 바로 질문합니다. 말을 멈추고 1.2초 지나거나 30초가 되면 스스로 멈춥니다. 입력창·버튼에 포커스가 있을 때 Space는 원래 동작을 합니다. 마이크 버튼은 눌러서 시작하고 다시 눌러서 보냅니다.
- **답변 읽기**: 작성 중인 초안부터 문장이 끝나는 대로 읽고, 최종 답변의 나머지를 이어 읽습니다. 숫자는 한국어로 바꿔 읽습니다(12통 → 열두 통, 2026년 → 이천이십육년). 코드 블록과 링크 주소는 읽지 않습니다.
- **끼어들기**: 읽는 중에 Space를 누르면 바로 멈추고 듣기 시작합니다. Esc는 듣기와 읽기를 모두 멈춥니다. 화면의 답변 작성은 계속됩니다.
- 음성 엔진을 불러오는 동안(처음 약 15~40초)은 입력창을 쓰라는 안내가 보입니다. 화면을 닫거나 다른 창이 연결을 가져가면 마이크를 닫고 읽기를 멈춥니다.

## 구성과 경계

- 받아쓰기: faster-whisper. GPU에서 large-v3-turbo(int8 가중치, float16 계산, 약 1.2GB), 아니면 CPU small(int8). 모델은 `%LOCALAPPDATA%\Jarvis\voice-models` 한 곳에 두고, 처음 한 번만 내려받습니다(약 1.6GB). 이후에는 `local_files_only`로 먼저 불러와 오프라인 재시작에서도 내려받지 않습니다.
- 음성 합성: CosyVoice 3(Fun-CosyVoice3-0.5B)을 프로젝트 venv가 아닌 사본의 Python 3.10에서 별도 프로세스(`jarvis/workers/cosyvoice_tts.py`)로 실행합니다. 표준 입출력으로 문장(JSON)을 보내고 PCM을 받습니다. HUD가 끝나면 입력이 닫혀 함께 끝납니다. GPU에서는 llm·flow 모듈을 반정밀도(fp16)로 바꿔 올리고 보코더(hift)만 fp32로 둡니다. 예열 음성이 비정상(무한대·무음)이거나 실패하면 fp32로 다시 불러옵니다. 사용 중인 정밀도는 음성 상태(`precision`)에 표시됩니다. 시작과 예열에 약 15초, 이후 한 문장에 약 1.5초입니다. 시작에 실패하거나 도중에 멈추면(한 문장 60초 초과 등) Heami로 바꿔 같은 문장부터 이어 읽고, HUD를 다시 켤 때까지 Heami를 씁니다.
- 연결: `/voice` WebSocket 하나. 작업 이벤트와 같은 검사(정확한 로컬 Origin, Host, 쿼리 없음, 첫 메시지의 HUD 토큰)를 거치며 한 번에 한 화면만 연결됩니다. 메시지는 1KB 이하이고 답변은 250자씩 나눠 보냅니다.
- GPU 메모리(2026-10-28 측정, 장치 사용량 증가분): 받아쓰기 약 1.2GB, CosyVoice fp16 약 2.0GB(fp32로 되돌아가면 약 3.6GB)가 채팅 모델(약 6.7GB)과 함께 올라갑니다. 16GB GPU에서 확인했습니다.

## 확인 기록 (2026-10-28, RTX 4070 Ti SUPER)

- CosyVoice 사본: transformers 4.51.3에서 VoiceStudio 패치 없이 로드. 한국어 문장 RTF 0.45~0.75, 24kHz.
- 받아쓰기: 첫 실행에서 모델을 내려받고(41초), `HF_HUB_OFFLINE=1`로 다시 시작했을 때 내려받지 않고 2초 만에 GPU로 로드. 합성 음성 4.2초를 0.2초에 정확히 받아 적음.
