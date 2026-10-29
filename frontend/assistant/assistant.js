'use strict';
// Assistant view: one orb, one caption, one composer, connected to the local chat API.
// State lives in presence.js: this file sends events (connect, listen, ask, speak, fail) and the orb, caption and
// status line follow. With the local voice service (hud --voice, voice.js) the mic, speech to text and spoken replies
// are native and its frames move the orb; without it the browser mic only moves the orb.
(() => {
  const $ = id => document.getElementById(id);
  const presenceState = window.JarvisPresence.create();
  window.JarvisOrb.create($('orb'), presenceState);
  const caption = $('caption'), reply = $('reply'), sources = $('sources'), input = $('input'), mic = $('mic');
  const send = $('send'), presence = $('presenceText'), dot = $('presenceDot'), connectLink = $('connectLink');
  const hint = $('hint');
  // Model-name button: the model that answers chat and voice (brain.js; the status line follows a switch).
  const brain = window.JarvisBrain?.create({button: $('modelButton'), panel: $('modelPanel'), token: () => token,
    onChange: selection => presenceState.setModel(selection.label)});
  const CAPTIONS = {connecting: '연결하고 있어요', idle: '무엇을 도와드릴까요?', listening: '듣고 있어요',
    thinking: '생각하고 있어요', speaking: ''};
  const POLL_MS = 400;  // only while a reply is pending: it carries the streamed draft
  const SPOKEN_MS = [900, 4000];  // how long the reply keeps the speaking look, from the pseudo speech length
  const DISCONNECTED = '연결이 필요해요.';
  let audio = null, token = null, conversation = null, pending = false, generation = 0;
  let resumePoll = null;  // set while a reply is pending; polling pauses while the tab is hidden
  let asked = 0, spokenTimer = null;  // the speaking look ends only for the answer that started it

  // Send an event; the caption shows text, or the state's own caption when text is undefined.
  function go(event, text, detail) {
    presenceState.dispatch(event, detail);
    caption.textContent = text ?? CAPTIONS[presenceState.state] ?? '';
  }
  // The status line shows the connection only: not connected, connecting, connected (model), or needs a check.
  presenceState.subscribe((state, _, info) => {
    caption.dataset.mode = state;
    mic.setAttribute('aria-pressed', String(state === 'listening'));
    const line = state === 'offline' ? ['연결 전', 'offline'] : state === 'connecting' ? ['연결 중', 'connecting']
      : info.linked ? ['연결됨 · ' + (info.model || '로컬 모델'), 'online'] : ['연결 확인 필요', 'warn'];
    presence.textContent = line[0];
    dot.dataset.state = line[1];
    connectLink.hidden = line[1] === 'online' || line[1] === 'connecting';
  });
  // Desktop app only: its mini ring follows this page's state. The app allows this page one command, 'presence'.
  const desktop = window.__TAURI_INTERNALS__;
  if (desktop && typeof desktop.invoke === 'function') {
    presenceState.subscribe(state => {Promise.resolve(desktop.invoke('presence', {state})).catch(() => {});});
  }
  function setBusy(value) {
    pending = value;
    send.disabled = value || !conversation;
    input.disabled = !conversation;
    for (const button of $('suggestions').querySelectorAll('button')) button.disabled = value || !conversation;
  }

  async function api(path, body) {
    const selected = token, version = generation;
    let response;
    try {
      response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
        headers: {Authorization: 'Bearer ' + selected, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit'});
    } catch (_) {  // the server is not running or the network is down
      if (token === selected && generation === version) presenceState.setLinked(false);
      throw new Error('자비스 서버에 연결하지 못했어요. 서버가 켜져 있는지 확인해 주세요.');
    }
    if (token !== selected || generation !== version) throw new Error('stale');
    presenceState.setLinked(true);
    if (response.status === 401) {disconnect(); throw new Error(DISCONNECTED);}
    let data;
    try {data = await response.json();} catch (_) {  // e.g. an HTML error page from something in between
      throw new Error('자비스 서버 응답을 읽지 못했어요. 잠시 뒤 다시 시도해 주세요.');
    }
    if (!response.ok) throw new Error(data.error || '자비스 연결을 확인해 주세요.');
    return data;
  }

  // ADR 0001: the chat model leaves the GPU after 5 idle minutes. Typing, or Space without the voice service, asks
  // the server to load it now. With the voice service its listen op does the same, so one key press wakes both.
  let warmedAt = -Infinity;
  function warmChat() {
    const now = Date.now();
    if (!token || now - warmedAt < 60000) return;
    warmedAt = now;
    api('/api/chat/warm', {}).catch(() => {});
  }

  function disconnect() {
    voice?.close(); voice = null; voiceReady = false; voiceListening = false;
    hint.textContent = HINTS.off;
    token = null; conversation = null; generation++;
    brain?.reset();
    window.JarvisConnection.forget();
    setBusy(false);
    go('disconnected', DISCONNECTED);
  }

  // --- mic: level and spectrum go to presence each frame; they only move the orb ---
  const raf = window.requestAnimationFrame?.bind(window), cancelRaf = window.cancelAnimationFrame?.bind(window);
  async function startListening() {
    if (presenceState.state === 'listening' || pending) return;
    if (!presenceState.dispatch('listen')) return;
    caption.textContent = CAPTIONS.listening;
    if (voiceOn()) {voiceListening = true; voice.listen(); return;}  // the service stops speaking first
    warmChat();
    let stream = null, context = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (presenceState.state !== 'listening') {stream.getTracks().forEach(track => track.stop()); return;}
      context = new AudioContext();
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = .6;
      context.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount), bins = new Float32Array(presenceState.BINS);
      audio = {stream, context, frame: 0};
      const pump = () => {
        if (!audio) return;
        analyser.getByteFrequencyData(data);
        let sum = 0;
        for (let i = 2; i < 64; i++) sum += data[i];   // speech band
        // 32 log-spaced bins from about 190 Hz to 6 kHz
        for (let i = 0; i < bins.length; i++) bins[i] = (data[Math.round(2 * Math.pow(32, i / bins.length))] || 0) / 255;
        presenceState.setLevel(sum / 62 / 255 * 2.4);
        presenceState.setSpectrum(bins);
        audio.frame = raf ? raf(pump) : 0;
      };
      pump();
    } catch (_) {
      // Audio setup can fail after the mic was granted: never leave it recording.
      if (stream) stream.getTracks().forEach(track => track.stop());
      if (context) {try {context.close();} catch (__) { /* already closed */ }}
      audio = null;
      go('fail', '마이크를 쓸 수 없어요. 입력창에 적어 주세요.');
    }
  }
  // Without a transcript: Esc, a hidden tab, a new question. finishListening() is the push-to-talk release.
  function stopListening(text) {
    if (voiceListening) {voiceListening = false; voice?.cancel();}
    if (audio) {
      if (audio.frame && cancelRaf) cancelRaf(audio.frame);
      audio.stream.getTracks().forEach(track => track.stop()); audio.context.close(); audio = null;
    }
    if (presenceState.state === 'listening') go('listenEnd', text);
  }
  function finishListening() {
    if (!voiceListening) {stopListening(NOT_YET); return;}
    voiceListening = false;
    voice.stop();                                       // a transcript follows and is asked
    go('listenEnd', HEARING);
  }
  const NOT_YET = '음성 입력은 준비 중이에요. 입력창에 적어 주세요.';

  // --- local voice service: native mic, transcript into ask(), replies spoken sentence by sentence ---
  const HINTS = {off: 'Space를 길게 누르면 마이크 반응을 보여 줘요. 음성 입력은 준비 중이에요.',
    loading: '음성 엔진을 준비하고 있어요. 그동안은 입력창에 적어 주세요.',
    on: 'Space를 누른 채 말하고 놓으면 보내요. Esc는 듣기와 말하기를 멈춰요.'};
  const VOICE_ERRORS = {mic_unavailable: '마이크를 쓸 수 없어요. 입력창에 적어 주세요.',
    stt_failed: '잘 알아듣지 못했어요. 다시 말해 주세요.', not_ready: '음성 엔진을 준비하고 있어요. 잠시 뒤 다시 해 주세요.',
    unavailable: '음성 엔진을 쓸 수 없어요. 입력창에 적어 주세요.', tts_failed: '답변을 소리로 읽지 못했어요.',
    tts_unavailable: '답변을 소리로 읽지 못했어요.', speaker_unavailable: '스피커를 쓸 수 없어요.'};
  const HEARING = '알아듣고 있어요';
  let voice = null, voiceReady = false, voiceListening = false, voiceTurn = 0, voiceSpoke = false;
  const voiceOn = () => Boolean(voice && voiceReady);
  function startVoice() {
    if (!window.JarvisVoice) return;
    voice?.close();
    const version = generation;
    const client = window.JarvisVoice.connect(token, {
      hello: status => voiceState(status.state, status.error),
      state: voiceState,
      transcript: text => {
        if (version !== generation) return;
        if (text.trim()) ask(text);
        else if (presenceState.state === 'idle') caption.textContent = '말소리를 듣지 못했어요. 다시 말해 주세요.';
      },
      frame: (level, bins) => {presenceState.setLevel(level); presenceState.setSpectrum(bins);},
      error: code => {
        const text = VOICE_ERRORS[code] || '음성 처리에 실패했어요.';
        if (presenceState.state === 'speaking' && !pending) go('spoken', text); else caption.textContent = text;
      },
      closed: () => {
        if (voice !== client) return;
        voice = null; voiceReady = false;
        if (voiceListening) {voiceListening = false; go('listenEnd');}
        hint.textContent = HINTS.off;
      },
    });
    voice = client;
  }
  function voiceState(state, error) {
    voiceReady = ['ready', 'listening', 'transcribing', 'speaking'].includes(state);
    hint.textContent = voiceReady ? HINTS.on : state === 'loading' ? HINTS.loading : HINTS.off;
    if (state === 'speaking') voiceSpoke = true;
    if (voiceListening && state !== 'listening') {
      // The service stopped by itself (a pause after speech, the time limit, or a mic failure).
      voiceListening = false;
      if (presenceState.state === 'listening') go('listenEnd', error ? VOICE_ERRORS[error] || HEARING : HEARING);
    }
    if (state === 'ready' && voiceSpoke && voiceTurn === asked && presenceState.state === 'speaking' && !pending) {
      go('spoken', '');
    }
  }
  // Reply text goes to the voice service when it is on; otherwise the orb plays pseudo speech for it.
  function speakText(text, final) {
    if (voiceOn()) {voice.speak(text, final); return 0;}
    return presenceState.speechText(text);
  }

  // --- replies ---
  function showText(text, draft) {
    reply.hidden = !text;
    reply.textContent = text;
    reply.classList.toggle('draft', draft);
    reply.setAttribute('aria-label', draft ? '작성 중인 답변, 아직 확인 전' : '자비스의 답변');
  }
  // One line under the reply: notes the answer cited, or notes that were searched but not cited.
  function sourceLine(list) {
    const valid = (Array.isArray(list) ? list : []).filter(item => item && typeof item.title === 'string'
      && Number.isInteger(item.line_start) && Number.isInteger(item.line_end));
    if (!valid.length) return '';
    const label = item => item.title + ' 줄 ' + (item.line_start === item.line_end ? item.line_start : item.line_start + '-' + item.line_end);
    const cited = valid.filter(item => item.cited === true);
    return cited.length ? '출처: ' + cited.map(label).join(', ')
      : '검색된 노트: ' + valid.map(label).join(', ') + ' (답변 인용 확인 안 됨)';
  }
  function showTurn(turn) {
    showText(turn.assistant, false);
    // Name the model only when it is not the plain local default: an external answer, or a local takeover.
    const model = turn.model?.external || turn.model?.local_reason ? window.JarvisBrain?.answeredBy(turn) : '';
    const line = [sourceLine(turn.sources), model].filter(Boolean).join(' · ');
    sources.hidden = !line;
    sources.textContent = line;
  }

  async function ask(text) {
    text = text.trim();
    if (!text || pending || !conversation) return;
    stopListening();
    const identity = conversation, version = generation, requestId = crypto.randomUUID().replaceAll('-', '');
    let polling = true, timer = null, drafted = '';
    const turnNumber = ++asked;
    clearTimeout(spokenTimer);
    if (voiceOn()) {voice.hush(); voiceSpoke = false; voiceTurn = turnNumber;}
    input.value = '';
    showText('', false); sources.hidden = true;
    setBusy(true);
    go('ask');
    const poll = async () => {
      if (!polling || version !== generation) return;
      if (document.hidden) {resumePoll = () => {resumePoll = null; poll();}; return;}  // resumes on visible
      try {
        const snapshot = await api('/api/chat/conversations/' + identity);
        const turn = snapshot.turns.find(item => item.id === requestId);
        if (polling && turn?.status === 'pending' && turn.draft && turn.draft !== drafted) {
          if (!turn.draft.startsWith(drafted) && voiceOn()) voice.hush();   // the draft was rewritten
          speakText(turn.draft.startsWith(drafted) ? turn.draft.slice(drafted.length) : turn.draft, false);
          drafted = turn.draft;
          if (presenceState.state !== 'speaking') go('speak');
          showText(drafted, true);
        }
      } catch (_) { /* the final response decides; a missed poll only delays the draft */ }
      if (polling && version === generation) timer = setTimeout(poll, POLL_MS);
    };
    timer = setTimeout(poll, POLL_MS);
    try {
      const result = await api('/api/chat/conversations/' + identity + '/messages', {request_id: requestId, message: text, mode: 'auto'});
      polling = false; clearTimeout(timer); resumePoll = null;
      if (version !== generation) return;
      const turn = result.turns.find(item => item.id === requestId);
      if (turn?.status === 'completed' && turn.assistant) {
        showTurn(turn);
        const rest = turn.assistant.startsWith(drafted) ? turn.assistant.slice(drafted.length) : turn.assistant;
        const spoken = voiceOn();
        const ms = speakText(rest, true);
        go('speak');
        // With voice the service says when it is done; this only ends a reply it had nothing to say for.
        spokenTimer = setTimeout(() => {
          if (turnNumber === asked && presenceState.state === 'speaking' && version === generation && !pending
              && !(spoken && voiceSpoke)) go('spoken', '');
        }, spoken ? 2500 : Math.min(SPOKEN_MS[1], Math.max(SPOKEN_MS[0], ms)));
      } else {
        if (voiceOn()) voice.hush();
        showText('', false);
        go('fail', '답변을 완료하지 못했어요. 자동으로 다시 실행하지 않아요.');
      }
    } catch (error) {
      polling = false; clearTimeout(timer);
      if (!input.value) input.value = text;  // first, so a disconnect (401) never loses the question
      if (voiceOn()) voice.hush();
      if (version !== generation) return;
      showText('', false);
      if (token) go('fail', error.message);
    } finally {
      polling = false; clearTimeout(timer); resumePoll = null;
      if (version === generation) setBusy(false);
    }
  }

  async function connect() {
    go('connecting');
    setBusy(false);
    const result = await window.JarvisConnection.start();
    if (!result.token) {
      go('disconnected', result.codeFailed ? '자동 연결 시간이 지났어요. 다시 연결해 주세요.' : DISCONNECTED);
      return;
    }
    token = result.token;
    const version = generation;
    try {
      const listing = await api('/api/chat/conversations');
      const latest = listing.conversations?.[0]?.id;
      const chosen = latest || (await api('/api/chat/conversations', {})).id;
      if (version !== generation) return;
      conversation = chosen;
      setBusy(false);
      go('connected', undefined, {model: listing.brain?.label || listing.model});
      brain?.refresh();
      window.JarvisConnection.publish?.(token);
      startVoice();
    } catch (error) {
      if (version !== generation) return;
      if (token) {presenceState.setLinked(false); go('fail', error.message);}
    }
  }

  $('composer').addEventListener('submit', event => {event.preventDefault(); ask(input.value);});
  input.addEventListener('input', warmChat);
  mic.addEventListener('click', () => (presenceState.state === 'listening' ? finishListening() : startListening()));
  $('suggestions').addEventListener('click', event => {
    const prompt = event.target.closest('button')?.dataset.prompt;
    if (prompt) ask(prompt);
  });
  // Hold Space to talk (or, without the voice service, to see the mic level), only when nothing is focused: on a
  // button, link or field Space keeps its own meaning. Esc stops the mic and the spoken reply; a server reply that is
  // still being written continues on screen.
  const spaceIsFree = () => !document.activeElement || document.activeElement === document.body;
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      stopListening();
      if (voiceOn()) {voice.cancel(); if (presenceState.state === 'speaking' && !pending) go('spoken', '');}
    }
    if (event.code === 'Space' && spaceIsFree() && !event.repeat) {
      event.preventDefault();
      startListening();
    }
  });
  document.addEventListener('keyup', event => {
    if (event.code === 'Space' && presenceState.state === 'listening' && spaceIsFree()) finishListening();
  });
  document.addEventListener('visibilitychange', () => {if (document.hidden) stopListening(); else if (resumePoll) resumePoll();});
  window.addEventListener('pagehide', () => {
    stopListening(); voice?.close(); voice = null; generation++; token = null; resumePoll = null;
    window.JarvisConnection.publish?.(null);
  });
  window.JarvisConnection.subscribe?.(selected => {if (token && selected === null) disconnect();});
  window.addEventListener('pageshow', event => {if (event.persisted) connect();});
  setBusy(false);
  connect();
})();
