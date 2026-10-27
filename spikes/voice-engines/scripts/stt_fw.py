import os, sys, time, json, psutil
from pathlib import Path
os.environ.setdefault("HF_HOME", r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike/cache")
_nv = Path(__file__).resolve().parent.parent / "venv/Lib/site-packages/nvidia"
os.environ["PATH"] = os.pathsep.join(str(_nv / d / "bin") for d in ("cublas", "cudnn", "cuda_nvrtc")) + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel
import soundfile as sf, numpy as np

W = Path(r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike/work")
refs = (W / "sentences.txt").read_text(encoding="utf-8").splitlines()

NUMS = [("십오", "15"), ("세 시", "3시"), ("세시", "3시"), ("두 건", "2건"), ("두건", "2건"), ("한 건", "1건"), ("한건", "1건"),
        ("두 권", "2권"), ("한 권", "1권"), ("팔십오", "85"), ("퍼센트", "%")]
def numnorm(s):
    for a, b in sorted(NUMS, key=lambda x: -len(x[0])): s = s.replace(a, b)  # longest first: 팔십오 before 십오
    return s.replace("시 15분", "시15분")
def cer(ref, hyp, nn=False):
    if nn: ref, hyp = numnorm(ref), numnorm(hyp)
    norm = lambda s: "".join(c for c in s if c.isalnum() or c == "%")
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
srcname = sys.argv[4] if len(sys.argv) > 4 else "heami"
import subprocess
def gpu_used():
    return int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.split()[0])
gpu0 = gpu_used()
proc = psutil.Process()
t = time.perf_counter()
m = WhisperModel(size, device=device, compute_type=ctype, download_root=str(Path(os.environ["HF_HOME"]) / "fw"))
load = time.perf_counter() - t
out = {"input": srcname, "model": size, "device": device, "compute": ctype, "load_s": round(load, 2), "rows": []}
for i, ref in enumerate(refs, 1):
    wav = (W / f"heami-{i}.wav") if srcname == "heami" else (W.parent / f"samples/{srcname}-{i}.wav")
    t = time.perf_counter()
    audio, sr = sf.read(str(wav), dtype="float32")
    if sr != 16000:
        audio = np.interp(np.arange(0, len(audio), sr / 16000), np.arange(len(audio)), audio).astype(np.float32)
    dur = len(audio) / 16000
    segs, _ = m.transcribe(audio, language="ko", beam_size=5, vad_filter=False)
    hyp = "".join(s.text for s in segs).strip()
    el = time.perf_counter() - t
    out["rows"].append({"i": i, "audio_s": round(dur, 2), "lat_s": round(el, 3), "rtf": round(el / dur, 3), "cer": round(cer(ref, hyp), 3), "cer_num": round(cer(ref, hyp, True), 3), "hyp": hyp})
out["vram_delta_mb"] = gpu_used() - gpu0 if device == "cuda" else None
out["rss_mb"] = round(proc.memory_info().rss / 2**20)
print(json.dumps(out, ensure_ascii=False))
