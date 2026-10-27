'use strict';
// Assistant view: one orb, one caption, one composer, connected to the local chat API.
// State lives in presence.js: this file sends events (connect, listen, ask, speak, fail) and the orb, caption and
// status line follow. The mic only moves the orb for now; speech recognition is wired in after PA1-VOICE (#51) lands.
(() => {
  const $ = id => document.getElementById(id);
  const presenceState = window.JarvisPresence.create();
  window.JarvisOrb.create($('orb'), presenceState);
  const caption = $('caption'), reply = $('reply'), sources = $('sources'), input = $('input'), mic = $('mic');
  const send = $('send'), presence = $('presenceText'), dot = $('presenceDot'), connectLink = $('connectLink');
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

  function disconnect() {
    window.JarvisConnection.forget();
    token = null; conversation = null; generation++;
    setBusy(false);
    go('disconnected', DISCONNECTED);
  }

  // --- mic: level and spectrum go to presence each frame; they only move the orb ---
  const raf = window.requestAnimationFrame?.bind(window), cancelRaf = window.cancelAnimationFrame?.bind(window);
  async function startListening() {
    if (presenceState.state === 'listening' || pending) return;
    if (!presenceState.dispatch('listen')) return;
    caption.textContent = CAPTIONS.listening;
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
  function stopListening(text) {
    if (audio) {
      if (audio.frame && cancelRaf) cancelRaf(audio.frame);
      audio.stream.getTracks().forEach(track => track.stop()); audio.context.close(); audio = null;
    }
    if (presenceState.state === 'listening') go('listenEnd', text);
  }
  const NOT_YET = '음성 입력은 준비 중이에요. 입력창에 적어 주세요.';

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
    const line = sourceLine(turn.sources);
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
          presenceState.speechText(turn.draft.startsWith(drafted) ? turn.draft.slice(drafted.length) : turn.draft);
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
        const ms = presenceState.speechText(rest);
        go('speak');
        spokenTimer = setTimeout(() => {
          if (turnNumber === asked && presenceState.state === 'speaking' && version === generation && !pending) go('spoken', '');
        }, Math.min(SPOKEN_MS[1], Math.max(SPOKEN_MS[0], ms)));
      } else {
        showText('', false);
        go('fail', '답변을 완료하지 못했어요. 자동으로 다시 실행하지 않아요.');
      }
    } catch (error) {
      polling = false; clearTimeout(timer);
      if (!input.value) input.value = text;  // first, so a disconnect (401) never loses the question
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
      go('connected', undefined, {model: listing.model});
    } catch (error) {
      if (version !== generation) return;
      if (token) {presenceState.setLinked(false); go('fail', error.message);}
    }
  }

  $('composer').addEventListener('submit', event => {event.preventDefault(); ask(input.value);});
  mic.addEventListener('click', () => (presenceState.state === 'listening' ? stopListening(NOT_YET) : startListening()));
  $('suggestions').addEventListener('click', event => {
    const prompt = event.target.closest('button')?.dataset.prompt;
    if (prompt) ask(prompt);
  });
  // Hold Space to see the mic level, only when nothing is focused: on a button, link or field Space keeps its own
  // meaning. Esc stops the mic. Neither cancels a server reply.
  const spaceIsFree = () => !document.activeElement || document.activeElement === document.body;
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') stopListening();
    if (event.code === 'Space' && spaceIsFree() && !event.repeat) {
      event.preventDefault();
      startListening();
    }
  });
  document.addEventListener('keyup', event => {
    if (event.code === 'Space' && presenceState.state === 'listening' && spaceIsFree()) stopListening(NOT_YET);
  });
  document.addEventListener('visibilitychange', () => {if (document.hidden) stopListening(); else if (resumePoll) resumePoll();});
  window.addEventListener('pagehide', () => {stopListening(); generation++; token = null; resumePoll = null;});
  setBusy(false);
  connect();
})();
