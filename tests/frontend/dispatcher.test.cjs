const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.events={}; this.hidden=false; this.disabled=false; this.textContent=''; this.style={}; this.attributes={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener(name, fn) { this.events[name]=fn; }
  setAttribute(name, value) { this.attributes[name]=value; }
}
const flatten = node => [node.textContent || '', ...node.children.map(flatten)].join(' ');
const find = (node, test) => test(node) ? node : node.children.map(child => find(child, test)).find(Boolean);
const deferred = () => { let resolve; const promise=new Promise(r=>resolve=r); return {promise,resolve}; };
const NOW = Date.now() / 1000;

function snapshot(changes = {}) {
  return {config_enabled:true, paused:false, paused_at:null, effective_enabled:true, interval_minutes:10, daily_runs:10,
    service:{running:true, pid:4242, started:NOW - 600},
    employees:{codex:{runs:3, total_input_tokens:12000, total_output_tokens:3400, exhausted_until:NOW + 3600},
      'claude-code':{runs:1, total_input_tokens:500, total_output_tokens:90, exhausted_until:null}},
    recent:Array.from({length:12}, (_, i) => ({id:i, employee:i % 2 ? 'codex' : '<b>cursor</b>', status:['ok','limit','running'][i % 3],
      started:NOW - 100 * i, ended:i % 3 === 2 ? null : NOW - 100 * i + 120, exit_code:i % 3 === 2 ? null : 0,
      input_tokens:i % 3 === 2 ? null : 100, output_tokens:i % 3 === 2 ? null : 20})),
    read_at:NOW, ...changes};
}

function harness({confirm = () => true} = {}) {
  const elements = {workspace:new Element('section'), tasksTab:new Element('button')};
  const windowEvents = {}, documentEvents = {}, requests = [], intervals = [];
  let handler = () => ({status:200, body:snapshot()});
  const document = {hidden:false, getElementById:id => elements[id], createElement:tag => new Element(tag),
    addEventListener:(name, fn) => documentEvents[name]=fn};
  const context = {document, window:{addEventListener:(name, fn) => windowEvents[name]=fn, confirm},
    setInterval:fn => intervals.push(fn), Intl, Date, Number, Math, Object, Array, String, Error, Boolean,
    fetch:async (path, options) => { requests.push({path, options}); const r = await handler(path, options);
      return {ok:r.status < 400, status:r.status, json:async () => r.body}; }};
  vm.runInNewContext(fs.readFileSync('frontend/dispatcher.js', 'utf8'), context);
  const panel = elements.workspace.children[0];
  const button = text => find(panel, n => n.tag === 'button' && n.textContent === text);
  const connect = async token => { windowEvents['jarvis-connection']({detail:{token}}); await new Promise(r => setImmediate(r)); };
  const settle = () => new Promise(r => setImmediate(r));
  return {elements, panel, requests, intervals, document, documentEvents, button, connect, settle, setHandler:fn => handler=fn};
}

test('panel stays hidden until connected, then loads with the connection token', async () => {
  const h = harness();
  assert.equal(h.panel.id, 'dispatcherPanel');
  assert.equal(h.panel.hidden, true);
  assert.equal(h.requests.length, 0);
  await h.connect('test-token');
  assert.equal(h.panel.hidden, false);
  assert.equal(h.requests[0].path, '/api/dispatcher');
  assert.equal(h.requests[0].options.headers.Authorization, 'Bearer test-token');
  assert.equal(h.requests[0].options.credentials, 'omit');
  const text = flatten(h.panel);
  assert.match(text, /켜짐/);
  assert.match(text, /서비스 실행 중 \(PID 4242\)/);
  assert.match(text, /10분마다/);
  assert.match(text, /codex · 실행 3회 · 한도 대기/);
  assert.match(text, /입력 12,000 · 출력 3,400 토큰/);
});

test('recent runs are capped at 10 and names are plain text', async () => {
  const h = harness();
  await h.connect('t');
  const recent = find(h.panel, n => n.attributes['aria-label'] === '최근 실행');
  assert.equal(recent.children.length, 10);
  assert.match(flatten(recent), /<b>cursor<\/b>/);  // shown literally via textContent, never parsed
  assert.match(flatten(recent), /한도 도달/);
  assert.match(flatten(recent), /실행 중/);
});

test('pause posts immediately; resume asks first and a cancel sends nothing', async () => {
  let answer = false;
  const h = harness({confirm: () => answer});
  await h.connect('t');
  h.setHandler(() => ({status:200, body:snapshot({paused:true, effective_enabled:false, paused_at:NOW})}));
  await h.button('일시정지').events.click();
  assert.equal(h.requests.at(-1).path, '/api/dispatcher/pause');
  assert.equal(h.requests.at(-1).options.method, 'POST');
  assert.match(flatten(h.panel), /일시정지했어요/);
  const resume = h.button('재개');
  assert.ok(resume);
  const before = h.requests.length;
  await resume.events.click();
  assert.equal(h.requests.length, before);
  answer = true;
  h.setHandler(() => ({status:200, body:snapshot()}));
  await resume.events.click();
  assert.equal(h.requests.at(-1).path, '/api/dispatcher/resume');
  assert.equal(resume.textContent, '일시정지');
});

test('config-disabled and paused states are told apart', async () => {
  const h = harness();
  h.setHandler(() => ({status:200, body:snapshot({config_enabled:false, effective_enabled:false})}));
  await h.connect('t');
  assert.match(flatten(h.panel), /설정에서 꺼짐/);
  assert.match(flatten(h.panel), /config\/dispatcher.json/);
});

test('errors show a Korean notice; malformed responses are rejected', async () => {
  for (const [status, pattern] of [[401, /연결이 만료/], [403, /바꿀 수 없어요/], [503, /읽을 수 없어요/]]) {
    const h = harness();
    h.setHandler(() => ({status, body:{error:'x'}}));
    await h.connect('t');
    assert.match(flatten(h.panel), pattern);
  }
  const h = harness();
  h.setHandler(() => ({status:200, body:{effective_enabled:'yes'}}));
  await h.connect('t');
  assert.match(flatten(h.panel), /형식을 확인할 수 없어요/);
});

test('polls only while visible and connected', async () => {
  const h = harness();
  assert.equal(h.intervals.length, 1);
  await h.intervals[0]();
  assert.equal(h.requests.length, 0);  // not connected
  await h.connect('t');
  const after = h.requests.length;
  h.document.hidden = true;
  await h.intervals[0]();
  h.document.hidden = false;
  h.elements.workspace.hidden = true;  // chat tab active
  await h.intervals[0]();
  assert.equal(h.requests.length, after);
  h.elements.workspace.hidden = false;
  await h.intervals[0]();
  assert.equal(h.requests.length, after + 1);
});

test('a late response from an old connection is dropped and disconnect hides the panel', async () => {
  const h = harness(), slow = deferred();
  h.setHandler(() => slow.promise);
  await h.connect('old');
  h.setHandler(() => ({status:200, body:snapshot({interval_minutes:30})}));
  await h.connect('new');
  slow.resolve({status:200, body:snapshot({interval_minutes:99})});
  await h.settle();
  assert.match(flatten(h.panel), /30분마다/);
  assert.doesNotMatch(flatten(h.panel), /99분마다/);
  await h.connect(null);
  assert.equal(h.panel.hidden, true);
});
