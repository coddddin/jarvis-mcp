"""Local voice I/O for the assistant view: push-to-talk capture, speech to text, and replies spoken sentence by
sentence, with level/spectrum frames for the orb.

The page decides what is said: it starts and stops listening, sends the transcript to the chat API itself, and
hands reply text back here to be spoken. This module only moves audio.

- Audio stays in memory. The mic stream is open only while listening and is closed on stop, cancel, error, close
  or client disconnect; captured samples are dropped once transcribed. Nothing is written to disk or logged,
  including transcripts.
- Speech to text: faster-whisper. GPU (large-v3-turbo, float16) when CUDA works, otherwise CPU (small, int8).
  Models live in one fixed folder; after the first download they load with local_files_only, so an offline
  restart never downloads again.
- Text to speech: a Synthesizer. SapiSynthesizer (Windows voice, Heami by default) is the default; another engine,
  such as a local VoiceStudio API, can be added later behind the same interface.
- Barge-in: listening while speaking stops playback at once and drops queued sentences.

Optional dependencies (requirements-voice.txt) are imported only when a real backend is built, so the HUD and
the tests run without them.
"""
import importlib.util
import math
import os
from pathlib import Path
import queue
import re
import threading
from typing import Callable, Protocol

import numpy as np

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
    """faster-whisper with a fixed model folder: GPU large-v3-turbo when CUDA works, else CPU small int8."""

    def __init__(self, model_root=DEFAULT_MODEL_ROOT, *, prefer_gpu=True, allow_download=True):
        self.model_root = Path(model_root)
        self.prefer_gpu, self.allow_download = prefer_gpu, allow_download
        self.model = None
        self.device = self.model_name = None
        self.downloaded = False                             # True only when this process fetched model files

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
                    attempts.insert(0, (GPU_MODEL, 'cuda', 'float16'))
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
            self.model, self.device, self.model_name = model, device, name
            return
        raise failure if isinstance(failure, VoiceError) else VoiceError('stt_unavailable')

    def transcribe(self, samples):
        segments, _ = self.model.transcribe(np.asarray(samples, dtype=np.float32), language='ko', beam_size=5,
                                            vad_filter=True, condition_on_previous_text=False)
        return ' '.join(segment.text.strip() for segment in segments).strip()


# --- audio devices ---------------------------------------------------------------------------------------------

class SoundDeviceAudio:
    """Default input and output devices through PortAudio. Streams run their callbacks on PortAudio threads."""

    def open_input(self, callback, rate, block):
        import sounddevice
        stream = sounddevice.InputStream(samplerate=rate, channels=1, dtype='float32', blocksize=block,
                                         callback=lambda data, frames, time, status: callback(data[:, 0].copy()))
        stream.start()
        return stream

    def open_output(self, callback, rate, block):
        import sounddevice

        def fill(out, frames, time, status):
            data = callback(frames)
            out[:, 0] = data
            if data.size and not data.any() and callback.finished():
                raise sounddevice.CallbackStop
        fill.finished = callback.finished
        stream = sounddevice.OutputStream(samplerate=rate, channels=1, dtype='float32', blocksize=block, callback=fill,
                                          finished_callback=callback.done)
        stream.start()
        return stream


# --- the service -----------------------------------------------------------------------------------------------

class _Playback:
    """Samples waiting to be played at one rate, pulled block by block by the output stream."""

    def __init__(self, service, rate):
        self.service, self.rate = service, rate
        self.chunks, self.offset = [], 0
        self.lock = threading.Lock()
        self.closed = False

    def add(self, samples):
        with self.lock:
            self.chunks.append(np.asarray(samples, dtype=np.float32))

    def __call__(self, frames):
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

    def finished(self):
        with self.lock:
            return not self.chunks and self.service._synthesis_idle()

    def done(self):
        self.service._playback_done(self)


