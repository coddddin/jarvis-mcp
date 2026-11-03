const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.events = {}; this.dataset = {}; this.value = ''; this.hidden = false;
    this.disabled = false; this.textContent = ''; this.attributes = {}; this.classes = new Set();
    this.classList = {toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name), contains: name => this.classes.has(name)};
  }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll() { return this.buttons || []; }
  closest() { return this; }
}
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
const source = (changes = {}) => ({note_id: 'm', title: '프로젝트 회의록', path: 'JARVIS-Vault/m.md', line_start: 1, line_end: 13, cited: true, ...changes});

async function harness({hash = '', stored = null, handler, mic = false, audioFails = false, desktop = null,
                        voice = false} = {}) {
  const track = {stopped: false, stop() {this.stopped = true;}};
  const ids = [...fs.readFileSync('frontend/assistant/index.html', 'utf8').matchAll(/id="([^"]+)"/g)].map(m => m[1]);
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  elements.suggestions.buttons = [new Element('button')];
  const storage = new Map(stored ? [['jarvis-hud-connection', stored]] : []);
  const docEvents = {}, winEvents = {}, requests = [], timers = [], orbCalls = [];
  const location = {hash, pathname: '/assistant', host: '127.0.0.1:8787', protocol: 'http:'};
  const sockets = [];
  const body = new Element('body');
  const document = {getElementById: id => elements[id], hidden: false, activeElement: body, body,
    addEventListener: (name, fn) => docEvents[name] = fn};
  const context = {
    document, location, crypto: {randomUUID: () => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'},
    history: {replaceState: (_, __, path) => {location.hash = ''; location.replaced = path;}},
    sessionStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setTimeout: (fn, ms) => {const timer = {fn, ms}; timers.push(timer); return timer;}, clearTimeout: timer => {if (timer) timer.cancelled = true;},
    navigator: {mediaDevices: {getUserMedia: async () => {if (!mic) throw new Error('no mic in tests'); return {getTracks: () => [track]};}}},
    AudioContext: class {constructor() {if (audioFails) throw new Error('audio setup failed');} createAnalyser() {return {getByteFrequencyData() {}, frequencyBinCount: 8};} createMediaStreamSource() {return {connect() {}};} close() {}},
    Uint8Array, Float32Array, performance: {now: () => 0}, queueMicrotask,
    WebSocket: class {
      constructor(url) {this.url = url; this.sent = []; sockets.push(this);}
      send(data) {this.sent.push(JSON.parse(data));}
      close() {if (!this.closed) {this.closed = true; this.onclose?.();}}
      open() {this.onopen();}
      receive(message) {this.onmessage({data: JSON.stringify(message)});}
      ops() {return this.sent.filter(item => item.op).map(item => item.op);}
    },
    fetch: async (path, options = {}) => {
      requests.push({path, options});
      if (handler === 'down') throw new TypeError('Failed to fetch');
      const [status, data] = await handler(path, options);
      return {ok: status < 400, status, json: async () => {if (data === 'html') throw new SyntaxError('Unexpected token <'); return data;}};
    },
  };
  context.window = {addEventListener: (name, fn) => winEvents[name] = fn,
    JarvisOrb: {create: (canvas, presence) => {presence.subscribe(state => orbCalls.push(state)); return {ok: true};}}};
  if (desktop) context.window.__TAURI_INTERNALS__ = desktop;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('frontend/assistant/presence.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('frontend/assistant/connection.js', 'utf8'), context);
  context.window.JarvisConnection = context.window.JarvisConnection || context.JarvisConnection;
  if (voice) vm.runInContext(fs.readFileSync('frontend/assistant/voice.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('frontend/assistant/assistant.js', 'utf8'), context);
  await flush();
  const run = async () => { const due = timers.filter(timer => !timer.cancelled && !timer.done); due.forEach(timer => {timer.done = true; timer.fn();}); await flush(); };
  return {elements, storage, requests, timers, docEvents, winEvents, document, location, run, orbCalls, track, sockets};
}
const listing = [200, {model: 'qwen2.5-coder:7b', conversations: [{id: 'c'.repeat(32), title: 't'}]}];
const REQUEST = 'aaaaaaaabbbbccccddddeeeeeeeeeeee';

test('in the desktop app each presence state goes to the app; a failing bridge is ignored; a browser sends nothing', async () => {
  const sent = [];
  const desktop = {invoke: async (command, args) => {sent.push([command, args.state]); throw new Error('app closed');}};
  const h = await harness({desktop, hash: '#connect=' + 'k'.repeat(43), handler: async path =>
    path === '/api/connect' ? [200, {token: 't'.repeat(43)}] : listing});
  assert.deepEqual(sent, [['presence', 'offline'], ['presence', 'connecting'], ['presence', 'idle']]);
  assert.equal(h.elements.presenceText.textContent, '연결됨 · qwen2.5-coder:7b');
  const plain = await harness({hash: '#connect=' + 'k'.repeat(43), handler: async path =>
    path === '/api/connect' ? [200, {token: 't'.repeat(43)}] : listing});
  assert.equal(plain.elements.presenceText.textContent, '연결됨 · qwen2.5-coder:7b');
});

test('connection key and #connect protocol match the HUD', () => {
  const hud = fs.readFileSync('frontend/hud.js', 'utf8'), own = fs.readFileSync('frontend/assistant/connection.js', 'utf8');
  const key = /SAVED_CONNECTION='([^']+)'/.exec(hud)[1];
  assert.match(own, new RegExp(`const KEY = '${key}';`));
  for (const pattern of ['#connect=([A-Za-z0-9_-]{43})', "'/api/connect'", '[A-Za-z0-9_-]{43}'])
    assert.ok(hud.includes(pattern) && own.includes(pattern), pattern);
});

test('one-time code is removed from the address bar, exchanged and kept for this tab', async () => {
  const token = 't'.repeat(43);
  const h = await harness({hash: '#connect=' + 'k'.repeat(43), handler: async path =>
    path === '/api/connect' ? [200, {token}] : listing});
  assert.equal(h.location.hash, ''); assert.equal(h.location.replaced, '/assistant');
  assert.equal(JSON.parse(h.requests[0].options.body).code, 'k'.repeat(43));
  assert.equal(h.storage.get('jarvis-hud-connection'), token);
  assert.equal(h.requests[1].options.headers.Authorization, 'Bearer ' + token);
  assert.equal(h.elements.presenceText.textContent, '연결됨 · qwen2.5-coder:7b');
  assert.equal(h.elements.connectLink.hidden, true);
  assert.equal(h.elements.send.disabled, false);
});

test('without a connection the page says so in one line and links to the developer screen', async () => {
  const h = await harness({handler: async () => assert.fail('no request without a token')});
  assert.equal(h.elements.caption.textContent, '연결이 필요해요.');
  assert.equal(h.elements.connectLink.hidden, false);
  assert.equal(h.elements.send.disabled, true); assert.equal(h.elements.input.disabled, true);
});

test('a rejected saved token is forgotten', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async () => [401, {error: 'connection_required'}]});
  assert.equal(h.storage.has('jarvis-hud-connection'), false);
  assert.equal(h.elements.connectLink.hidden, false);
});

