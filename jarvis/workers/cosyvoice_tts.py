"""CosyVoice 3 speech worker. Runs in JARVIS's own CosyVoice copy (Python 3.10), never in the project venv.

Started by CosyVoiceSynthesizer in service.py as
    <root>/venv/Scripts/python.exe -I -X utf8 cosyvoice_tts.py --root <root> --ref <voice.wav>
It imports nothing from JARVIS. Protocol over stdin/stdout:
    worker -> {"ready": true, "rate": 24000}                after the model loaded and one warm-up sentence
    client -> {"text": "..."}                               one sentence per line, UTF-8 JSON
    worker -> {"ok": true, "samples": N} + N int16 LE mono samples, or {"ok": false, "error": "<code>"}
EOF on stdin ends the worker, so it never outlives the HUD. Text and audio are never logged or written to disk;
stdout carries only the protocol (stray prints from libraries go to stderr, which the client discards).
"""
import argparse
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
        from cosyvoice.cli.cosyvoice import AutoModel
        model = AutoModel(model_dir=os.path.join(args.root, 'models', MODEL))
        model.add_zero_shot_spk('', args.ref, SPEAKER)     # the reference voice is analysed once

        def synthesize(text):
            parts = [item['tts_speech'] for item in
                     model.inference_cross_lingual(PROMPT + text, args.ref, zero_shot_spk_id=SPEAKER, stream=False)]
            speech = np.concatenate([part.squeeze(0).cpu().numpy() for part in parts]) if parts else np.zeros(0)
            return (np.clip(speech, -1, 1) * 32767).astype('<i2').tobytes()

        synthesize(WARM_UP)                                # the first call compiles kernels and is slow
    except Exception as exc:                               # noqa: BLE001 - reported as a code, details on stderr
        print(f'load failed: {type(exc).__name__}', file=sys.stderr)
        send({'ready': False, 'error': 'tts_unavailable'})
        return 1
    send({'ready': True, 'rate': model.sample_rate})

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
