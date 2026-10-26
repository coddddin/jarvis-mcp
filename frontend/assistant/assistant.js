// Assistant view prototype: one orb, one caption, one composer.
// States: idle -> listening (mic level drives the orb) -> thinking -> speaking -> idle.
// PROTOTYPE: replies are samples; the real chat API and speech recognition (PA1-VOICE) are wired in later.
(() => {
  const $ = id => document.getElementById(id);
  const orb = window.JarvisOrb.create($('orb'));
  const caption = $('caption'), reply = $('reply'), input = $('input'), mic = $('mic');
  const CAPTIONS = {idle: '무엇을 도와드릴까요?', listening: '듣고 있어요', thinking: '생각하고 있어요', speaking: ''};
  let mode = 'idle', audio = null, typing = null;

  function setMode(next, text) {
    mode = next;
    orb.setMode(next);
    caption.dataset.mode = next;
    caption.textContent = text ?? CAPTIONS[next];
    mic.setAttribute('aria-pressed', String(next === 'listening'));
  }

  async function startListening() {
    if (mode === 'listening') return;
    stopSpeaking();
    setMode('listening');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
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
    } catch (error) {
      setMode('idle', '마이크를 쓸 수 없어요. 입력창에 적어 주세요.');
    }
  }

  function stopListening() {
    if (audio) { audio.stream.getTracks().forEach(track => track.stop()); audio.context.close(); audio = null; }
    orb.setSource(null);
    if (mode === 'listening') setMode('idle');
  }

  function stopSpeaking() {
    if (typing) { clearInterval(typing); typing = null; }
    orb.setSource(null);
  }

  // Sample answers so the flow can be felt before the chat API is connected.
  const SAMPLES = {
    '오늘 일정': '오늘은 일정이 두 개 있어요. 10시 팀 회의, 15시 치과 예약입니다.',
    '메일': '안 읽은 메일이 12통 있어요. 답장이 필요해 보이는 건 2통입니다.',
    '노트': '지난주 회의 노트에서 "배포는 금요일 오후에 하지 않는다"는 결정을 찾았어요.',
  };
  const answerFor = text => Object.entries(SAMPLES).find(([key]) => text.includes(key))?.[1]
    ?? '지금은 미리보기라 예시 답만 할 수 있어요. 연결하면 실제로 답해 드릴게요.';

  function ask(text) {
    text = text.trim();
    if (!text) return;
    stopListening();
    input.value = '';
    reply.hidden = true;
    setMode('thinking');
    setTimeout(() => speak(answerFor(text)), 1400);
  }

  function speak(text) {
    setMode('speaking');
    reply.hidden = false;
    reply.textContent = '';
    let i = 0, pulse = 0;
    orb.setSource(() => pulse);
    typing = setInterval(() => {
      reply.textContent = text.slice(0, ++i);
      pulse = /[\s.,]/.test(text[i - 1] || '') ? .15 : .45 + Math.random() * .35;
      if (i >= text.length) { stopSpeaking(); setMode('idle'); }
    }, 38);
  }

  $('composer').addEventListener('submit', event => { event.preventDefault(); ask(input.value); });
  mic.addEventListener('click', () => (mode === 'listening' ? stopListening() : startListening()));
  $('suggestions').addEventListener('click', event => {
    const prompt = event.target.closest('button')?.dataset.prompt;
    if (prompt) ask(prompt);
  });

  // Hold Space (outside the text field) to listen; Esc stops listening or speaking.
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { stopListening(); stopSpeaking(); setMode('idle'); }
    if (event.code === 'Space' && document.activeElement !== input && !event.repeat) {
      event.preventDefault();
      startListening();
    }
  });
  document.addEventListener('keyup', event => {
    if (event.code === 'Space' && mode === 'listening' && document.activeElement !== input) {
      stopListening();
      setMode('thinking');
      setTimeout(() => speak(answerFor('')), 1200);
    }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopListening(); });
  window.addEventListener('pagehide', stopListening);
})();