test('a question goes to the chat API and the reply shows with a one-line note source', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) => {
    if (options.method === 'POST') return [200, {turns: [{id: REQUEST, status: 'completed', assistant: '11월 첫째 주예요.', sources: [source(), source({title: '메모', line_start: 4, line_end: 4, cited: false})]}]}];
    return listing;
  }});
  h.elements.input.value = '내 노트에서 회의 찾아줘';
  h.elements.composer.events.submit({preventDefault() {}});
  await flush();
  const post = h.requests.find(request => request.options.method === 'POST');
  assert.equal(post.path, '/api/chat/conversations/' + 'c'.repeat(32) + '/messages');
  assert.deepEqual(JSON.parse(post.options.body), {request_id: REQUEST, message: '내 노트에서 회의 찾아줘', mode: 'auto'});
  assert.equal(h.elements.reply.textContent, '11월 첫째 주예요.');
  assert.equal(h.elements.sources.textContent, '출처: 프로젝트 회의록 줄 1-13');
  assert.equal(h.elements.sources.hidden, false);
});

test('uncited notes are labelled, and no sources means no line', async () => {
  let sources = [source({cited: false})];
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: '네', sources}]}] : listing});
  h.elements.composer.events.submit({preventDefault() {}}); h.elements.input.value = '질문';
  h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.sources.textContent, '검색된 노트: 프로젝트 회의록 줄 1-13 (답변 인용 확인 안 됨)');
  sources = [];
  h.elements.input.value = '또 질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.sources.hidden, true);
});

