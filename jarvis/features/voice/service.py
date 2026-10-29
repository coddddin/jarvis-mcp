"""Local voice I/O for the assistant view: push-to-talk capture, speech to text, and replies spoken sentence by
sentence, with level/spectrum frames for the orb.

The page decides what is said: it starts and stops listening, sends the transcript to the chat API itself, and
hands reply text back here to be spoken. This module only moves audio.

- Audio stays in memory. The mic stream is open only while listening and is closed on stop, cancel, error, close
  or client disconnect; captured samples are dropped once transcribed. Nothing is written to disk or logged,
  including transcripts.
- Speech to text: faster-whisper. GPU (large-v3-turbo, int8 weights with float16 compute) when CUDA works, otherwise CPU (small, int8).
  Models live in one fixed folder; after the first download they load with local_files_only, so an offline
  restart never downloads again.
- Text to speech: a Synthesizer. CosyVoice 3 in its own Python 3.10 process (jarvis/workers/cosyvoice_tts.py) when JARVIS's copy
  exists (scripts/voice/link_cosyvoice.py); the Windows voice Heami only if it is missing or fails to load.
- Barge-in: listening while speaking stops playback at once and drops queued sentences.

Optional dependencies (requirements-voice.txt) are imported only when a real backend is built, so the HUD and
the tests run without them.
"""
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
from typing import Callable, Protocol

import numpy as np

from jarvis.core.paths import WORKERS_ROOT

SAMPLE_RATE = 16000
CAPTURE_BLOCK = 480                # 30 ms
MAX_LISTEN_SECONDS = 30            # capture stops by itself after this
AUTO_STOP_SILENCE = 1.2            # seconds of silence after speech that end a held push-to-talk
MIN_SPEECH_SECONDS = 0.3           # shorter captures are not transcribed
FRAME_HZ = 30                      # level/spectrum frames per second while listening or speaking
BINS = 32
MAX_TEXT = 300                     # characters per speak() call
MAX_PENDING_TEXT = 4000            # characters waiting to be spoken
SENTENCE_LIMIT = 160               # a run-on sentence is cut at a comma or space after this many characters

GPU_MODEL, CPU_MODEL = 'large-v3-turbo', 'small'
# Idle release (ADR 0001). Measured on this PC: STT reloads from disk in about 1-2 s, CosyVoice respawns in about
# 7 s plus warm-up. Both wake when the mic opens, so the wait hides behind the user's speaking time.
STT_IDLE_SECONDS = 5 * 60          # the GPU speech-to-text model is fully unloaded after this long without use
TTS_IDLE_SECONDS = 30 * 60         # the CosyVoice worker ends after this long without use (Heami covers the reload)
IDLE_CHECK_SECONDS = 15
DEFAULT_MODEL_ROOT = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'Jarvis' / 'voice-models'

STATES = ('loading', 'ready', 'listening', 'transcribing', 'speaking', 'unavailable')


