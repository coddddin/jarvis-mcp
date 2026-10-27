import sys, time, json, psutil
from pathlib import Path
import numpy as np, soundfile as sf, sherpa_onnx
sys.path.insert(0, str(Path(__file__).parent))
B = Path(r"C:/Users/heejun/AppData/Local/JarvisVoiceSpike")
M = B / "cache/zipformer-ko/sherpa-onnx-streaming-zipformer-korean-2024-06-16"
W = B / "work"
refs = (W / "sentences.txt").read_text(encoding="utf-8").splitlines()

NUMS = [("십오", "15"), ("세 시", "3시"), ("세시", "3시"), ("두 건", "2건"), ("두건", "2건"), ("한 건", "1건"), ("한건", "1건"),
        ("두 권", "2권"), ("한 권", "1권"), ("팔십오", "85"), ("퍼센트", "%")]
def numnorm(s):
    for a, b in NUMS: s = s.replace(a, b)
    return s.replace("시 15분", "시15분")
def cer(ref, hyp, nn=False):
    if nn: ref, hyp = numnorm(ref), numnorm(hyp)
    norm = lambda s: "".join(c for c in s if c.isalnum() or c == "%")
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]; d[j] = min(d[j] + 1, d[j-1] + 1, prev + (r[i-1] != h[j-1])); prev = cur
    return d[len(h)] / max(1, len(r))

q = sys.argv[1]  # int8 or fp32
sfx = ".int8.onnx" if q == "int8" else ".onnx"
t = time.perf_counter()
rec = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=str(M / "tokens.txt"), encoder=str(M / f"encoder-epoch-99-avg-1{sfx}"),
    decoder=str(M / f"decoder-epoch-99-avg-1{sfx}"), joiner=str(M / f"joiner-epoch-99-avg-1{sfx}"),
    num_threads=4, sample_rate=16000, feature_dim=80, decoding_method="greedy_search")
out = {"model": f"zipformer-ko-streaming-{q}", "device": "cpu", "load_s": round(time.perf_counter() - t, 2), "rows": []}
chunk = 1600  # 100 ms
for i, ref in enumerate(refs, 1):
    a, sr = sf.read(str(W / f"heami-{i}.wav"), dtype="float32")
    s = rec.create_stream(); compute = 0.0; first_partial = None
    for k in range(0, len(a), chunk):
        s.accept_waveform(sr, a[k:k + chunk])
        t = time.perf_counter()
        while rec.is_ready(s): rec.decode_stream(s)
        compute += time.perf_counter() - t
        if first_partial is None and rec.get_result(s).strip():
            first_partial = round((k + chunk) / sr, 2)
    t = time.perf_counter()
    s.accept_waveform(sr, np.zeros(int(0.3 * sr), dtype="float32")); s.input_finished()
    while rec.is_ready(s): rec.decode_stream(s)
    final_lat = time.perf_counter() - t
    hyp = rec.get_result(s).strip(); dur = len(a) / sr
    out["rows"].append({"i": i, "audio_s": round(dur, 2), "first_partial_at_audio_s": first_partial,
        "lat_s": round(final_lat, 3), "rtf": round((compute + final_lat) / dur, 3), "cer": round(cer(ref, hyp), 3), "cer_num": round(cer(ref, hyp, True), 3), "hyp": hyp})
out["rss_mb"] = round(psutil.Process().memory_info().rss / 2**20)
print(json.dumps(out, ensure_ascii=False))