test('the draft streams while pending, polling pauses while hidden and stops when the reply ends', async () => {
  const answer = deferred();
  let draft = '초안';
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) => {
    if (options.method === 'POST' && path.endsWith('/messages')) return answer.promise;
    if (path === '/api/chat/conversations/' + 'c'.repeat(32)) return [200, {turns: [{id: REQUEST, status: 'pending', draft}]}];
    return listing;
  }});
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.send.disabled, true);
  await h.run();
  assert.equal(h.elements.reply.textContent, '초안'); assert.ok(h.elements.reply.classList.contains('draft'));
  const polls = () => h.requests.filter(request => request.path === '/api/chat/conversations/' + 'c'.repeat(32)).length;
  const before = polls();
  h.document.hidden = true; await h.run();
  assert.equal(polls(), before, 'no poll while hidden');
  draft = '초안 더'; h.document.hidden = false; h.docEvents.visibilitychange(); await flush();
  assert.equal(polls(), before + 1, 'resumes when visible');
  assert.equal(h.elements.reply.textContent, '초안 더');
  answer.resolve([200, {turns: [{id: REQUEST, status: 'completed', assistant: '최종 답변', sources: []}]}]); await flush();
  assert.equal(h.elements.reply.textContent, '최종 답변'); assert.ok(!h.elements.reply.classList.contains('draft'));
  const after = polls(); await h.run(); await h.run();
  assert.equal(polls(), after, 'polling stopped after the reply');
  assert.equal(h.elements.send.disabled, false);
});

test('task progress shows as the caption while thinking, never as reply text and never spoken', async () => {
  const answer = deferred();
  const progress = {kind: 'web', phase: 'page', line: '페이지 여는 중: python.org', pages_read: 1, page_limit: 10};
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) => {
    if (options.method === 'POST' && path.endsWith('/messages')) return answer.promise;
    if (path === '/api/chat/conversations/' + 'c'.repeat(32)) return [200, {turns: [{id: REQUEST, status: 'pending', draft: '', progress}]}];
    return listing;
  }});
  h.elements.input.value = '파이썬 최신 버전 찾아줘'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  await h.run();
  assert.equal(h.elements.caption.textContent, '페이지 여는 중: python.org');
  assert.equal(h.elements.reply.hidden, true, 'no reply text while only progress exists');
  assert.equal(h.orbCalls.at(-1), 'thinking', 'the orb keeps thinking: progress is not speech');
  answer.resolve([200, {turns: [{id: REQUEST, status: 'completed', assistant: '3.14.1 입니다.', sources: []}]}]); await flush();
  assert.equal(h.elements.reply.textContent, '3.14.1 입니다.');
});

test('a failed turn is reported without a fake answer and pagehide stops polling', async () => {
  const answer = deferred();
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? answer.promise : path.endsWith('c'.repeat(32)) ? [200, {turns: []}] : listing});
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  h.winEvents.pagehide(); const count = h.requests.length; await h.run();
  assert.equal(h.requests.length, count, 'no poll after pagehide');
  const g = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? [200, {turns: [{id: REQUEST, status: 'failed', assistant: ''}]}] : listing});
  g.elements.input.value = '질문'; g.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(g.elements.caption.textContent, '답변을 완료하지 못했어요. 자동으로 다시 실행하지 않아요.');
  assert.equal(g.elements.reply.hidden, true);
});

