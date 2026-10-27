import json, time
from pathlib import Path
import numpy as np, soundfile as sf
from openwakeword.model import Model
B = Path(r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike")
W = B / "work/wake"
FR = 1280

def to16k(a, sr):
    if a.ndim > 1: a = a.mean(1)
    if sr != 16000:
        x = np.arange(0, len(a), sr / 16000); a = np.interp(x, np.arange(len(a)), a)
    return (np.clip(a, -1, 1) * 32767).astype(np.int16)

def scores(m, a):
    m.reset(); pad = np.zeros(16000, np.int16); a = np.concatenate([pad, a, pad]); out = []
    for k in range(0, len(a) - FR + 1, FR):
        out.append(float(next(iter(m.predict(a[k:k + FR]).values()))))
    return np.array(out)

texts = dict(l.split("\t")[1:] for l in (W / "clips.tsv").read_text(encoding="utf-8").splitlines())
clips = {k: to16k(*sf.read(str(W / f"{k}.wav"), dtype="float32")) for k in texts}
for f in sorted((B / "samples").glob("qwen3-*-sohee-*.wav")):
    clips[f.stem] = to16k(*sf.read(str(f), dtype="float32")); texts[f.stem] = "(Korean sentence, Qwen Sohee voice)"
for f in sorted((B / "samples").glob("heami-*.wav")):
    clips[f.stem] = to16k(*sf.read(str(f), dtype="float32")); texts[f.stem] = "(Korean sentence, Heami voice)"
res = {}
for label, kw in [("plain", {}), ("vad0.5", {"vad_threshold": 0.5})]:
    m = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx", **kw)
    rows = []
    t = time.perf_counter(); n = 0
    for k, a in clips.items():
        s = scores(m, a); n += len(s)
        rows.append({"clip": k, "text": texts[k], "max": round(float(s.max()), 3), "frames_ge_0.5": int((s >= 0.5).sum())})
    res[label] = {"ms_per_80ms_frame": round(1000 * (time.perf_counter() - t) / n, 2), "rows": rows}
korean = np.concatenate([a for k, a in clips.items() if k.startswith(("qwen3", "heami-")) or k in ("heami-06", "heami-07")])
res["korean_stream_s"] = round(len(korean) / 16000, 1)
print(json.dumps(res, ensure_ascii=False))