class VoiceError(Exception):
    """A user-facing failure with a short code; messages never carry audio or transcripts."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- signal features -------------------------------------------------------------------------------------------

def _bin_edges(rate, size):
    """FFT bin index ranges for BINS log-spaced bands from about 190 Hz to 6 kHz, like the browser analyser."""
    freqs = [190 * (6000 / 190) ** (i / BINS) for i in range(BINS + 1)]
    index = [min(size // 2, max(1, round(f * size / rate))) for f in freqs]
    return [(index[i], max(index[i] + 1, index[i + 1])) for i in range(BINS)]


def frame_features(block, rate):
    """Level 0..1 and BINS spectrum values 0..1 for one block of float samples."""
    block = np.asarray(block, dtype=np.float32).reshape(-1)
    if block.size < 16:
        return 0.0, [0.0] * BINS
    rms = float(np.sqrt(np.mean(block * block)))
    level = min(1.0, max(0.0, (20 * math.log10(rms + 1e-9) + 60) / 50))       # -60..-10 dBFS
    size = 1 << (block.size - 1).bit_length()
    spectrum = np.abs(np.fft.rfft(block * np.hanning(block.size), n=size)) * 2 / block.size
    db = 20 * np.log10(spectrum + 1e-12)
    bins = []
    for start, end in _bin_edges(rate, size):
        value = (float(db[start:end].max()) + 100) / 70                         # -100..-30 dB, AnalyserNode style
        bins.append(round(min(1.0, max(0.0, value)), 3))
    return round(level, 3), bins


# --- text to speech --------------------------------------------------------------------------------------------

_MARKUP = re.compile(r'```.*?```|`[^`]*`', re.S)
_URL = re.compile(r'https?://\S+')
_SENTENCE = re.compile(r'.+?(?:[.!?…。]+(?=\s|$)|\n+)', re.S)


def speakable(text):
    """Reply text as it should be read: no code blocks, links read as one word, no markdown marks."""
    text = _MARKUP.sub(' ', text)
    text = _URL.sub('링크', text)
    text = re.sub(r'[*_#>|~]+', ' ', text)
    return re.sub(r'[ \t]+', ' ', text)


def split_sentences(buffer, final=False):
    """Complete sentences from the front of buffer, and the rest. final=True also returns the rest."""
    found, position = [], 0
    for match in _SENTENCE.finditer(buffer):
        found.append(match.group().strip())
        position = match.end()
    rest = buffer[position:]
    while len(rest) > SENTENCE_LIMIT:                       # a long run without an end mark: cut at a pause
        cut = max(rest.rfind(',', 0, SENTENCE_LIMIT), rest.rfind(' ', 0, SENTENCE_LIMIT))
        cut = cut + 1 if cut > 0 else SENTENCE_LIMIT
        found.append(rest[:cut].strip())
        rest = rest[cut:]
    if final:
        found.append(rest.strip())
        rest = ''
    return [item for item in found if re.search(r'\w', item)], rest


class Synthesizer(Protocol):
    """One sentence in, mono float32 samples and their rate out. Called from one worker thread."""
    name: str

    def synthesize(self, text: str) -> tuple[np.ndarray, int]: ...


class SapiSynthesizer:
    """A Windows SAPI 5 voice rendered to memory. Heami (ko-KR) by default; text is never read as SAPI XML."""
    RATE = 22050

    def __init__(self, voice='Heami'):
        self.voice_hint = voice
        self.name = 'windows-sapi'
        self._voice = self._format = self._lib = None

    def _open(self):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()                             # SAPI is COM: initialise on the worker thread
        voice = comtypes.client.CreateObject('SAPI.SpVoice')
        from comtypes.gen import SpeechLib
        voices = voice.GetVoices()
        chosen = next((voices.Item(i) for i in range(voices.Count)
                       if self.voice_hint.lower() in voices.Item(i).GetDescription().lower()), None)
        if chosen is None:
            raise VoiceError('voice_not_installed')
        voice.Voice = chosen
        self.name = 'windows-sapi:' + chosen.GetDescription()
        audio_format = comtypes.client.CreateObject('SAPI.SpAudioFormat')
        audio_format.Type = SpeechLib.SAFT22kHz16BitMono
        self._voice, self._format, self._lib = voice, audio_format, SpeechLib

    def synthesize(self, text):
        import comtypes.client
        if self._voice is None:
            self._open()
        stream = comtypes.client.CreateObject('SAPI.SpMemoryStream')
        stream.Format = self._format
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text, self._lib.SVSFIsNotXML | self._lib.SVSFPurgeBeforeSpeak)
        samples = np.frombuffer(bytes(stream.GetData()), dtype=np.int16).astype(np.float32) / 32768
        return samples, self.RATE


# Numbers read aloud. CosyVoice has no Korean text frontend, so "12통" must arrive as "열두 통".
_SINO = '영일이삼사오육칠팔구'
_NATIVE_TENS = ['', '열', '스물', '서른', '마흔', '쉰', '예순', '일흔', '여든', '아흔']
_NATIVE_ONES = ['', '한', '두', '세', '네', '다섯', '여섯', '일곱', '여덟', '아홉']
_NATIVE_COUNTERS = ('시간', '마리', '가지', '군데', '사람', '번째', '개', '명', '통', '시', '살', '잔', '권', '장', '달',
                    '병', '켤레', '그릇')
_NUMBER = re.compile(r'(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s?)(%|개월|' + '|'.join(_NATIVE_COUNTERS) + r'|월)?')


def _sino(number):
    """Sino-Korean reading: 2026 -> 이천이십육, 10000 -> 만. Very long numbers are read digit by digit."""
    if number == 0:
        return '영'
    if number >= 10 ** 16:
        return ''.join(_SINO[int(d)] for d in str(number))
    words = []
    for unit, size in (('조', 10 ** 12), ('억', 10 ** 8), ('만', 10 ** 4), ('', 1)):
        group, number = divmod(number, size)
        if not group:
            continue
        part = ''
        for name, place in (('천', 1000), ('백', 100), ('십', 10), ('', 1)):
            digit, group = divmod(group, place)
            if digit:
                part += ('' if digit == 1 and name else _SINO[digit]) + name
        words.append('' if part == '일' and unit == '만' and not words else part)   # 10000 is 만, not 일만
        words[-1] += unit
    return ''.join(words)


def _native(number):
    """Native Korean counting form before a counter, 1..99: 12 -> 열두, 20 -> 스무."""
    if number == 20:
        return '스무'
    tens, ones = divmod(number, 10)
    return _NATIVE_TENS[tens] + _NATIVE_ONES[ones]


def korean_numbers(text):
    """Digits as Korean words: native numbers before counters (12통 -> 열두 통, 3시 -> 세 시), Sino-Korean
    otherwise (2026년 -> 이천이십육년, 3.5% -> 삼 점 오 퍼센트, 6월 -> 유월)."""
    def read(match):
        whole, fraction, space, suffix = match.group(1), match.group(2), match.group(3), match.group(4) or ''
        number = int(whole.replace(',', ''))
        if suffix in _NATIVE_COUNTERS and not fraction and 1 <= number <= 99:
            return '첫 번째' if (number, suffix) == (1, '번째') else f'{_native(number)} {suffix}'
        if suffix == '월' and not fraction:
            return {6: '유월', 10: '시월'}.get(number, _sino(number) + '월')
        words = _sino(number)
        if fraction:
            words += ' 점 ' + ' '.join(_SINO[int(d)] for d in fraction[1:])
        if suffix == '%':
            return words + ' 퍼센트'
        return words + space + suffix
    return _NUMBER.sub(read, text)


DEFAULT_COSYVOICE_ROOT = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'Jarvis' / 'voice' / 'cosyvoice'
DEFAULT_VOICE_REF = DEFAULT_COSYVOICE_ROOT.parent / 'jarvis-ref-en.wav'
COSYVOICE_LOAD_SECONDS = 240                               # model load plus one warm-up sentence
COSYVOICE_SENTENCE_SECONDS = 60


class CosyVoiceSynthesizer:
    """CosyVoice 3 in a separate Python 3.10 process (jarvis/workers/cosyvoice_tts.py) that JARVIS owns.

    The worker is started by warm_up() and ends when its stdin closes (close() or this process exiting). One
    sentence is in flight at a time. A worker that dies or stalls is stopped and reported as tts_unavailable."""

    def __init__(self, root=DEFAULT_COSYVOICE_ROOT, ref=DEFAULT_VOICE_REF, *, python=None, worker=None):
        self.root, self.ref = Path(root), Path(ref)
        self.python = Path(python) if python else self.root / 'venv' / 'Scripts' / 'python.exe'
        self.worker = Path(worker) if worker else WORKERS_ROOT / 'cosyvoice_tts.py'
        self.name = 'cosyvoice3'
        self.process = self.rate = self.precision = None  # precision: 'fp16' or 'fp32', from the worker
        self.lock = threading.Lock()

    def available(self):
        return self.python.is_file() and self.ref.is_file()

    def _read(self, size, timeout):
        """Exactly size bytes from the worker, or None on EOF or timeout (the worker is then stopped)."""
        result = {}

        def reader():
            data = b''
            while len(data) < size:
                chunk = self.process.stdout.read(size - len(data))
                if not chunk:
                    break
                data += chunk
            result['data'] = data
        thread = threading.Thread(target=reader, daemon=True, name='cosyvoice-read')
        thread.start()
        thread.join(timeout)
        data = result.get('data')
        if data is None or len(data) < size:
            self._stop()
            return None
        return data

    def _header(self, timeout):
        result = {}

        def reader():
            result['line'] = self.process.stdout.readline()
        thread = threading.Thread(target=reader, daemon=True, name='cosyvoice-read')
        thread.start()
        thread.join(timeout)
        line = result.get('line')
        try:
            header = json.loads(line) if line else None
        except ValueError:
            header = None
        if not isinstance(header, dict):
            self._stop()
            raise VoiceError('tts_unavailable')
        return header

    def warm_up(self):
        import subprocess
        if not self.available():
            raise VoiceError('tts_unavailable')
        with self.lock:
            self.process = subprocess.Popen(
                [str(self.python), '-I', '-X', 'utf8', str(self.worker),
                 '--root', str(self.root), '--ref', str(self.ref)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            header = self._header(COSYVOICE_LOAD_SECONDS)
            if header.get('ready') is not True or not isinstance(header.get('rate'), int):
                self._stop()
                raise VoiceError('tts_unavailable')
            self.rate = header['rate']
            self.precision = header.get('precision') if header.get('precision') in ('fp16', 'fp32') else None

    def synthesize(self, text):
        text = korean_numbers(text)
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise VoiceError('tts_unavailable')
            try:
                self.process.stdin.write(json.dumps({'text': text}).encode() + b'\n')
                self.process.stdin.flush()
            except OSError:
                self._stop()
                raise VoiceError('tts_unavailable')
            header = self._header(COSYVOICE_SENTENCE_SECONDS)
            if header.get('ok') is not True:
                raise VoiceError(header.get('error') if header.get('error') in ('bad_text', 'tts_failed')
                                 else 'tts_failed')
            count = header.get('samples')
            if not isinstance(count, int) or count < 0:
                self._stop()
                raise VoiceError('tts_unavailable')
            data = self._read(count * 2, COSYVOICE_SENTENCE_SECONDS)
            if data is None:
                raise VoiceError('tts_unavailable')
        return np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768, self.rate

    def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(5)
        except Exception:
            process.kill()

    def close(self):
        self._stop()                                        # not under the lock: a sentence may be in flight


# --- speech to text --------------------------------------------------------------------------------------------

def _add_cuda_wheels_to_path():
    """pip nvidia-cublas-cu12 / nvidia-cudnn-cu12 keep their DLLs in site-packages; CTranslate2 needs them on PATH."""
    found = []
    for name in ('nvidia.cublas', 'nvidia.cudnn', 'nvidia.cuda_nvrtc'):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        for location in (spec.submodule_search_locations or []) if spec else []:
            folder = Path(location) / 'bin'
            if folder.is_dir():
                found.append(str(folder))
    if found:
        os.environ['PATH'] = os.pathsep.join(found + [os.environ.get('PATH', '')])
    return bool(found)


class WhisperTranscriber:
    """faster-whisper with a fixed model folder: GPU large-v3-turbo when CUDA works, else CPU small int8.

    On CUDA the weights are int8 with float16 compute: about 1.2 GB on the device instead of 2.2 GB for float16, with
    the same speed and the same text on the fixed Korean test clip (ADR 0001 measurements)."""

    def __init__(self, model_root=DEFAULT_MODEL_ROOT, *, prefer_gpu=True, allow_download=True):
        self.model_root = Path(model_root)
        self.prefer_gpu, self.allow_download = prefer_gpu, allow_download
        self.model = None
        self.device = self.model_name = self.compute = None
        self.downloaded = False                             # True only when this process fetched model files
        self.lock = threading.Lock()                        # transcribe, release and reload never overlap

    def _build(self, name, device, compute):
        from faster_whisper import WhisperModel
        self.model_root.mkdir(parents=True, exist_ok=True)
        try:
            return WhisperModel(name, device=device, compute_type=compute, download_root=str(self.model_root),
                                local_files_only=True)
        except Exception:
            if not self.allow_download:
                raise VoiceError('model_missing')
        model = WhisperModel(name, device=device, compute_type=compute, download_root=str(self.model_root),
                             local_files_only=False)        # first run only: later loads find the files
        self.downloaded = True
        return model

    def load(self):
        attempts = [(CPU_MODEL, 'cpu', 'int8')]
        if self.prefer_gpu:
            _add_cuda_wheels_to_path()
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    attempts.insert(0, (GPU_MODEL, 'cuda', 'int8_float16'))
            except Exception:
                pass
        failure = None
        for name, device, compute in attempts:
            try:
                model = self._build(name, device, compute)
                # CUDA libraries load on first use: prove the device works before choosing it.
                list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language='ko', beam_size=1)[0])
            except Exception as exc:
                failure = exc
                continue
            self.model, self.device, self.model_name, self.compute = model, device, name, compute
            return
        raise failure if isinstance(failure, VoiceError) else VoiceError('stt_unavailable')

    def _inner(self):
        return getattr(self.model, 'model', None)          # the CTranslate2 Whisper object

    @property
    def loaded(self):
        inner = self._inner()
        return inner is not None and bool(inner.model_is_loaded)

    def release(self):
        """Free the GPU copy of the model: a full unload, not parked in RAM. Only on CUDA (the CPU model is small).
        Returns True when something was freed."""
        with self.lock:
            inner = self._inner()
            if self.device != 'cuda' or inner is None or not inner.model_is_loaded:
                return False
            inner.unload_model()
            return True

    def ensure_loaded(self):
        """Reload a released model. Called when the mic opens, and again before transcribing."""
        with self.lock:
            self._ensure_loaded_locked()

    def _ensure_loaded_locked(self):
        inner = self._inner()
        if inner is not None and not inner.model_is_loaded:
            inner.load_model()

    def transcribe(self, samples):
        with self.lock:
            self._ensure_loaded_locked()
            segments, _ = self.model.transcribe(np.asarray(samples, dtype=np.float32), language='ko', beam_size=5,
                                                vad_filter=True, condition_on_previous_text=False)
            return ' '.join(segment.text.strip() for segment in segments).strip()


# --- audio devices ---------------------------------------------------------------------------------------------

class SoundDeviceAudio:
    """Default input and output devices through PortAudio. Callbacks run on PortAudio threads."""

    def open_input(self, callback, rate, block):
        import sounddevice
        stream = sounddevice.InputStream(samplerate=rate, channels=1, dtype='float32', blocksize=block,
                                         callback=lambda data, frames, time, status: callback(data[:, 0].copy()))
        stream.start()
        return stream

    def open_output(self, source, rate, block):
        """source.pull(frames) gives samples; once source.finished() the last block plays and source.done() runs."""
        import sounddevice

        def fill(out, frames, time, status):
            out[:, 0] = source.pull(frames)
            if source.finished():
                raise sounddevice.CallbackStop

        stream = sounddevice.OutputStream(samplerate=rate, channels=1, dtype='float32', blocksize=block,
                                          callback=fill, finished_callback=source.done)
        stream.start()
        return stream


def resample(samples, rate, target):
    """Linear resampling; enough for speech that is only played back."""
    samples = np.asarray(samples, dtype=np.float32)
    if rate == target or samples.size == 0:
        return samples
    positions = np.arange(0, samples.size, rate / target)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


# --- the service -----------------------------------------------------------------------------------------------

class _Playback:
    """Sentences waiting to be played at one rate, pulled block by block by the output stream."""

    def __init__(self, service, rate):
        self.service, self.rate = service, rate
        self.chunks, self.offset = [], 0
        self.lock = threading.Lock()

    def add(self, samples, rate):
        with self.lock:
            self.chunks.append(resample(samples, rate, self.rate))

    def pull(self, frames):
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self.lock:
            while filled < frames and self.chunks:
                chunk = self.chunks[0]
                take = min(frames - filled, chunk.size - self.offset)
                out[filled:filled + take] = chunk[self.offset:self.offset + take]
                filled += take
                self.offset += take
                if self.offset >= chunk.size:
                    self.chunks.pop(0)
                    self.offset = 0
        if filled:
            self.service._frame(out[:filled], self.rate)
        return out

    def drained(self):
        with self.lock:
            return not self.chunks

    def finished(self):
        """Nothing left to play and nothing more coming for this playback."""
        return self.drained() and self.service._reply_complete(self)

    def done(self):
        self.service._playback_done(self)


class VoiceService:
    """One local voice session. Thread-safe. Listeners receive plain dicts:
    {'type': 'state', 'state', 'error'?}, {'type': 'transcript', 'text'}, {'type': 'frame', 'level', 'bins'},
    {'type': 'error', 'code'}."""

    def __init__(self, *, transcriber, synthesizer, audio=None, clock=None, fallback=None):
        import time
        self.transcriber, self.synthesizer, self.fallback = transcriber, synthesizer, fallback
        self.audio = audio or SoundDeviceAudio()
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.listeners: set[Callable[[dict], None]] = set()
        self.state, self.error = 'loading', None
        # Separate generations: a new capture drops a stale transcript, a hush drops stale sentences.
        self.capture_turn = self.speech_turn = 0
        self.mic = None
        self.captured, self.captured_count = [], 0
        self.levels, self.silent_since = [], None
        self.pending_text, self.reply_open = '', False     # reply_open: the page may still send more text
        self.sentences = queue.Queue()
        self.synthesizing = False
        self.playback = self.speaker = None
        self.last_frame = -1.0
        self.closed = False
        # Idle release: when the service was last used, and whether the main voice engine is up. A released engine
        # is woken in the background while the fallback speaks; tts_lifecycle keeps a release and a wake apart.
        self.last_used = self.clock()
        self.tts_awake, self.tts_waking = True, False
        self.tts_wake_failed = False                        # a released engine could not start again this run
        self.tts_lifecycle = threading.Lock()
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self._synthesis_loop, daemon=True, name='voice-tts')
        self.worker.start()

    # listeners
    def subscribe(self, listener):
        with self.lock:
            self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def _emit(self, event):
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                pass

    def _set_state(self, state, error=None):
        changed = (state, error) != (self.state, self.error)
        self.state, self.error = state, error
        if changed:
            self._emit({'type': 'state', 'state': state, **({'error': error} if error else {})})

    def status(self):
        return {'state': self.state, 'error': self.error, 'device': self.transcriber.device,
                'model': self.transcriber.model_name, 'compute': getattr(self.transcriber, 'compute', None),
                'voice': self.synthesizer.name, 'precision': getattr(self.synthesizer, 'precision', None),
                'stt_loaded': getattr(self.transcriber, 'loaded', True), 'tts_awake': self.tts_awake,
                'tts_wake_failed': self.tts_wake_failed}

    # loading
    def start(self):
        threading.Thread(target=self._load, daemon=True, name='voice-load').start()
        threading.Thread(target=self._watch_idle, daemon=True, name='voice-idle').start()

    def _load(self):
        code = None
        try:
            self.transcriber.load()
        except VoiceError as exc:
            code = exc.code
        except Exception:
            code = 'stt_unavailable'
        warm = getattr(self.synthesizer, 'warm_up', None)
        if code is None and warm is not None:
            try:
                warm()                                      # e.g. the first neural TTS call is slow
            except Exception:
                if self.fallback is None:
                    code = 'tts_unavailable'
                else:
                    self.synthesizer = self.fallback        # only a failed engine load falls back
        with self.lock:
            if not self.closed and self.state == 'loading':
                self._set_state('unavailable', code) if code else self._set_state('ready')

    # listening
    def listen(self):
        """Open the mic. Speaking stops first (barge-in)."""
        with self.lock:
            if self.closed or self.state in ('loading', 'unavailable'):
                raise VoiceError('not_ready' if self.state == 'loading' else 'unavailable')
            if self.state == 'listening':
                return
            self._silence_locked()
            self.capture_turn += 1                          # a transcript still being made is dropped
            self.captured, self.captured_count = [], 0
            self.levels, self.silent_since = [], None
            try:
                self.mic = self.audio.open_input(self._captured_block, SAMPLE_RATE, CAPTURE_BLOCK)
            except Exception:
                self.mic = None
                self._set_state('ready', 'mic_unavailable')
                raise VoiceError('mic_unavailable')
            self._set_state('listening')
            self.last_used = self.clock()
        self._wake()                                        # reload while the user speaks

    def _captured_block(self, block):
        with self.lock:
            if self.state != 'listening':
                return
            self.captured.append(block)
            self.captured_count += block.size
            level, _ = self._frame(block, SAMPLE_RATE)
            heard = self.captured_count / SAMPLE_RATE
            # Noise = a low percentile of the levels so far, so fans and room noise set the floor. Speech was heard
            # once the loudest block stood well above it; a pause is a run of blocks near the floor after that.
            self.levels.append(level)
            noise = float(np.percentile(self.levels, 10))
            if max(self.levels) > noise + 0.15 and level < noise + 0.08:
                self.silent_since = heard if self.silent_since is None else self.silent_since
            else:
                self.silent_since = None
            paused = self.silent_since is not None and heard - self.silent_since >= AUTO_STOP_SILENCE
            if paused or heard >= MAX_LISTEN_SECONDS:
                threading.Thread(target=self.stop_listening, daemon=True, name='voice-autostop').start()

    def _close_mic_locked(self):
        mic, self.mic = self.mic, None
        if mic is not None:
            try:
                mic.stop()
                mic.close()
            except Exception:
                pass

    def stop_listening(self):
        """End of push-to-talk: transcribe what was heard. A short capture gives an empty transcript."""
        with self.lock:
            if self.state != 'listening':
                return
            self._close_mic_locked()
            samples = np.concatenate(self.captured) if self.captured else np.zeros(0, dtype=np.float32)
            self.captured, self.captured_count = [], 0
            turn = self.capture_turn
            if samples.size < MIN_SPEECH_SECONDS * SAMPLE_RATE:
                self._set_state('ready')
                self._emit({'type': 'transcript', 'text': ''})
                return
            self._set_state('transcribing')
        threading.Thread(target=self._transcribe, args=(samples, turn), daemon=True, name='voice-stt').start()

    def _transcribe(self, samples, turn):
        try:
            text, failed = self.transcriber.transcribe(samples), None
        except Exception:
            text, failed = '', 'stt_failed'
        del samples                                         # the capture does not outlive this call
        with self.lock:
            self.last_used = self.clock()
            if turn != self.capture_turn or self.state != 'transcribing':
                return                                      # cancelled or replaced meanwhile
            self._set_state('ready', failed)
            if not failed:
                self._emit({'type': 'transcript', 'text': text[:2000]})

    # speaking
    def speak(self, text, final=False):
        """Queue reply text. Each complete sentence plays as soon as it is synthesized; final=True ends the reply.
        Returns False when the service is not in a state to speak (for example the user is talking)."""
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise VoiceError('bad_text')
        with self.lock:
            if self.closed or self.state in ('loading', 'unavailable', 'listening', 'transcribing'):
                return False
            if len(self.pending_text) + len(text) > MAX_PENDING_TEXT:
                raise VoiceError('too_much_text')
            sentences, self.pending_text = split_sentences(self.pending_text + speakable(text), final)
            self.last_used = self.clock()
            self.reply_open = not final
            for sentence in sentences:
                self.sentences.put((self.speech_turn, sentence))
            if sentences:
                self._set_state('speaking')
            self._maybe_done_locked()
            return True

    def hush(self):
        """Stop speaking now (Esc or a new question); queued sentences are dropped. A transcript in progress stays."""
        with self.lock:
            self._silence_locked()

    def cancel(self):
        """Esc: stop listening without transcribing, drop a transcript in progress, and stop speaking."""
        with self.lock:
            self.capture_turn += 1
            if self.state in ('listening', 'transcribing'):
                self._close_mic_locked()
                self.captured, self.captured_count = [], 0
                self._set_state('ready')
            self._silence_locked()

    def _silence_locked(self):
        self.speech_turn += 1
        self.pending_text, self.reply_open = '', False
        while True:
            try:
                self.sentences.get_nowait()
            except queue.Empty:
                break
        speaker, self.speaker, self.playback = self.speaker, None, None
        if speaker is not None:
            try:
                speaker.abort()
                speaker.close()
            except Exception:
                pass
        if self.state == 'speaking':
            self._set_state('ready')

    @staticmethod
    def _synthesize(synthesizer, sentence):
        try:
            samples, rate = synthesizer.synthesize(sentence)
            return samples, rate, None
        except VoiceError as exc:
            return None, None, exc.code
        except Exception:
            return None, None, 'tts_failed'

    def _fall_back(self, broken):
        """An engine that died after loading (for example a stalled CosyVoice worker) is replaced by the fallback for
        the rest of this run, instead of failing every later reply. Returns True when the fallback is now in use."""
        with self.lock:
            if self.fallback is None or broken is self.fallback or self.closed:
                return False
            if self.synthesizer is broken:
                self.synthesizer = self.fallback
        closer = getattr(broken, 'close', None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
        return True

    def _synthesis_loop(self):
        while True:
            item = self.sentences.get()
            if item is None:
                return
            turn, sentence = item
            with self.lock:
                if turn != self.speech_turn:
                    continue
                self.synthesizing = True
                synthesizer = self.synthesizer
                asleep = not self.tts_awake and synthesizer is not self.fallback and self.fallback is not None
                if asleep:
                    synthesizer = self.fallback             # the main voice is reloading; this sentence uses Heami
            if asleep:
                self._wake_tts()
            samples, rate, failed = self._synthesize(synthesizer, sentence)
            if failed == 'tts_unavailable' and self._fall_back(synthesizer):
                samples, rate, failed = self._synthesize(self.fallback, sentence)   # the same sentence, new voice
            with self.lock:
                self.synthesizing = False
                if turn != self.speech_turn:
                    continue
                if failed:
                    self._silence_locked()
                    self._emit({'type': 'error', 'code': failed})
                elif self.playback is not None:
                    self.playback.add(samples, rate)
                else:
                    self.playback = _Playback(self, rate)
                    self.playback.add(samples, rate)
                    try:
                        self.speaker = self.audio.open_output(self.playback, rate, max(64, rate // FRAME_HZ))
                    except Exception:
                        self._silence_locked()
                        self._emit({'type': 'error', 'code': 'speaker_unavailable'})
                self._maybe_done_locked()

    def _reply_complete(self, playback):
        """No more audio will be added to this playback: the reply ended and nothing is queued or being made."""
        with self.lock:
            return (playback is not self.playback or
                    (not self.reply_open and not self.pending_text and not self.synthesizing and self.sentences.empty()))

    def _playback_done(self, playback):
        with self.lock:
            if playback is not self.playback:
                return
            self.last_used = self.clock()
            self.playback, speaker, self.speaker = None, self.speaker, None
            if speaker is not None:
                try:
                    speaker.close()
                except Exception:
                    pass
            self._maybe_done_locked()

    def _maybe_done_locked(self):
        """Speaking ends when nothing is playing and the reply is complete (final text with no sentences left)."""
        if self.state == 'speaking' and self.playback is None and self._reply_complete(None):
            self._set_state('ready')

    # idle release (ADR 0001)
    def _watch_idle(self):
        while not self.stopping.wait(IDLE_CHECK_SECONDS):
            self.release_idle()

    def release_idle(self):
        """Free models that were not used for a while. Only between turns: never while listening or speaking."""
        with self.lock:
            if (self.closed or self.state != 'ready' or self.synthesizing or self.playback is not None
                    or not self.sentences.empty()):
                return
            idle = self.clock() - self.last_used
            release_stt = idle >= STT_IDLE_SECONDS
            engine = self.synthesizer
            release_tts = (idle >= TTS_IDLE_SECONDS and self.tts_awake and not self.tts_waking
                           and self.fallback is not None and engine is not self.fallback
                           and hasattr(engine, 'warm_up') and hasattr(engine, 'close'))
            if release_tts:
                # Taken before the service lock is let go, so a wake that starts now waits for this close and
                # does not have its new worker ended by it. _warm_tts never asks for the service lock while holding
                # tts_lifecycle, so this order cannot deadlock.
                self.tts_lifecycle.acquire()
                self.tts_awake = False
        if release_tts:
            try:
                engine.close()
            except Exception:
                pass
            finally:
                self.tts_lifecycle.release()
        if release_stt:
            releaser = getattr(self.transcriber, 'release', None)
            if releaser is not None:
                try:
                    releaser()
                except Exception:
                    pass

    def _wake(self):
        """Start reloading whatever idle release freed, without blocking the caller."""
        loader = getattr(self.transcriber, 'ensure_loaded', None)
        if loader is not None:
            def load():
                try:
                    loader()
                except Exception:
                    pass                                    # _transcribe tries again and reports stt_failed
            threading.Thread(target=load, daemon=True, name='voice-stt-wake').start()
        self._wake_tts()

    def _wake_tts(self):
        with self.lock:
            if self.closed or self.tts_awake or self.tts_waking or self.synthesizer is self.fallback:
                return
            self.tts_waking, engine = True, self.synthesizer
        threading.Thread(target=self._warm_tts, args=(engine,), daemon=True, name='voice-tts-wake').start()

    def _warm_tts(self, engine):
        try:
            with self.tts_lifecycle:
                engine.warm_up()
            ok = True
        except Exception:
            ok = False
        with self.lock:
            self.tts_waking = False
            closed = self.closed
            if ok and not closed:
                self.tts_awake = True
            elif not ok and self.synthesizer is engine and self.fallback is not None:
                self.synthesizer = self.fallback            # a reload that fails is a broken engine, as in #69
                self.tts_wake_failed = True
        if closed or not ok:
            try:
                engine.close()
            except Exception:
                pass

    # frames
    def _frame(self, block, rate):
        level, bins = frame_features(block, rate)
        now = self.clock()
        if now - self.last_frame >= 0.9 / FRAME_HZ:
            self.last_frame = now
            self._emit({'type': 'frame', 'level': level, 'bins': bins})
        return level, bins

    def close(self):
        self.stopping.set()
        with self.lock:
            self.closed = True
            self.capture_turn += 1
            self._close_mic_locked()
            self._silence_locked()
            self.listeners.clear()
        self.sentences.put(None)
        closer = getattr(self.synthesizer, 'close', None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass


def build_default_service(model_root=DEFAULT_MODEL_ROOT, *, prefer_gpu=True, voice='Heami'):
    """The real backends. Raises ImportError when requirements-voice.txt is not installed."""
    for module in ('faster_whisper', 'sounddevice', 'comtypes'):
        if importlib.util.find_spec(module) is None:
            raise ImportError(module)
    transcriber = WhisperTranscriber(model_root, prefer_gpu=prefer_gpu)
    cosyvoice = CosyVoiceSynthesizer()
    if not cosyvoice.available():                           # scripts/voice/link_cosyvoice.py was not run
        return VoiceService(transcriber=transcriber, synthesizer=SapiSynthesizer(voice))
    return VoiceService(transcriber=transcriber, synthesizer=cosyvoice, fallback=SapiSynthesizer(voice))