test('releasing Space only says voice input is not ready; only the chat warm-up is sent', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async () => listing});
  const posts = path => h.requests.filter(request => request.options.method === 'POST' && (request.path === '/api/chat/warm') === !!path).length;
  h.docEvents.keydown({code: 'Space', key: ' ', repeat: false, preventDefault() {}});
  h.docEvents.keyup({code: 'Space'}); await flush();
  assert.equal(posts(), 0);
  assert.equal(posts('warm'), 1);                         // only the chat model warm-up (ADR 0001)
  assert.match(h.elements.caption.textContent, /마이크를 쓸 수 없어요|음성 입력은 준비 중이에요/);
  assert.ok(!fs.readFileSync('frontend/assistant/assistant.js', 'utf8').includes('SAMPLES'));
});

test('with a working mic, releasing Space says voice input is coming and sends only the chat warm-up', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async () => listing, mic: true});
  h.docEvents.keydown({code: 'Space', key: ' ', repeat: false, preventDefault() {}}); await flush();
  assert.equal(h.elements.caption.textContent, '듣고 있어요');
  h.docEvents.keyup({code: 'Space'}); await flush();
  assert.equal(h.elements.caption.textContent, '음성 입력은 준비 중이에요. 입력창에 적어 주세요.');
  assert.deepEqual(h.requests.filter(request => request.options.method === 'POST').map(request => request.path), ['/api/chat/warm']);
});

const space = h => ({code: 'Space', key: ' ', repeat: false, prevented: false, preventDefault() {this.prevented = true;}});

test('Space on a focused button, link or field keeps its own meaning and does not start the mic', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async () => listing, mic: true});
  for (const focused of [h.elements.send, h.elements.mic, h.elements.connectLink, h.elements.input]) {
    h.document.activeElement = focused;
    const event = space(h);
    h.docEvents.keydown(event); await flush();
    assert.equal(event.prevented, false);
    assert.notEqual(h.elements.mic.attributes['aria-pressed'], 'true');
  }
  h.document.activeElement = h.document.body;
  const event = space(h); h.docEvents.keydown(event); await flush();
  assert.equal(event.prevented, true);
  assert.equal(h.elements.mic.attributes['aria-pressed'], 'true');
});

test('a question is put back in the input when the send ends in a disconnect (401)', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? [401, {error: 'connection_required'}] : listing});
  h.elements.input.value = '긴 질문을 다시 쓰고 싶지 않아요'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.input.value, '긴 질문을 다시 쓰고 싶지 않아요');
  assert.equal(h.elements.caption.textContent, '연결이 필요해요.');
  assert.equal(h.storage.has('jarvis-hud-connection'), false);
});

test('a failed audio setup after the mic was granted stops the mic', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async () => listing, mic: true, audioFails: true});
  h.docEvents.keydown(space(h)); await flush();
  assert.equal(h.track.stopped, true);
  assert.equal(h.elements.caption.textContent, '마이크를 쓸 수 없어요. 입력창에 적어 주세요.');
});

test('a non-JSON error page is reported in plain words', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? [502, 'html'] : listing});
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.caption.textContent, '자비스 서버 응답을 읽지 못했어요. 잠시 뒤 다시 시도해 주세요.');
  assert.equal(h.elements.input.value, '질문');
});

test('the orb follows the presence states: connect, think, speak, then rest', async () => {
  const answer = deferred();
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? answer.promise
      : path.endsWith('c'.repeat(32)) ? [200, {turns: [{id: REQUEST, status: 'pending', draft: '안녕하세요'}]}] : listing});
  assert.deepEqual(h.orbCalls, ['offline', 'connecting', 'idle']);
  assert.equal(h.elements.presenceDot.dataset.state, 'online');
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.orbCalls.at(-1), 'thinking'); assert.equal(h.elements.caption.dataset.mode, 'thinking');
  await h.run();
  assert.equal(h.orbCalls.at(-1), 'speaking');
  answer.resolve([200, {turns: [{id: REQUEST, status: 'completed', assistant: '안녕하세요, 자비스예요.', sources: []}]}]); await flush();
  const rest = h.timers.find(timer => !timer.done && !timer.cancelled);
  assert.ok(rest.ms >= 900 && rest.ms <= 4000, 'speaking lasts about as long as the text: ' + rest.ms);
  await h.run();
  assert.equal(h.orbCalls.at(-1), 'idle');
});

