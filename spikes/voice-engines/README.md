# Voice engine spike (not for merge)

Branch `claude/spike-voice-engines`. Board task 887b8923. Measurement scripts and raw results only.

- Venv, model caches and audio live outside the repo in `%LOCALAPPDATA%\JarvisVoiceSpike` (venv, cache, work, samples).
- Test sentences: `sentences.txt` (short, medium, numbers plus loanword).
- Hardware: RTX 4070 Ti SUPER 16GB (driver 610.88), Ryzen 7 9800X3D, 31.1GB RAM.

## Notes
- faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`, which PyAV 19 removed. Workaround: pass decoded numpy audio (soundfile).
- GPU faster-whisper needs pip `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*` (about 2GB) with their `bin` dirs on PATH before import.
- STT input is synthetic (Windows Heami TTS). CER ignores punctuation and spaces, but counts "세 시" vs "3시" as errors.

## Results (2026-10-27)

Sentences: 1 short greeting, 2 medium schedule, 3 numbers plus English (`GitHub PR`, `CPU`, `85%`).
Samples to listen to: `%LOCALAPPDATA%\JarvisVoiceSpike\samples` (heami-N, qwen3-0.6B-sohee-N, qwen3-1.7B-sohee-N).

### TTS
| Engine | License (commercial) | Korean | First sound | RTF | Memory | Status |
|---|---|---|---|---|---|---|
| Windows Heami (System.Speech) | OS component | yes | ~0.06 s (full sentence) | 0.002-0.011 | 110 MB RAM | measured, robotic |
| Qwen3-TTS 0.6B CustomVoice, Sohee | Apache-2.0 (yes) | yes | ~0.35 s est.* | 1.23 | 2.5 GB VRAM | measured |
| Qwen3-TTS 1.7B CustomVoice, Sohee | Apache-2.0 (yes) | yes | ~0.35 s est.* | 1.24 | 4.2 GB VRAM | measured |
| OpenVoice V2 (MeloTTS KR) | MIT (yes) | yes | - | - | - | not run: source install blocked by permission check |
| CosyVoice2 / Fun-CosyVoice3 | Apache-2.0 (yes) | yes (v3 explicit) | - | - | repo 4.9 / 9.8 GB | not run: same block |
| FireRedTTS-2 | Apache-2.0 (yes) | yes | - | - | ~9 GB VRAM bf16 (card) | not run |
| Kokoro-82M | Apache-2.0 | no | - | - | - | excluded |
| Kyutai TTS 1.6B | CC-BY 4.0 | no (en/fr) | - | - | - | excluded |

*Qwen3-TTS pip package (qwen-tts 0.1.1) has no streaming audio output. First sound = time to 4th codec frame (about 0.32 s audio) plus decoding those frames, i.e. what a streaming wrapper could reach. Step time is ~98 ms per 80 ms frame for both sizes, so the eager HF loop is overhead bound, not compute bound; CUDA graphs or torch.compile are untested.

### STT (Heami synthetic input, CER ignores spaces/punct, counts 세 시 vs 3시)
| Model | Device | Latency per clip | RTF | CER s1/s2/s3 | Memory |
|---|---|---|---|---|---|
| faster-whisper small int8 | CPU | 0.80-0.90 s | 0.10-0.15 | 0.00/0.03/0.24 | 354 MB |
| faster-whisper medium int8 | CPU | 2.4-2.7 s | 0.30-0.46 | 0.00/0.07/0.21 | 863 MB |
| faster-whisper small fp16 | GPU | 0.14-0.18 s (1.0 s first call) | 0.02 | 0.00/0.03/0.24 | - |
| faster-whisper medium fp16 | GPU | 0.24-0.34 s | 0.04-0.06 | 0.00/0.07/0.21 | - |
| faster-whisper large-v3-turbo fp16 | GPU | 0.15-0.32 s | 0.02-0.06 | 0.00/0.03/0.21 | - |
| zipformer-ko streaming int8 | CPU | ~7 ms after end of speech | 0.02 | 0.15/0.00/0.45 | 224 MB |
All engines misread "GitHub PR" (Heami itself pronounces it oddly). Zipformer is weak on loanwords and English.

### Wake word (openWakeWord hey_jarvis v0.1, onnx, 1.3 ms per 80 ms frame)
- English TTS (Zira): "Hey Jarvis" 0.999, "Jarvis" 0.998, "Hey Java" 0.997, "Hey jar" 0.986, "Hey Travis" 0.510, "Hey ja" 0.220.
- With `vad_threshold=0.5`: "Hey jar" drops to 0.325, "Hey Java" stays 0.997.
- Korean TTS (Heami) "헤이 자비스" only 0.16-0.28: Korean pronunciation may not trigger at all.
- 84 s of Korean speech (Heami, Sohee): max 0.008, no false trigger. Too short for a per-hour rate.
- Pretrained model license is CC BY-NC-SA 4.0 (non-commercial).
