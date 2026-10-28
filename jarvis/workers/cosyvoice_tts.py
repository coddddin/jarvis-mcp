"""CosyVoice 3 speech worker. Runs in JARVIS's own CosyVoice copy (Python 3.10), never in the project venv.

Started by CosyVoiceSynthesizer in service.py as
    <root>/venv/Scripts/python.exe -I -X utf8 cosyvoice_tts.py --root <root> --ref <voice.wav>
It imports nothing from JARVIS. Protocol over stdin/stdout:
    worker -> {"ready": true, "rate": 24000, "precision": "fp16"|"fp32"}   after loading and one warm-up sentence
    client -> {"text": "..."}                               one sentence per line, UTF-8 JSON
    worker -> {"ok": true, "samples": N} + N int16 LE mono samples, or {"ok": false, "error": "<code>"}
EOF on stdin ends the worker, so it never outlives the HUD. Text and audio are never logged or written to disk;
stdout carries only the protocol (stray prints from libraries go to stderr, which the client discards).

On CUDA the llm and flow modules are cast to half after loading (about 2.0 GB instead of 3.6 GB on the device, and
slightly faster); the hift vocoder stays fp32. CosyVoice's own fp16 flag only turns on autocast and keeps fp32
weights. If the half-precision warm-up fails or produces non-finite or silent audio, the model is reloaded in fp32.
A sentence whose audio is not finite is answered with tts_failed rather than played as silence.
"""
import argparse
import gc
import json
import logging
import os
import sys

MODEL = 'Fun-CosyVoice3-0.5B'
PROMPT = 'You are a helpful assistant.<|endofprompt|>'
SPEAKER = 'jarvis'
MAX_TEXT = 400
WARM_UP = '안녕하세요.'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--ref', required=True)
    args = parser.parse_args()

    out = os.fdopen(os.dup(sys.stdout.fileno()), 'wb')     # the protocol channel
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())      # anything printed by libraries goes to stderr
    logging.disable(logging.WARNING)                       # CosyVoice logs every sentence it reads at INFO

    def send(header, payload=b''):
        out.write(json.dumps(header).encode() + b'\n' + payload)
        out.flush()

    src = os.path.join(args.root, 'src')
    sys.path[:0] = [os.path.join(src, 'third_party', 'Matcha-TTS'), src]
    try:
        import numpy as np
        import torch
        from cosyvoice.cli.cosyvoice import AutoModel

        def load(half):
            loaded = AutoModel(model_dir=os.path.join(args.root, 'models', MODEL), fp16=half)
            if half:
                loaded.model.llm.half()
                loaded.model.flow.half()                   # hift (the vocoder) stays fp32
            loaded.add_zero_shot_spk('', args.ref, SPEAKER)  # the reference voice is analysed once
            return loaded

        def speech(loaded, text):
            parts = [item['tts_speech'] for item in
                     loaded.inference_cross_lingual(PROMPT + text, args.ref, zero_shot_spk_id=SPEAKER, stream=False)]
            return (np.concatenate([part.squeeze(0).float().cpu().numpy() for part in parts]) if parts
                    else np.zeros(0, dtype=np.float32))

        def sound(samples):
            return samples.size > 0 and bool(np.isfinite(samples).all()) and float(np.abs(samples).max()) > 0.01

        model, precision = None, 'fp32'
        if torch.cuda.is_available():
            try:
                model = load(True)
                if sound(speech(model, WARM_UP)):          # the first call compiles kernels and is slow
                    precision = 'fp16'
                else:
                    print('fp16 warm-up was not usable; reloading in fp32', file=sys.stderr)
                    model = None
            except Exception as exc:                       # noqa: BLE001 - fall back to the full-precision model
                print(f'fp16 load failed: {type(exc).__name__}; reloading in fp32', file=sys.stderr)
                model = None
            if model is None:
                gc.collect()                               # a cycle can keep the half model alive otherwise
                torch.cuda.empty_cache()
        if model is None:
            model = load(False)
            speech(model, WARM_UP)

        def synthesize(text):
            samples = speech(model, text)
            if not np.isfinite(samples).all():
                raise ValueError('non-finite audio')       # reported as tts_failed, never played as silence
            return (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
    except Exception as exc:                               # noqa: BLE001 - reported as a code, details on stderr
        print(f'load failed: {type(exc).__name__}', file=sys.stderr)
        send({'ready': False, 'error': 'tts_unavailable'})
        return 1
    send({'ready': True, 'rate': model.sample_rate, 'precision': precision})

    for line in sys.stdin.buffer:
        try:
            text = json.loads(line)['text']
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise ValueError
            text = text.replace('<|', ' ').replace('|>', ' ')  # no control tokens from reply text
        except (ValueError, KeyError, TypeError):
            send({'ok': False, 'error': 'bad_text'})
            continue
        try:
            pcm = synthesize(text)
        except Exception as exc:                           # noqa: BLE001
            print(f'synthesis failed: {type(exc).__name__}', file=sys.stderr)
            send({'ok': False, 'error': 'tts_failed'})
            continue
        send({'ok': True, 'samples': len(pcm) // 2}, pcm)
    return 0


if __name__ == '__main__':
    sys.exit(main())