test('a failed reply dims the orb, and the next question starts thinking again', async () => {
  let status = 'failed';
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? [200, {turns: [{id: REQUEST, status, assistant: status === 'failed' ? '' : '네'}]}] : listing});
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.orbCalls.at(-1), 'error'); assert.equal(h.elements.caption.dataset.mode, 'error');
  status = 'completed';
  h.elements.input.value = '다시'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.deepEqual(h.orbCalls.slice(-2), ['thinking', 'speaking']);
});

test('the status line follows the real connection: offline, unreachable server, then back', async () => {
  const offline = await harness({handler: async () => assert.fail('no request without a token')});
  assert.equal(offline.elements.presenceText.textContent, '연결 전');
  assert.equal(offline.elements.presenceDot.dataset.state, 'offline');
  const html = fs.readFileSync('frontend/assistant/index.html', 'utf8');
  assert.match(html, /id="presenceDot"[^>]*data-state="offline"/, 'no online glow before the script runs');

  let down = false;
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) => {
    if (down) throw new TypeError('Failed to fetch');
    return options.method === 'POST' ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: '네', sources: []}]}] : listing;
  }});
  assert.equal(h.elements.presenceText.textContent, '연결됨 · qwen2.5-coder:7b');
  down = true;
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.presenceText.textContent, '연결 확인 필요');
  assert.equal(h.elements.presenceDot.dataset.state, 'warn');
  assert.equal(h.elements.caption.textContent, '자비스 서버에 연결하지 못했어요. 서버가 켜져 있는지 확인해 주세요.');
  assert.equal(h.elements.input.value, '질문');
  down = false;
  h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.elements.presenceText.textContent, '연결됨 · qwen2.5-coder:7b');
  assert.equal(h.elements.presenceDot.dataset.state, 'online');
});

test('the mic needs a connection and its failure dims the orb', async () => {
  const offline = await harness({handler: async () => assert.fail('no request'), mic: true});
  offline.docEvents.keydown(space(offline)); await flush();
  assert.notEqual(offline.elements.mic.attributes['aria-pressed'], 'true');
  const h = await harness({stored: 's'.repeat(43), handler: async () => listing, mic: false});
  h.docEvents.keydown(space(h)); await flush();
  assert.equal(h.orbCalls.at(-1), 'error');
});

test('a question asked while the last answer is still speaking shows thinking, and the old timer cannot end the new answer', async () => {
  const second = deferred();
  let posts = 0;
  const h = await harness({stored: 's'.repeat(43), handler: async (path, options) => {
    if (options.method === 'POST' && path.endsWith('/messages'))
      return ++posts === 1 ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: '첫 번째 답이에요. 조금 길게 말해요.', sources: []}]}] : second.promise;
    return path.endsWith('c'.repeat(32)) ? [200, {turns: []}] : listing;
  }});
  h.elements.input.value = '첫 질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.orbCalls.at(-1), 'speaking');
  const oldTimer = h.timers.find(timer => !timer.done && !timer.cancelled && timer.ms >= 900);
  h.elements.input.value = '두 번째 질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.orbCalls.at(-1), 'thinking');
  assert.equal(h.elements.caption.textContent, '생각하고 있어요');
  assert.equal(h.elements.caption.dataset.mode, 'thinking');
  second.resolve([200, {turns: [{id: REQUEST, status: 'completed', assistant: '두 번째 답이에요. 이것도 길게 말해요.', sources: []}]}]); await flush();
  assert.equal(h.orbCalls.at(-1), 'speaking');
  oldTimer.fn();  // even if it fired late, it belongs to the first answer
  assert.equal(h.orbCalls.at(-1), 'speaking', 'the first answer timer does not end the second answer');
});

