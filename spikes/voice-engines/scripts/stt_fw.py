import os, sys, time, json, psutil
from pathlib import Path
os.environ.setdefault("HF_HOME", r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike/cache")
_nv = Path(__file__).resolve().parent.parent / "venv/Lib/site-packages/nvidia"
os.environ["PATH"] = os.pathsep.join(str(_nv / d / "bin") for d in ("cublas", "cudnn", "cuda_nvrtc")) + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel
import soundfile as sf, numpy as np

W = Path(r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike/work")
refs = (W / "sentences.txt").read_text(encoding="utf-8").splitlines()

def cer(ref, hyp):
    norm = lambda s: "".join(c for c in s if c.isalnum())
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j-1] + 1, prev + (r[i-1] != h[j-1]))
            prev = cur
    return d[len(h)] / max(1, len(r))

size, device, ctype = sys.argv[1], sys.argv[2], sys.argv[3]
proc = psutil.Process()
t = time.perf_counter()
m = WhisperModel(size, device=device, compute_type=ctype, download_root=str(Path(os.environ["HF_HOME"]) / "fw"))
load = time.perf_counter() - t
out = {"model": size, "device": device, "compute": ctype, "load_s": round(load, 2), "rows": []}
for i, ref in enumerate(refs, 1):
    wav = W / f"heami-{i}.wav"
    dur = (wav.stat().st_size - 44) / 32000
    t = time.perf_counter()
    audio, sr = sf.read(str(wav), dtype="float32"); assert sr == 16000
    segs, _ = m.transcribe(audio, language="ko", beam_size=5, vad_filter=False)
    hyp = "".join(s.text for s in segs).strip()
    el = time.perf_counter() - t
    out["rows"].append({"i": i, "audio_s": round(dur, 2), "lat_s": round(el, 3), "rtf": round(el / dur, 3), "cer": round(cer(ref, hyp), 3), "hyp": hyp})
out["rss_mb"] = round(proc.memory_info().rss / 2**20)
print(json.dumps(out, ensure_ascii=False))