class VoiceService:
    """One local voice session. Thread-safe; events go to listeners as plain dicts (see EVENTS in voice_routes)."""

    def __init__(self, *, transcriber, synthesizer, audio=None, clock=None):
        import time
        self.transcriber, self.synthesizer = transcriber, synthesizer
        self.audio = audio or SoundDeviceAudio()
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.listeners: set[Callable[[dict], None]] = set()
        self.state, self.error = 'loading', None
        self.turn = 0                                       # bumps on every listen/cancel/hush: stale work is dropped
        self.mic = None
        self.captured, self.captured_count = [], 0
        self.heard_speech, self.silent_since, self.noise = False, None, 0.0
        self.pending_text = ''
        self.sentences = queue.Queue()
        self.synthesizing = False
        self.playback = self.speaker = None
        self.last_frame = 0.0
        self.closed = False
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
        self.state, self.error = state, error
        self._emit({'type': 'state', 'state': state, **({'error': error} if error else {})})

    def status(self):
        return {'state': self.state, 'error': self.error, 'device': self.transcriber.device,
                'model': self.transcriber.model_name, 'voice': self.synthesizer.name}

    # loading
    def start(self):
        threading.Thread(target=self._load, daemon=True, name='voice-load').start()

    def _load(self):
        try:
            self.transcriber.load()
        except VoiceError as exc:
            with self.lock:
                self._set_state('unavailable', exc.code)
            return
        except Exception:
            with self.lock:
                self._set_state('unavailable', 'stt_unavailable')
            return
        with self.lock:
            if self.state == 'loading':
                self._set_state('ready')

    # listening
    def listen(self):
        with self.lock:
            if self.closed or self.state in ('loading', 'unavailable'):
                raise VoiceError('not_ready' if self.state == 'loading' else 'unavailable')
            if self.state == 'listening':
                return
            self._silence_locked()                          # barge-in: speaking stops before the mic opens
            self.turn += 1
            self.captured, self.captured_count = [], 0
            self.heard_speech, self.silent_since, self.noise = False, None, 0.0
            try:
                self.mic = self.audio.open_input(self._captured_block, SAMPLE_RATE, CAPTURE_BLOCK)
            except Exception:
                self.mic = None
                self._set_state('ready')
                raise VoiceError('mic_unavailable')
            self._set_state('listening')

    def _captured_block(self, block):
        with self.lock:
            if self.state != 'listening':
                return
            self.captured.append(block)
            self.captured_count += block.size
            level, _ = self._frame(block, SAMPLE_RATE)
            now = self.captured_count / SAMPLE_RATE
            # Speech = clearly above the quietest level heard so far (adapts to fans and room noise).
            self.noise = level if not self.heard_speech and self.captured_count <= CAPTURE_BLOCK else min(self.noise, level) if self.noise else level
            if level > self.noise + 0.15:
                self.heard_speech, self.silent_since = True, None
            elif self.heard_speech and self.silent_since is None:
                self.silent_since = now
            ended = (self.heard_speech and self.silent_since is not None and now - self.silent_since >= AUTO_STOP_SILENCE)
            if ended or now >= MAX_LISTEN_SECONDS:
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
        """End of push-to-talk: transcribe what was heard. Short or silent captures give an empty transcript."""
        with self.lock:
            if self.state != 'listening':
                return
            self._close_mic_locked()
            samples = np.concatenate(self.captured) if self.captured else np.zeros(0, dtype=np.float32)
            self.captured, self.captured_count = [], 0
            turn = self.turn
            if samples.size < MIN_SPEECH_SECONDS * SAMPLE_RATE:
                self._set_state('ready')
                self._emit({'type': 'transcript', 'text': ''})
                return
            self._set_state('transcribing')
        threading.Thread(target=self._transcribe, args=(samples, turn), daemon=True, name='voice-stt').start()

    def _transcribe(self, samples, turn):
        try:
            text = self.transcriber.transcribe(samples)
            failed = None
        except Exception:
            text, failed = '', 'stt_failed'
        del samples                                         # nothing of the capture outlives this call
        with self.lock:
            if turn != self.turn or self.state != 'transcribing':
                return                                      # cancelled meanwhile
            self._set_state('ready', failed)
            if not failed:
                self._emit({'type': 'transcript', 'text': text[:2000]})

    # speaking
    def speak(self, text, final=False):
        """Queue reply text; complete sentences start playing as soon as each is synthesized."""
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise VoiceError('bad_text')
        with self.lock:
            if self.closed or self.state in ('loading', 'unavailable', 'listening', 'transcribing'):
                return False
            if len(self.pending_text) + len(text) > MAX_PENDING_TEXT:
                raise VoiceError('too_much_text')
            sentences, self.pending_text = split_sentences(self.pending_text + speakable(text), final)
            for sentence in sentences:
                self.sentences.put((self.turn, sentence))
            if sentences and self.state != 'speaking':
                self._set_state('speaking')
            elif final and not sentences and self.state == 'speaking':
                self._maybe_done_locked()
            return True

    def hush(self):
        """Stop speaking now (Esc or a new question); queued sentences are dropped."""
        with self.lock:
            self._silence_locked()

    def cancel(self):
        """Esc: stop listening without transcribing, and stop speaking."""
        with self.lock:
            self.turn += 1
            if self.state in ('listening', 'transcribing'):
                self._close_mic_locked()
                self.captured, self.captured_count = [], 0
                self._set_state('ready')
            self._silence_locked()

    def _silence_locked(self):
        self.turn += 1
        self.pending_text = ''
        while True:
            try:
                self.sentences.get_nowait()
            except queue.Empty:
                break
        speaker, self.speaker, self.playback = self.speaker, None, None
        if speaker is not None:
            try:
                speaker.abort() if hasattr(speaker, 'abort') else speaker.stop()
                speaker.close()
            except Exception:
                pass
        if self.state == 'speaking':
            self._set_state('ready')

    def _synthesis_loop(self):
        while True:
            item = self.sentences.get()
            if item is None:
                return
            turn, sentence = item
            with self.lock:
                if turn != self.turn:
                    continue
                self.synthesizing = True
            try:
                samples, rate = self.synthesizer.synthesize(sentence)
                failed = None
            except VoiceError as exc:
                samples, rate, failed = None, None, exc.code
            except Exception:
                samples, rate, failed = None, None, 'tts_failed'
            with self.lock:
                self.synthesizing = False
                if turn != self.turn:
                    continue
                if failed:
                    self._silence_locked()
                    self._emit({'type': 'error', 'code': failed})
                    continue
                if self.playback is not None and self.playback.rate != rate:
                    self.playback.add(np.zeros(0, dtype=np.float32))   # rates never mix within one engine
                if self.playback is None:
                    self.playback = _Playback(self, rate)
                    self.playback.add(samples)
                    try:
                        self.speaker = self.audio.open_output(self.playback, rate, max(64, rate // FRAME_HZ))
                    except Exception:
                        self.playback = None
                        self._silence_locked()
                        self._emit({'type': 'error', 'code': 'speaker_unavailable'})
                else:
                    self.playback.add(samples)

    def _synthesis_idle(self):
        with self.lock:
            return not self.synthesizing and self.sentences.empty() and not self.pending_text

    def _playback_done(self, playback):
        with self.lock:
            if playback is not self.playback:
                return
            self.playback, speaker, self.speaker = None, self.speaker, None
            if speaker is not None:
                try:
                    speaker.close()
                except Exception:
                    pass
            self._maybe_done_locked()

    def _maybe_done_locked(self):
        if self.state == 'speaking' and self.playback is None and self._synthesis_idle():
            self._set_state('ready')

    # frames
    def _frame(self, block, rate):
        level, bins = frame_features(block, rate)
        now = self.clock()
        if now - self.last_frame >= 1 / FRAME_HZ * 0.9:
            self.last_frame = now
            self._emit({'type': 'frame', 'level': level, 'bins': bins})
        return level, bins

    def close(self):
        with self.lock:
            self.closed = True
            self.turn += 1
            self._close_mic_locked()
            self._silence_locked()
            self.listeners.clear()
        self.sentences.put(None)


def build_default_service(model_root=DEFAULT_MODEL_ROOT, *, prefer_gpu=True, voice='Heami'):
    """The real backends. Raises ImportError when requirements-voice.txt is not installed."""
    for module in ('faster_whisper', 'sounddevice', 'comtypes'):
        if importlib.util.find_spec(module) is None:
            raise ImportError(module)
    return VoiceService(transcriber=WhisperTranscriber(model_root, prefer_gpu=prefer_gpu),
                        synthesizer=SapiSynthesizer(voice))