test('the mic interrupts speaking and starts listening', async () => {
  const h = await harness({stored: 's'.repeat(43), mic: true, handler: async (path, options) =>
    options.method === 'POST' && path.endsWith('/messages') ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: '답이에요.', sources: []}]}] : listing});
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.orbCalls.at(-1), 'speaking');
  h.elements.mic.events.click(); await flush();
  assert.equal(h.orbCalls.at(-1), 'listening');
  assert.equal(h.elements.mic.attributes['aria-pressed'], 'true');
  assert.equal(h.elements.caption.textContent, '듣고 있어요');
});


// --- local voice service (hud --voice) ---
async function voiced(handler) {
  const h = await harness({stored: 's'.repeat(43), voice: true, handler});
  const socket = h.sockets[0];
  socket.open();
  socket.receive({type: 'hello', state: 'ready', error: null, device: 'cuda', model: 'large-v3-turbo', voice: 'cosyvoice3'});
  return {...h, socket};
}
const talkKey = h => ({
  down() {h.docEvents.keydown({code: 'Space', key: ' ', repeat: false, preventDefault() {}});},
  up() {h.docEvents.keyup({code: 'Space'});},
});

test('the voice socket sends the token first and the hint says how to talk', async () => {
  const h = await voiced(async () => listing);
  assert.equal(h.socket.url, 'ws://127.0.0.1:8787/voice');
  assert.deepEqual(h.socket.sent[0], {token: 's'.repeat(43)});
  assert.match(h.elements.hint.textContent, /Space를 누른 채 말하고/);
  assert.ok(fs.readFileSync('frontend/assistant/index.html', 'utf8').includes('/assets/assistant/voice.js'));
});

test('push-to-talk: Space opens the native mic, release transcribes, the transcript is asked and the reply spoken', async () => {
  const h = await voiced(async (path, options) => options.method === 'POST'
    ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: '오후 3시에 회의가 있어요.', sources: []}]}] : listing);
  talkKey(h).down(); await flush();
  assert.deepEqual(h.socket.ops(), ['listen']);
  assert.equal(h.elements.caption.textContent, '듣고 있어요');
  h.socket.receive({type: 'state', state: 'listening'});
  talkKey(h).up(); await flush();
  assert.deepEqual(h.socket.ops(), ['listen', 'stop']);
  assert.equal(h.elements.caption.textContent, '알아듣고 있어요');
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0, 'nothing asked before the transcript');
  h.socket.receive({type: 'state', state: 'transcribing'});
  h.socket.receive({type: 'state', state: 'ready'});
  h.socket.receive({type: 'transcript', text: '오늘 일정 알려줘'}); await flush();
  const post = h.requests.find(request => request.options.method === 'POST');
  assert.equal(JSON.parse(post.options.body).message, '오늘 일정 알려줘');
  const speak = h.socket.sent.filter(item => item.op === 'speak');
  assert.deepEqual(speak, [{op: 'speak', text: '오후 3시에 회의가 있어요.', final: true}]);
  assert.equal(h.socket.ops()[2], 'hush', 'an old reply stops before the new one is spoken');
  assert.equal(h.orbCalls.at(-1), 'speaking');
  h.socket.receive({type: 'state', state: 'speaking'});        // the service says so as soon as text is queued
  await h.run();
  assert.equal(h.orbCalls.at(-1), 'speaking', 'the fallback timer leaves a reply that is being spoken alone');
  h.socket.receive({type: 'state', state: 'ready'}); await flush();
  assert.equal(h.orbCalls.at(-1), 'idle', 'the service says when speaking ended');
});

