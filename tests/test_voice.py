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
out.write(b'{"ready": true, "rate": 24000}\n'); out.flush()
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
    process = synth.process
    synth.close()
    assert synth.process is None and process.poll() is not None


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
