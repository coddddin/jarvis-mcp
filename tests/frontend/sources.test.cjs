const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.events = {}; this.value = ''; this.hidden = false; this.classList = {toggle() {}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute() {}
  querySelectorAll() { return []; }
  focus() {}
}
const text = value => typeof value === 'string' ? value : [value.textContent || '', ...value.children.map(text)].join(' ');
const all = (value, out = []) => { if (typeof value !== 'string') { out.push(value); value.children.forEach(child => all(child, out)); } return out; };
const source = (changes = {}) => ({note_id: 'meeting', title: '프로젝트 회의록', path: 'JARVIS-Vault/meeting.md',
  line_start: 1, line_end: 13, source_sha256: 'a'.repeat(64), score: 0.17, cited: true, ...changes});

function load(extra = {}) {
  const window = {addEventListener() {}};
  const context = {document: {createElement: tag => new Element(tag), getElementById: () => new Element(), querySelectorAll: () => []}, window, ...extra};
  vm.runInNewContext(fs.readFileSync('frontend/sources.js', 'utf8'), context);
  return window.JarvisSources;
}

test('no sources renders nothing', () => {
  const sources = load();
  assert.equal(sources.render({sources: []}), null);
  assert.equal(sources.render({}), null);
  assert.equal(sources.render({sources: [{title: 1}]}), null);
});

test('cited notes show title and line range, opened', () => {
  const box = load().render({sources: [source()]});
  assert.equal(box.tagName, 'DETAILS');
  assert.equal(box.open, true);
  const shown = text(box);
  assert.match(shown, /참고한 노트 · 1개/);
  assert.match(shown, /프로젝트 회의록/);
  assert.match(shown, /줄 1-13/);
  assert.equal(all(box).find(node => node.tagName === 'LI').title, 'JARVIS-Vault/meeting.md');
});

test('uncited notes are labelled as not confirmed and stay closed', () => {
  const box = load().render({sources: [source({cited: false, line_start: 4, line_end: 4})]});
  assert.equal(box.open, false);
  assert.match(text(box), /검색된 노트 · 1개 \(답변 인용 확인 안 됨\)/);
  assert.match(text(box), /줄 4(?!-)/);
});

test('note text is rendered as text, never markup', () => {
  const box = load().render({sources: [source({title: '<img src=x onerror=alert(1)>'})]});
  const strong = all(box).find(node => node.tagName === 'STRONG');
  assert.equal(strong.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(strong.children.length, 0);
});

test('chat.js appends source cards after the answer and clears them for a new conversation', async () => {
  const ids = [...fs.readFileSync('frontend/index.html', 'utf8').matchAll(/id="([^"]+)"/g)].map(m => m[1]);
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const events = {};
  let conversation = {id: 'c1', turns: [{id: 't1', user: '내 노트에서 회의 찾아줘', assistant: '11월 첫째 주예요.', status: 'completed', tools: [], sources: [source()]}]};
  const window = {addEventListener: (name, fn) => events[name] = fn};
  const context = {document: {getElementById: id => elements[id], createElement: tag => new Element(tag), querySelectorAll: () => []},
    window, crypto: {randomUUID: () => 'request-1'}, setTimeout: () => 0, clearTimeout() {},
    fetch: async path => ({ok: true, json: async () => path.endsWith('/conversations') ? {model: 'local', conversations: [{id: 'c1', title: 't'}]} : conversation})};
  vm.runInNewContext(fs.readFileSync('frontend/sources.js', 'utf8'), context);
  vm.runInNewContext(fs.readFileSync('frontend/chat.js', 'utf8'), context);
  await events['jarvis-connection']({detail: {token: 'token'}});
  const shown = text(elements.chatMessages);
  assert.ok(shown.indexOf('11월 첫째 주예요.') < shown.indexOf('참고한 노트'));
  assert.match(shown, /줄 1-13/);
  conversation = {id: 'c2', turns: []};
  await elements.newChat.events.click();
  assert.doesNotMatch(text(elements.chatMessages), /참고한 노트/);
  await events['jarvis-connection']({detail: {token: null}});
  assert.equal(elements.chatMessages.children.length, 0);
});