test('a pause ends listening by itself and an empty transcript asks nothing', async () => {
  const h = await voiced(async () => listing);
  talkKey(h).down(); await flush();
  h.socket.receive({type: 'state', state: 'listening'});
  h.socket.receive({type: 'state', state: 'transcribing'}); await flush();
  assert.equal(h.elements.caption.textContent, '알아듣고 있어요');
  talkKey(h).up(); await flush();
  assert.deepEqual(h.socket.ops(), ['listen'], 'releasing after the auto stop sends nothing more');
  h.socket.receive({type: 'state', state: 'ready'});
  h.socket.receive({type: 'transcript', text: ''}); await flush();
  assert.equal(h.requests.filter(request => request.options.method === 'POST').length, 0);
  assert.match(h.elements.caption.textContent, /말소리를 듣지 못했어요/);
});

test('Esc cancels listening without a transcript and stops a spoken reply', async () => {
  const h = await voiced(async (path, options) => options.method === 'POST'
    ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: '긴 답변입니다.', sources: []}]}] : listing);
  talkKey(h).down(); await flush();
  h.docEvents.keydown({key: 'Escape', code: 'Escape'}); await flush();
  assert.deepEqual(h.socket.ops(), ['listen', 'cancel', 'cancel']);
  assert.equal(h.orbCalls.at(-1), 'idle');
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  h.socket.receive({type: 'state', state: 'speaking'});
  h.docEvents.keydown({key: 'Escape', code: 'Escape'}); await flush();
  assert.equal(h.socket.ops().at(-1), 'cancel');
  assert.equal(h.orbCalls.at(-1), 'idle');
});

test('long replies go out in chunks small enough for the socket, without control characters', async () => {
  const text = '가'.repeat(600) + '\u0001끝.';
  const h = await voiced(async (path, options) => options.method === 'POST'
    ? [200, {turns: [{id: REQUEST, status: 'completed', assistant: text, sources: []}]}] : listing);
  h.elements.input.value = '질문'; h.elements.composer.events.submit({preventDefault() {}}); await flush();
  const speak = h.socket.sent.filter(item => item.op === 'speak');
  assert.ok(speak.length >= 3 && speak.every(item => item.text.length <= 250));
  assert.deepEqual(speak.map(item => item.final), [...speak.slice(1).map(() => false), true]);
  assert.equal(speak.map(item => item.text).join(''), '가'.repeat(600) + ' 끝.');
  assert.ok(speak.every(item => Buffer.byteLength(JSON.stringify(item)) < 1024));
});

test('without the voice service the page keeps the browser-only behaviour', async () => {
  const h = await harness({stored: 's'.repeat(43), voice: true, handler: async () => listing});
  h.sockets[0].close();                                    // hud started without --voice: no /voice route
  talkKey(h).down(); talkKey(h).up(); await flush();
  assert.deepEqual(h.sockets[0].sent, []);
  assert.match(h.elements.caption.textContent, /마이크를 쓸 수 없어요|음성 입력은 준비 중이에요/);
  assert.match(h.elements.hint.textContent, /음성 입력은 준비 중이에요/);
});

test('a voice service that is still loading keeps typing as the way in', async () => {
  const h = await harness({stored: 's'.repeat(43), voice: true, handler: async () => listing});
  h.sockets[0].open();
  h.sockets[0].receive({type: 'hello', state: 'loading', error: null, device: null, model: null, voice: 'cosyvoice3'});
  assert.match(h.elements.hint.textContent, /음성 엔진을 준비하고 있어요/);
  talkKey(h).down(); await flush();
  assert.deepEqual(h.sockets[0].ops(), []);
});

test('typing asks the server to load the chat model at most once a minute; a failed warm-up changes nothing', async () => {
  const h = await harness({stored: 's'.repeat(43), handler: async path =>
    path === '/api/chat/warm' ? [503, {error: 'down'}] : listing});
  h.elements.input.events.input();
  h.elements.input.events.input();
  await flush();
  const warms = h.requests.filter(request => request.path === '/api/chat/warm');
  assert.equal(warms.length, 1);
  assert.equal(warms[0].options.method, 'POST'); assert.equal(warms[0].options.body, '{}');
  assert.equal(h.elements.send.disabled, false);
  const off = await harness({handler: async () => assert.fail('no request without a token')});
  off.elements.input.events.input();
  await flush();
});
