"""Voice service with fake devices and engines: no mic, speaker, model or optional package is needed."""
import threading
import time

import numpy as np
import pytest

from jarvis.features.voice.service import (BINS, CAPTURE_BLOCK, SAMPLE_RATE, VoiceError, VoiceService,
                                           frame_features, speakable, split_sentences)


def wait(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    raise AssertionError('condition not reached')


class Stream:
    def __init__(self):
        self.stopped = self.closed = self.aborted = False

    def stop(self):
        self.stopped = True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


class FakeAudio:
    def __init__(self):
        self.inputs, self.outputs = [], []

    def open_input(self, callback, rate, block):
        stream = Stream()
        stream.callback = callback
        self.inputs.append(stream)
        return stream

    def open_output(self, source, rate, block):
        stream = Stream()
        stream.source, stream.rate = source, rate
        self.outputs.append(stream)
        return stream


def drain(stream, block=735):
    """Play an output stream like PortAudio: pull blocks until the source says it is finished."""
    for _ in range(10000):
        stream.source.pull(block)
        if stream.source.finished():
            stream.source.done()
            return
    raise AssertionError('playback never finished')


class FakeTranscriber:
    def __init__(self, text='안녕하세요', fail=False, gate=None):
        self.text, self.fail, self.gate = text, fail, gate
        self.device = self.model_name = None
        self.heard = []

    def load(self):
        if self.fail:
            raise RuntimeError('no model')
        self.device, self.model_name = 'cpu', 'fake'

    def transcribe(self, samples):
        self.heard.append(samples.size)
        if self.gate:
            self.gate.wait(2)
        return self.text


class FakeSynth:
    def __init__(self, name='fake', fail_warm=False):
        self.name, self.fail_warm = name, fail_warm
        self.spoken = []

    def warm_up(self):
        if self.fail_warm:
            raise RuntimeError('engine failed to load')

    def synthesize(self, text):
        self.spoken.append(text)
        return np.full(2205, 0.2, dtype=np.float32), 22050


def service(**changes):
    events = []
    parts = {'transcriber': FakeTranscriber(), 'synthesizer': FakeSynth(), 'audio': FakeAudio(), **changes}
    voice = VoiceService(**parts)
    voice.subscribe(events.append)
    voice.start()
    wait(lambda: voice.state != 'loading')
    return voice, events, parts['audio']


def states(events):
    return [event['state'] for event in events if event['type'] == 'state']


def speech(seconds=0.6):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def feed(stream, samples):
    for start in range(0, samples.size, CAPTURE_BLOCK):
        stream.callback(samples[start:start + CAPTURE_BLOCK])


# --- text and features -----------------------------------------------------------------------------------------

def test_sentences_split_at_end_marks_and_keep_the_rest():
    found, rest = split_sentences('첫 문장입니다. 둘째 문장이에요! 셋째')
    assert found == ['첫 문장입니다.', '둘째 문장이에요!'] and rest == ' 셋째'
    assert split_sentences(' 셋째', final=True) == (['셋째'], '')
    assert split_sentences('...', final=True) == ([], '')          # punctuation alone is not spoken
    long, rest = split_sentences('가' * 100 + ', ' + '나' * 100)
    assert long and len(long[0]) <= 161


def test_speakable_drops_code_links_and_markdown():
    text = speakable('**중요**: `x = 1` 자세한 건 https://example.com/a?b 참고\n```py\nprint(1)\n```')
    assert 'print' not in text and 'x = 1' not in text and 'https' not in text and '*' not in text
    assert '링크' in text and '중요' in text


def test_frame_features_are_bounded_and_place_a_tone_in_its_band():
    silence = frame_features(np.zeros(735, dtype=np.float32), 22050)
    assert silence[0] == 0 and len(silence[1]) == BINS and max(silence[1]) < 0.2
    t = np.arange(735) / 22050
    level, bins = frame_features(0.5 * np.sin(2 * np.pi * 1000 * t), 22050)
    assert 0.5 < level <= 1 and all(0 <= value <= 1 for value in bins)
    expected = round(BINS * np.log(1000 / 190) / np.log(6000 / 190))
    assert abs(int(np.argmax(bins)) - expected) <= 1


# --- listening -------------------------------------------------------------------------------------------------

def test_push_to_talk_transcribes_and_closes_the_mic():
    voice, events, audio = service()
    voice.listen()
    mic = audio.inputs[-1]
    feed(mic, speech())
    assert any(event['type'] == 'frame' for event in events)        # the orb follows the mic
    voice.stop_listening()
    wait(lambda: any(event['type'] == 'transcript' for event in events))
    assert mic.stopped and mic.closed
    assert [e['text'] for e in events if e['type'] == 'transcript'] == ['안녕하세요']
    assert states(events)[-3:] == ['listening', 'transcribing', 'ready']
    assert voice.captured == [] and voice.captured_count == 0      # nothing of the capture is kept


def test_a_tap_without_speech_gives_an_empty_transcript_without_running_stt():
    transcriber = FakeTranscriber()
    voice, events, audio = service(transcriber=transcriber)
    voice.listen()
    feed(audio.inputs[-1], speech(0.1))
    voice.stop_listening()
    assert [e['text'] for e in events if e['type'] == 'transcript'] == [''] and transcriber.heard == []


def test_held_key_stops_by_itself_after_a_pause():
    voice, events, audio = service()
    voice.listen()
    feed(audio.inputs[-1], np.concatenate([speech(0.6), np.zeros(int(1.4 * SAMPLE_RATE), dtype=np.float32)]))
    wait(lambda: any(event['type'] == 'transcript' for event in events))
    assert audio.inputs[-1].closed


def test_cancel_drops_a_transcript_in_progress_but_hush_keeps_it():
    gate = threading.Event()
    voice, events, audio = service(transcriber=FakeTranscriber(gate=gate))
    voice.listen(); feed(audio.inputs[-1], speech()); voice.stop_listening()
    voice.cancel(); gate.set()
    time.sleep(0.1)
    assert not any(event['type'] == 'transcript' for event in events) and voice.state == 'ready'
    gate.clear()
    voice.listen(); feed(audio.inputs[-1], speech()); voice.stop_listening()
    voice.hush(); gate.set()
    wait(lambda: any(event['type'] == 'transcript' for event in events))


def test_listen_before_the_model_loads_or_after_a_failed_load_is_refused():
    voice, events, _ = service(transcriber=FakeTranscriber(fail=True))
    assert voice.state == 'unavailable' and events[-1] == {'type': 'state', 'state': 'unavailable', 'error': 'stt_unavailable'}
    with pytest.raises(VoiceError):
        voice.listen()


# --- speaking --------------------------------------------------------------------------------------------------

def test_streamed_reply_keeps_speaking_between_drafts_and_ends_after_final():
    synth = FakeSynth()
    voice, events, audio = service(synthesizer=synth)
    assert voice.speak('첫 문장입니다. 둘째')
    wait(lambda: audio.outputs)
    speaker = audio.outputs[0]
    for _ in range(20):
        speaker.source.pull(735)
    assert not speaker.source.finished() and voice.state == 'speaking'   # more text may still come
    voice.speak(' 문장이에요.', final=True)
    wait(lambda: len(synth.spoken) == 2)
    drain(speaker)
    assert synth.spoken == ['첫 문장입니다.', '둘째 문장이에요.']
    assert voice.state == 'ready' and states(events)[-2:] == ['speaking', 'ready']
    assert len(audio.outputs) == 1                                         # one stream for the whole reply


def test_listening_while_speaking_stops_playback_and_drops_queued_sentences():
    gate = threading.Event()

    class Slow(FakeSynth):
        def synthesize(self, text):
            if self.spoken:
                gate.wait(2)
            return super().synthesize(text)
    synth = Slow()
    voice, events, audio = service(synthesizer=synth)
    voice.speak('하나입니다. 둘입니다. 셋입니다.', final=True)
    wait(lambda: audio.outputs)
    voice.listen()
    gate.set()
    time.sleep(0.1)
    assert audio.outputs[0].aborted and audio.outputs[0].closed
    assert voice.state == 'listening' and len(audio.outputs) == 1 and len(synth.spoken) <= 2
    assert voice.speak('지금은 말하지 않아요.') is False                    # the user is talking


def test_speak_rejects_oversized_text_and_ends_cleanly_on_empty_final():
    voice, events, _ = service()
    with pytest.raises(VoiceError):
        voice.speak('가' * 301)
    assert voice.speak('', final=True) and voice.state == 'ready'


def test_engine_that_fails_to_warm_up_falls_back_and_errors_stop_speaking():
    fallback = FakeSynth('heami')
    voice, events, audio = service(synthesizer=FakeSynth('cosyvoice', fail_warm=True), fallback=fallback)
    assert voice.state == 'ready' and voice.status()['voice'] == 'heami'

    class Broken(FakeSynth):
        def synthesize(self, text):
            raise RuntimeError('boom')
    voice, events, audio = service(synthesizer=Broken())
    voice.speak('말해 볼게요.', final=True)
    wait(lambda: any(event['type'] == 'error' for event in events))
    assert voice.state == 'ready' and audio.outputs == []


def test_close_releases_the_mic_and_stops_events():
    voice, events, audio = service()
    voice.listen()
    voice.close()
    count = len(events)
    assert audio.inputs[-1].closed
    voice._emit({'type': 'state', 'state': 'ready'})
    assert len(events) == count


# --- numbers and the CosyVoice worker client -------------------------------------------------------------------

def test_numbers_are_read_as_korean_words():
    from jarvis.features.voice.service import korean_numbers
    assert korean_numbers('읽지 않은 메일 12통, 오후 3시') == '읽지 않은 메일 열두 통, 오후 세 시'
    assert korean_numbers('2026년 6월 10월 3개월') == '이천이십육년 유월 시월 삼개월'
    assert korean_numbers('3.5% 1,234원 10000명 20개 1번째') == '삼 점 오 퍼센트 천이백삼십사원 만명 스무 개 첫 번째'
    assert korean_numbers('0 110000 100000000') == '영 십일만 일억'
    assert korean_numbers('숫자 없음') == '숫자 없음'


FAKE_WORKER = r'''
import json, sys
out = sys.stdout.buffer
mode = sys.argv[sys.argv.index('--root') + 1].rsplit('-', 1)[-1]
if mode == 'broken':
    out.write(b'{"ready": false, "error": "tts_unavailable"}\n'); out.flush(); sys.exit(1)
precision = b'"int4"' if mode == 'oddprecision' else b'"fp16"'
out.write(b'{"ready": true, "rate": 24000, "precision": ' + precision + b'}\n'); out.flush()
for line in sys.stdin.buffer:
    text = json.loads(line)['text']
    if mode == 'dies':
        sys.exit(1)
    pcm = (1000).to_bytes(2, 'little', signed=True) * len(text)
    out.write(json.dumps({'ok': True, 'samples': len(text)}).encode() + b'\n' + pcm); out.flush()
'''


def cosyvoice(tmp_path, mode):
    import sys
    from jarvis.features.voice.service import CosyVoiceSynthesizer
    worker = tmp_path / 'fake_worker.py'
    worker.write_text(FAKE_WORKER, encoding='utf-8')
    ref = tmp_path / 'ref.wav'
    ref.write_bytes(b'')
    return CosyVoiceSynthesizer(tmp_path / f'root-{mode}', ref, python=sys.executable, worker=worker)


def test_cosyvoice_client_speaks_through_the_worker_and_stops_it(tmp_path):
    synth = cosyvoice(tmp_path, 'ok')
    synth.warm_up()
    samples, rate = synth.synthesize('12통')                  # sent as 열두 통: four characters
    assert rate == 24000 and samples.size == 4 and abs(samples[0] - 1000 / 32768) < 1e-6
    assert synth.precision == 'fp16'
    process = synth.process
    synth.close()
    assert synth.process is None and process.poll() is not None


def test_an_unknown_precision_from_the_worker_is_not_reported(tmp_path):
    synth = cosyvoice(tmp_path, 'oddprecision')
    synth.warm_up()
    assert synth.precision is None and synth.rate == 24000
    synth.close()


def test_gpu_speech_to_text_uses_int8_weights_with_float16_compute(monkeypatch, tmp_path):
    import sys
    import types
    from jarvis.features.voice import service
    built = []

    class Model:
        def transcribe(self, samples, **options):
            return iter(()), None

    def build(name, device, compute):
        built.append((name, device, compute))
        return Model()
    monkeypatch.setattr(service, '_add_cuda_wheels_to_path', lambda: True)
    monkeypatch.setitem(sys.modules, 'ctranslate2', types.SimpleNamespace(get_cuda_device_count=lambda: 1))
    transcriber = service.WhisperTranscriber(tmp_path)
    monkeypatch.setattr(transcriber, '_build', build)
    transcriber.load()
    assert built == [(service.GPU_MODEL, 'cuda', 'int8_float16')]
    assert (transcriber.device, transcriber.compute) == ('cuda', 'int8_float16')


def test_cosyvoice_that_cannot_load_or_dies_reports_unavailable(tmp_path):
    with pytest.raises(VoiceError) as error:
        cosyvoice(tmp_path, 'broken').warm_up()
    assert error.value.code == 'tts_unavailable'
    synth = cosyvoice(tmp_path, 'dies')
    synth.warm_up()
    with pytest.raises(VoiceError) as error:
        synth.synthesize('안녕하세요.')
    assert error.value.code == 'tts_unavailable' and synth.process is None
    with pytest.raises(VoiceError):
        synth.synthesize('다시요.')


def test_an_engine_that_dies_after_loading_hands_over_to_the_fallback():
    class Dead(FakeSynth):
        closed = False

        def synthesize(self, text):
            raise VoiceError('tts_unavailable')            # e.g. a CosyVoice worker that stalled and was stopped

        def close(self):
            self.closed = True
    dead, fallback = Dead('cosyvoice3'), FakeSynth('heami')
    voice, events, audio = service(synthesizer=dead, fallback=fallback)
    voice.speak('첫 답변입니다.', final=True)
    wait(lambda: audio.outputs)
    drain(audio.outputs[0])
    assert fallback.spoken == ['첫 답변입니다.'] and dead.closed      # the same sentence, in the fallback voice
    assert voice.status()['voice'] == 'heami' and not any(e['type'] == 'error' for e in events)
    voice.speak('다음 답변입니다.', final=True)
    wait(lambda: len(fallback.spoken) == 2)


def test_a_failing_fallback_is_reported_and_not_swapped_again():
    class Dead(FakeSynth):
        def synthesize(self, text):
            raise VoiceError('tts_unavailable')
    voice, events, audio = service(synthesizer=Dead('cosyvoice3'), fallback=Dead('heami'))
    voice.speak('말해 볼게요.', final=True)
    wait(lambda: any(event['type'] == 'error' for event in events))
    assert [e['code'] for e in events if e['type'] == 'error'] == ['tts_unavailable'] and voice.state == 'ready'


# --- the real CosyVoice worker script, with fake torch and cosyvoice packages -------------------------------------

FAKE_TORCH = r'''
import os


class _Cuda:
    @staticmethod
    def is_available():
        return True

    @staticmethod
    def empty_cache():
        from cosyvoice.cli.cosyvoice import half_alive
        with open(os.environ['FAKE_TTS_LOG'], 'a', encoding='utf-8') as log:
            log.write(f'empty_cache half_alive={half_alive()}\n')


cuda = _Cuda()
'''

FAKE_COSYVOICE = r'''
import os
import weakref

import numpy as np

_halves = weakref.WeakSet()


def half_alive():
    return len(_halves)


class Tensor:
    def __init__(self, array):
        self.array = array

    def squeeze(self, dim):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class Module:
    def __init__(self, owner):
        self.owner = owner                  # owner -> inner -> module -> owner: only gc frees it

    def half(self):
        self.owner.is_half = True
        _halves.add(self.owner)


class Inner:
    pass


class AutoModel:
    def __init__(self, model_dir, fp16=False):
        self.sample_rate, self.is_half = 24000, False
        self.model = Inner()
        self.model.llm, self.model.flow = Module(self), Module(self)

    def add_zero_shot_spk(self, *args):
        pass

    def inference_cross_lingual(self, text, ref, zero_shot_spk_id, stream):
        bad = (self.is_half and os.environ.get('FAKE_TTS_MODE') == 'nan_half') or 'NAN' in text
        yield {'tts_speech': Tensor(np.full(240, np.nan if bad else 0.5, dtype=np.float32))}
'''


def run_worker(tmp_path, mode):
    import json
    import os
    import subprocess
    import sys
    from jarvis.features.voice.service import WORKERS_ROOT
    src = tmp_path / 'root' / 'src'
    (src / 'torch').mkdir(parents=True)
    (src / 'torch' / '__init__.py').write_text(FAKE_TORCH, encoding='utf-8')
    (src / 'cosyvoice' / 'cli').mkdir(parents=True)
    (src / 'cosyvoice' / '__init__.py').write_text('', encoding='utf-8')
    (src / 'cosyvoice' / 'cli' / '__init__.py').write_text('', encoding='utf-8')
    (src / 'cosyvoice' / 'cli' / 'cosyvoice.py').write_text(FAKE_COSYVOICE, encoding='utf-8')
    log = tmp_path / 'log.txt'
    env = {**os.environ, 'FAKE_TTS_MODE': mode, 'FAKE_TTS_LOG': str(log)}
    process = subprocess.Popen([sys.executable, '-X', 'utf8', str(WORKERS_ROOT / 'cosyvoice_tts.py'),
                                '--root', str(tmp_path / 'root'), '--ref', str(tmp_path / 'ref.wav')],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)

    def ask(text):
        process.stdin.write(json.dumps({'text': text}).encode() + b'\n')
        process.stdin.flush()
        header = json.loads(process.stdout.readline())
        if header.get('ok'):
            process.stdout.read(header['samples'] * 2)
        return header
    header = json.loads(process.stdout.readline())
    return process, header, ask, log


def test_worker_drops_the_half_model_before_reloading_in_fp32(tmp_path):
    process, header, ask, log = run_worker(tmp_path, 'nan_half')
    try:
        assert header == {'ready': True, 'rate': 24000, 'precision': 'fp32'}
        assert log.read_text(encoding='utf-8') == 'empty_cache half_alive=0\n'
        assert ask('안녕하세요') == {'ok': True, 'samples': 240}
    finally:
        process.stdin.close()
        process.wait(10)


def test_worker_reports_non_finite_sentence_audio_as_a_failure(tmp_path):
    process, header, ask, log = run_worker(tmp_path, 'ok')
    try:
        assert header == {'ready': True, 'rate': 24000, 'precision': 'fp16'}
        assert not log.exists()                         # no fallback, so no cache flush
        assert ask('NAN 문장') == {'ok': False, 'error': 'tts_failed'}
        assert ask('다음 문장') == {'ok': True, 'samples': 240}
    finally:
        process.stdin.close()
        process.wait(10)


# --- idle release (ADR 0001) -----------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ReleasableTranscriber(FakeTranscriber):
    def __init__(self):
        super().__init__()
        self.loaded, self.releases, self.reloads = True, 0, 0

    def release(self):
        self.loaded = False
        self.releases += 1

    def ensure_loaded(self):
        if not self.loaded:
            self.loaded = True
            self.reloads += 1


class Engine(FakeSynth):
    """A main voice that can be ended and started again; later warm-ups wait for `gate` and may fail."""
    def __init__(self, fail_reload=False):
        super().__init__('cosyvoice3')
        self.warm_ups = self.closes = 0
        self.gate, self.fail_reload = threading.Event(), fail_reload

    def warm_up(self):
        self.warm_ups += 1
        if self.warm_ups > 1:
            self.gate.wait(3)
            if self.fail_reload:
                raise RuntimeError('reload failed')

    def close(self):
        self.closes += 1


def idle_service(**changes):
    from jarvis.features.voice import service as module
    clock = Clock()
    parts = {'transcriber': ReleasableTranscriber(), 'synthesizer': Engine(), 'fallback': FakeSynth('heami'),
             'clock': clock, **changes}
    voice, events, audio = service(**parts)
    return voice, events, audio, clock, module


def finish_reply(voice, audio, text):
    assert voice.speak(text, final=True)
    wait(lambda: audio.outputs)
    drain(audio.outputs.pop())
    wait(lambda: voice.state == 'ready')


def test_idle_stt_is_released_after_five_minutes_and_reloads_when_the_mic_opens():
    voice, events, audio, clock, module = idle_service()
    stt = voice.transcriber
    clock.now += module.STT_IDLE_SECONDS - 1
    voice.release_idle()
    assert stt.releases == 0 and voice.status()['stt_loaded'] is True
    clock.now += 1
    voice.release_idle()
    assert stt.releases == 1 and voice.status()['stt_loaded'] is False
    voice.listen()                                          # the reload starts with the mic, not after it
    wait(lambda: stt.reloads == 1)
    assert voice.synthesizer.closes == 0                    # the voice engine has a longer idle time
    voice.close()


def test_idle_tts_worker_ends_and_heami_covers_until_it_is_back():
    voice, events, audio, clock, module = idle_service()
    engine, heami = voice.synthesizer, voice.fallback
    clock.now += module.TTS_IDLE_SECONDS
    voice.release_idle()
    assert engine.closes == 1 and voice.status()['tts_awake'] is False
    finish_reply(voice, audio, '첫 문장입니다.')             # Heami speaks while the engine reloads
    assert heami.spoken == ['첫 문장입니다.'] and engine.spoken == []
    wait(lambda: engine.warm_ups == 2)
    engine.gate.set()
    wait(lambda: voice.status()['tts_awake'])
    finish_reply(voice, audio, '다음 문장입니다.')
    assert engine.spoken == ['다음 문장입니다.'] and voice.synthesizer is engine
    voice.close()


def test_the_mic_wakes_a_released_voice_engine_before_the_reply():
    voice, events, audio, clock, module = idle_service()
    engine = voice.synthesizer
    engine.gate.set()
    clock.now += module.TTS_IDLE_SECONDS
    voice.release_idle()
    voice.listen()
    wait(lambda: voice.status()['tts_awake'])
    assert engine.warm_ups == 2
    voice.close()


def test_nothing_is_released_while_listening_or_speaking():
    voice, events, audio, clock, module = idle_service()
    voice.listen()
    clock.now += module.TTS_IDLE_SECONDS
    voice.release_idle()
    voice.cancel()
    assert voice.transcriber.releases == 0 and voice.synthesizer.closes == 0
    assert voice.speak('말하는 중입니다.', final=True)
    wait(lambda: audio.outputs)
    clock.now += module.TTS_IDLE_SECONDS
    voice.release_idle()
    assert voice.transcriber.releases == 0 and voice.synthesizer.closes == 0
    voice.close()


def test_a_voice_engine_that_fails_to_reload_hands_over_to_the_fallback():
    voice, events, audio, clock, module = idle_service(synthesizer=Engine(fail_reload=True))
    engine, heami = voice.synthesizer, voice.fallback
    engine.gate.set()
    clock.now += module.TTS_IDLE_SECONDS
    voice.release_idle()
    voice.listen()
    wait(lambda: voice.synthesizer is heami)
    assert engine.closes == 2 and voice.status()['voice'] == 'heami'
    assert voice.status()['tts_wake_failed'] is True
    voice.close()


class WorkerEngine(FakeSynth):
    """Like CosyVoiceSynthesizer: warm_up starts a worker, close ends it, synthesize needs a running worker."""
    def __init__(self):
        super().__init__('cosyvoice3')
        self.process = None

    def warm_up(self):
        self.process = object()

    def close(self):
        self.process = None

    def synthesize(self, text):
        if self.process is None:
            raise VoiceError('tts_unavailable')
        return super().synthesize(text)


class SlowRelease(ReleasableTranscriber):
    """An STT unload that takes a moment, as unload_model does on CUDA."""
    def __init__(self):
        super().__init__()
        self.entered, self.go = threading.Event(), threading.Event()

    def release(self):
        self.entered.set()
        self.go.wait(3)
        super().release()


def test_a_wake_during_release_keeps_the_new_worker():
    # Both models are released on the same tick (for example the first tick after the PC wakes from sleep) and the
    # mic opens while the STT unload is still running. The wake must not lose its worker to the release.
    voice, events, audio, clock, module = idle_service(transcriber=SlowRelease(), synthesizer=WorkerEngine())
    stt, engine, heami = voice.transcriber, voice.synthesizer, voice.fallback
    clock.now += module.TTS_IDLE_SECONDS
    releasing = threading.Thread(target=voice.release_idle)
    releasing.start()
    assert stt.entered.wait(3)
    voice.listen()
    wait(lambda: voice.status()['tts_awake'])
    stt.go.set()
    releasing.join(3)
    voice.cancel()
    assert voice.status()['tts_awake'] is True and engine.process is not None
    finish_reply(voice, audio, '안녕하세요.')
    assert voice.synthesizer is engine and engine.spoken == ['안녕하세요.'] and heami.spoken == []
    voice.close()


def test_without_a_fallback_the_voice_engine_is_never_released():
    voice, events, audio, clock, module = idle_service(fallback=None)
    clock.now += module.TTS_IDLE_SECONDS
    voice.release_idle()
    assert voice.synthesizer.closes == 0 and voice.status()['tts_awake'] is True
    assert voice.transcriber.releases == 1
    voice.close()


def test_whisper_release_is_a_full_unload_on_cuda_and_transcribe_reloads(tmp_path):
    from jarvis.features.voice.service import WhisperTranscriber

    class Inner:
        def __init__(self):
            self.model_is_loaded, self.calls = True, []

        def unload_model(self, to_cpu=False):
            self.calls.append(('unload', to_cpu))
            self.model_is_loaded = False

        def load_model(self):
            self.calls.append(('load',))
            self.model_is_loaded = True

    class Model:
        def __init__(self):
            self.model = Inner()

        def transcribe(self, samples, **options):
            assert self.model.model_is_loaded
            return iter(()), None

    transcriber = WhisperTranscriber(tmp_path)
    transcriber.model, transcriber.device = Model(), 'cpu'
    assert transcriber.release() is False and transcriber.loaded
    transcriber.device = 'cuda'
    assert transcriber.release() is True and not transcriber.loaded
    assert transcriber.release() is False
    assert transcriber.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32)) == ''
    assert transcriber.model.model.calls == [('unload', False), ('load',)]
