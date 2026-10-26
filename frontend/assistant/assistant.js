'use strict';
// Assistant view: one orb, one caption, one composer, connected to the local chat API.
// States: idle -> listening (mic level drives the orb) -> thinking -> speaking (draft or reply) -> idle.
// The mic only moves the orb for now; speech recognition is wired in after PA1-VOICE (#51) lands.
(() => {
  const $ = id => document.getElementById(id);
  const orb = window.JarvisOrb.create($('orb'));
  const caption = $('caption'), reply = $('reply'), sources = $('sources'), input = $('input'), mic = $('mic');
  const send = $('send'), presence = $('presenceText'), dot = $('presenceDot'), connectLink = $('connectLink');
  const CAPTIONS = {idle: '무엇을 도와드릴까요?', listening: '듣고 있어요', thinking: '생각하고 있어요', speaking: ''};
  const POLL_MS = 400;  // only while a reply is pending: it carries the streamed draft
  const DISCONNECTED = '연결이 필요해요.';
  let mode = 'idle', audio = null, token = null, conversation = null, pending = false, generation = 0, level = 0;
  let resumePoll = null;  // set while a reply is pending; polling pauses while the tab is hidden

  function setMode(next, text) {
    mode = next;
    orb.setMode(next);
    caption.dataset.mode = next;
    caption.textContent = text ?? CAPTIONS[next];
    mic.setAttribute('aria-pressed', String(next === 'listening'));
  }
  function setPresence(text, online) {
    presence.textContent = text;
    dot.dataset.state = online ? 'online' : 'offline';
    connectLink.hidden = online || text === '연결 중';
  }
  function setBusy(value) {
    pending = value;
    send.disabled = value || !conversation;
    input.disabled = !conversation;
    for (const button of $('suggestions').querySelectorAll('button')) button.disabled = value || !conversation;
  }

  async function api(path, body) {
    const selected = token, version = generation;
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: 'Bearer ' + selected, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit'});
    if (token !== selected || generation !== version) throw new Error('stale');
    if (response.status === 401) {disconnect(); throw new Error(DISCONNECTED);}
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '자비스 연결을 확인해 주세요.');
    return data;
  }

  function disconnect() {
    window.JarvisConnection.forget();
    token = null; conversation = null; generation++;
    setBusy(false);
    setPresence('연결 전', false);
    setMode('idle', DISCONNECTED);
  }

  // --- mic: the level only moves the orb ---
  async function startListening() {
    if (mode === 'listening' || pending) return;
    setMode('listening');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (mode !== 'listening') {stream.getTracks().forEach(track => track.stop()); return;}
      const context = new AudioContext(), analyser = context.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = .6;
      context.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      audio = {stream, context};
      orb.setSource(() => {
        analyser.getByteFrequencyData(data);
        let sum = 0;
        for (let i = 2; i < 64; i++) sum += data[i];   // speech band
        return sum / 62 / 255 * 2.4;
      });
    } catch (_) {
      setMode('idle', '마이크를 쓸 수 없어요. 입력창에 적어 주세요.');
    }
  }
  function stopListening(text) {
    if (audio) {audio.stream.getTracks().forEach(track => track.stop()); audio.context.close(); audio = null;}
    orb.setSource(null);
    if (mode === 'listening') setMode('idle', text);
  }
  const NOT_YET = '음성 입력은 준비 중이에요. 입력창에 적어 주세요.';

  // --- replies ---
  const pulse = () => {level = .55 + Math.random() * .35;};
  const speakingSource = () => (level *= .92);
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
    input.value = '';
    showText('', false); sources.hidden = true;
    setBusy(true);
    setMode('thinking');
    const poll = async () => {
      if (!polling || version !== generation) return;
      if (document.hidden) {resumePoll = () => {resumePoll = null; poll();}; return;}  // resumes on visible
      try {
        const snapshot = await api('/api/chat/conversations/' + identity);
        const turn = snapshot.turns.find(item => item.id === requestId);
        if (polling && turn?.status === 'pending' && turn.draft && turn.draft !== drafted) {
          drafted = turn.draft;
          if (mode !== 'speaking') {setMode('speaking'); orb.setSource(speakingSource);}
          showText(drafted, true); pulse();
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
        setMode('speaking'); orb.setSource(speakingSource); pulse();
        setTimeout(() => {if (mode === 'speaking' && version === generation && !pending) {orb.setSource(null); setMode('idle', '');}}, 900);
      } else {
        showText('', false);
        setMode('idle', '답변을 완료하지 못했어요. 자동으로 다시 실행하지 않아요.');
      }
    } catch (error) {
      polling = false; clearTimeout(timer);
      if (version !== generation) return;
      orb.setSource(null);
      showText('', false);
      if (!input.value) input.value = text;
      if (token) setMode('idle', error.message);
    } finally {
      polling = false; clearTimeout(timer); resumePoll = null;
      if (version === generation) setBusy(false);
    }
  }

  async function connect() {
    setPresence('연결 중', false);
    setBusy(false);
    const result = await window.JarvisConnection.start();
    if (!result.token) {
      setPresence('연결 전', false);
      setMode('idle', result.codeFailed ? '자동 연결 시간이 지났어요. 다시 연결해 주세요.' : DISCONNECTED);
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
      setPresence('연결됨 · ' + (listing.model || '로컬 모델'), true);
      setBusy(false);
      setMode('idle');
    } catch (error) {
      if (version !== generation) return;
      if (token) {setPresence('연결 확인 필요', false); setMode('idle', error.message);}
    }
  }

  $('composer').addEventListener('submit', event => {event.preventDefault(); ask(input.value);});
  mic.addEventListener('click', () => (mode === 'listening' ? stopListening(NOT_YET) : startListening()));
  $('suggestions').addEventListener('click', event => {
    const prompt = event.target.closest('button')?.dataset.prompt;
    if (prompt) ask(prompt);
  });
  // Hold Space (outside the text field) to see the mic level; Esc stops the mic. Neither cancels a server reply.
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') stopListening();
    if (event.code === 'Space' && document.activeElement !== input && !event.repeat) {
      event.preventDefault();
      startListening();
    }
  });
  document.addEventListener('keyup', event => {
    if (event.code === 'Space' && mode === 'listening' && document.activeElement !== input) stopListening(NOT_YET);
  });
  document.addEventListener('visibilitychange', () => {if (document.hidden) stopListening(); else if (resumePoll) resumePoll();});
  window.addEventListener('pagehide', () => {stopListening(); generation++; token = null; resumePoll = null;});
  setPresence('연결 전', false);
  setBusy(false);
  connect();
})();
