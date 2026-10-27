# Voice engine spike (not for merge)

Branch `claude/spike-voice-engines`. Board task 887b8923. Measurement scripts and raw results only.

- Venv, model caches and audio live outside the repo in `%LOCALAPPDATA%\JarvisVoiceSpike` (venv, cache, work, samples).
- Test sentences: `sentences.txt` (short, medium, numbers plus loanword).
- Hardware: RTX 4070 Ti SUPER 16GB (driver 610.88), Ryzen 7 9800X3D, 31.1GB RAM.

## Notes
- faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`, which PyAV 19 removed. Workaround: pass decoded numpy audio (soundfile).
- GPU faster-whisper needs pip `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*` (about 2GB) with their `bin` dirs on PATH before import.
- STT input is synthetic (Windows Heami TTS). CER ignores punctuation and spaces, but counts "세 시" vs "3시" as errors.
