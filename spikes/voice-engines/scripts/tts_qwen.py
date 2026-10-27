import os, sys, time, json
from pathlib import Path
B = Path(r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike")
os.environ.setdefault("HF_HOME", str(B / "cache/hf"))
import torch, numpy as np, soundfile as sf, psutil
from qwen_tts import Qwen3TTSModel

size = sys.argv[1]  # 0.6B or 1.7B
repo = f"Qwen/Qwen3-TTS-12Hz-{size}-CustomVoice"
texts = (B / "work/sentences.txt").read_text(encoding="utf-8").splitlines()
t = time.perf_counter()
tts = Qwen3TTSModel.from_pretrained(repo, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
load = time.perf_counter() - t
inner = tts.model
talker = inner.talker
steps = []
def _hook(mod, inp, outp):
    torch.cuda.synchronize(); steps.append(time.perf_counter())
talker.register_forward_hook(_hook)
tok = inner.speech_tokenizer
captured = {}
orig_dec = tok.decode
def cap_dec(items, *a, **k):
    captured["codes"] = items[0]["audio_codes"]; return orig_dec(items, *a, **k)
tok.decode = cap_dec
print("speakers", tts.get_supported_speakers(), file=sys.stderr)
tts.generate_custom_voice(text="테스트입니다.", speaker="Sohee", language="Korean")  # warmup
out = {"engine": f"qwen3-tts-{size}-customvoice", "speaker": "Sohee", "load_s": round(load, 2), "rows": []}
torch.cuda.reset_peak_memory_stats()
K = 4  # 4 frames at 12.5 Hz, about 0.32 s audio
for i, text in enumerate(texts, 1):
    steps.clear(); torch.cuda.synchronize(); t0 = time.perf_counter()
    wavs, sr = tts.generate_custom_voice(text=text, speaker="Sohee", language="Korean")
    torch.cuda.synchronize(); total = time.perf_counter() - t0
    codes = captured["codes"]
    torch.cuda.synchronize(); t1 = time.perf_counter()
    orig_dec([{"audio_codes": codes[:K]}]); torch.cuda.synchronize()
    dec_k = time.perf_counter() - t1
    # steps[0] = prefill + first frame; steps[K-1] = Kth frame
    ttfa = (steps[K - 1] - t0) + dec_k if len(steps) >= K else None
    w = wavs[0]; dur = len(w) / sr
    sf.write(str(B / f"samples/qwen3-{size}-sohee-{i}.wav"), w, sr)
    out["rows"].append({"i": i, "audio_s": round(dur, 2), "total_s": round(total, 3), "rtf": round(total / dur, 3),
                        "ttfa_est_s": round(ttfa, 3) if ttfa else None, "frames": int(codes.shape[0]),
                        "step_ms_avg": round(1000 * (steps[-1] - steps[0]) / max(1, len(steps) - 1), 1)})
out["vram_peak_gb"] = round(torch.cuda.max_memory_reserved() / 2**30, 2)
out["rss_mb"] = round(psutil.Process().memory_info().rss / 2**20)
out["sr"] = sr
print(json.dumps(out, ensure_ascii=False))
