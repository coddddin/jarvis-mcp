# Voice engine spike (not for merge)

Branch `claude/spike-voice-engines`. Board task 887b8923. Measurement scripts and raw results only.

- Venv, model caches and audio live outside the repo in `%LOCALAPPDATA%\JarvisVoiceSpike` (venv, cache, work, samples).
- Test sentences: `sentences.txt` (short, medium, numbers plus loanword).
- Hardware: RTX 4070 Ti SUPER 16GB (driver 610.88), Ryzen 7 9800X3D, 31.1GB RAM.

## Notes
- faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`, which PyAV 19 removed. Workaround: pass decoded numpy audio (soundfile).
- GPU faster-whisper needs pip `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*` (about 2GB) with their `bin` dirs on PATH before import.
- STT input is synthetic (Windows Heami TTS). CER ignores punctuation and spaces, but counts "세 시" vs "3시" as errors.

## Results (2026-10-27, rev 2 after heejun-e8 review)

Sentences: 1 short greeting, 2 medium schedule, 3 numbers plus English (`GitHub PR`, `CPU`, `85%`).
Samples to listen to: `%LOCALAPPDATA%\JarvisVoiceSpike\samples` (heami-N, qwen3-0.6B-sohee-N, qwen3-1.7B-sohee-N).
All audio is synthetic TTS, one voice per engine, no noise or room. 3 sentences means 1 character is about 0.03 CER; differences under 0.05 mean nothing.

### TTS
| Engine | License (commercial) | Korean | Gapless start* | RTF | Memory | Status |
|---|---|---|---|---|---|---|
| Windows Heami (System.Speech) | OS component | yes | ~0.06 s | 0.002-0.011 | 110 MB RAM | measured, robotic |
| Qwen3-TTS 0.6B CustomVoice, Sohee | Apache-2.0 (yes) | yes | 1.6-2.5 s (+0.8-1.1 s leading silence) | 1.23-1.28 | 2.4 GB VRAM | measured |
| Qwen3-TTS 1.7B CustomVoice, Sohee | Apache-2.0 (yes) | yes | 1.1-1.9 s | 1.24-1.26 | 4.2 GB VRAM | measured |
| OpenVoice V2 (MeloTTS KR) | MIT (yes) | yes | - | - | - | not run: source install blocked by permission check |
| CosyVoice2 / Fun-CosyVoice3 | Apache-2.0 (yes) | yes (v3 explicit) | - | - | repo 4.9 / 9.8 GB | not run: same block |
| FireRedTTS-2 | Apache-2.0 (yes) | yes | - | - | ~9 GB VRAM bf16 (card) | not run |
| Kokoro-82M | Apache-2.0 | no | - | - | - | excluded |
| Kyutai TTS 1.6B | CC-BY 4.0 | no (en/fr) | - | - | - | excluded |

*Gapless start = earliest playback start such that every 80 ms frame is ready before it is played, assuming a streaming wrapper (qwen-tts 0.1.1 has none): max over frames of (ready time - frame offset) plus one 4-frame decode. Frame j counts as ready at the end of talker call j+1, because the code predictor fills frame j's remaining codebooks in the next call (modeling_qwen3_tts.py 1665-1692). Time to the first 4 frames alone is ~0.44 s, but with RTF > 1 the buffer then runs dry, so that number is not a usable start time.
- Decoding the first 4 frames alone matches the same span of the full decode (max sample diff 0.0001-0.009), so chunked decoding looks safe.
- 0.6B clips are longer than 1.7B mainly because of 0.8-1.1 s leading and 0.4-1.2 s trailing silence (1.7B: under 0.1 s).
- Step time ~98 ms per 80 ms frame for both sizes: eager HF loop is overhead bound. CUDA graphs or torch.compile are untested.
- Sampling is on (do_sample=True), so lengths vary between runs.
- Intelligibility: STT on Qwen 1.7B Sohee gives CER(num-normalized) 0/0.03/0.00 with large-v3-turbo; it says "GitHub PR" clearly. Heami does not.

### STT
CER = raw; CERn = after mapping number words to digits on both sides (세 시 -> 3시, 두 건 -> 2건, 십오 -> 15, ...).
Input A = Heami clips, input B = Qwen 1.7B Sohee clips (24 kHz resampled to 16 kHz).

| Model | Device | Latency per clip | CERn A s1/s2/s3 | CERn B s1/s2/s3 | Memory |
|---|---|---|---|---|---|
| faster-whisper small int8 | CPU | 0.77-0.94 s | 0/0/0.21 | 0/0.03/0.16 | 354 MB RAM |
| faster-whisper medium int8 | CPU | 2.1-2.7 s | 0/0.03/0.21 | 0/0.03/0.16 | 862 MB RAM |
| faster-whisper small fp16 | GPU | 0.14-0.26 s | 0/0/0.21 | 0/0.03/0.16 | ~0.84 GB VRAM |
| faster-whisper medium fp16 | GPU | 0.24-0.34 s | 0/0.03/0.21 | 0/0.03/0.16 | ~2.2 GB VRAM |
| faster-whisper large-v3-turbo fp16 | GPU | 0.15-0.30 s | 0/0/0.19 | 0/0.03/0.00 | ~2.3 GB VRAM |
| zipformer-ko streaming int8 | CPU | ~7 ms after audio end | 0.15/0/0.37 | - | 224 MB RAM |

- VRAM = total GPU memory.used delta (Windows WDDM gives no per-process figure).
- The zipformer latency excludes end-of-speech detection. In practice both paths add a ~0.5 s endpoint wait, so the real gap to GPU whisper is smaller than the table shows.
- B s2 "내일까지의" appears in every model's output, so it is likely in the TTS audio itself.
- VRAM budget (not co-loaded, sum of measured or file sizes): whisper turbo 2.3 + Qwen 1.7B 4.2 + Ollama qwen2.5-coder:7b ~4.7 (file size) is about 11 GB of 16 GB. Not tested together.

### Wake word (openWakeWord hey_jarvis v0.1, onnx, 1.3 ms per 80 ms frame)
- English TTS (Zira): "Hey Jarvis" 0.999, "Jarvis" 0.998, "Hey Java" 0.997, "Hey jar" 0.986, "Hey Travis" 0.510, "Hey ja" 0.220.
- With `vad_threshold=0.5`: "Hey jar" drops to 0.325, "Hey Java" stays 0.997.
- Korean TTS (Heami) "헤이 자비스" only 0.16-0.28: Korean pronunciation may not trigger at all.
- 84 s of Korean speech (Heami, Sohee): max 0.008, no false trigger. Too short for a per-hour rate.
- Pretrained model license is CC BY-NC-SA 4.0 (non-commercial).
