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

test('process watch findings show as plain-text warnings; none keeps the section hidden', async () => {
  const h = harness();
  await h.connect('t');
  const list = find(h.panel, n => n.attributes['aria-label'] === '감시 경고');
  assert.equal(list.hidden, true);
  h.setHandler(() => ({status:200, body:snapshot({watch:{checked_at:NOW, findings:[
    {kind:'owner_dead', ref:'run:7', since:NOW - 60, detail:{employee:'<i>codex</i>', pid:12}},
    {kind:'overdue', ref:'run:8', since:NOW - 4000, detail:{employee:'cursor', elapsed_seconds:3950, limit_seconds:1800}},
    {kind:'service_down', ref:'service', since:null, detail:{}},
    {kind:'unknown_kind', ref:'x', since:null, detail:{}}]}})}));
  await h.button('새로고침').events.click();
  await h.settle();
  assert.equal(list.hidden, false);
  assert.equal(list.children.length, 3);  // the unknown kind is ignored
  const text = flatten(list);
  assert.match(text, /run:7 \(<i>codex<\/i>\): 실행 중으로 남아 있지만 프로세스가 없어요/);
  assert.match(text, /제한 시간 30분을 넘겼어요/);
  assert.match(text, /서비스가 꺼져 있어요/);
});

test('an unavailable watch is reported without hiding the rest', async () => {
  const h = harness();
  h.setHandler(() => ({status:200, body:snapshot({watch:{findings:[], checked_at:null, error:'watch_unavailable'}})}));
  await h.connect('t');
  const list = find(h.panel, n => n.attributes['aria-label'] === '감시 경고');
  assert.equal(list.hidden, false);
  assert.match(flatten(list), /감시 결과를 읽지 못했어요/);
  assert.match(flatten(h.panel), /켜짐/);
});

test('last observed usage shows per employee with stale marking; absent keeps it hidden', async () => {
  const h = harness();
  await h.connect('t');
  const list = find(h.panel, n => n.attributes['aria-label'] === '마지막 사용량');
  assert.equal(list.hidden, true);
  h.setHandler(() => ({status:200, body:snapshot({usage:{
    codex:{five_hour:81, weekly:44, sampled_at:NOW - 60, source:'codex_rollout_observed', fresh:true},
    '<b>x</b>':{five_hour:23.5, weekly:null, sampled_at:NOW - 7200, source:'claude_statusline', fresh:false}}})}));
  await h.button('새로고침').events.click();
  await h.settle();
  assert.equal(list.hidden, false);
  const text = flatten(list);
  assert.match(text, /codex · 5시간 81% · 주간 44%/);
  assert.match(text, /<b>x<\/b> · 5시간 24%/);
  assert.match(text, /오래됨/);
  assert.match(text, /판단에는 쓰지 않음/);
});

test('todays alerts show as plain text; none keeps the section hidden', async () => {
  const h = harness();
  await h.connect('t');
  const list = find(h.panel, n => n.attributes['aria-label'] === '오늘 알림');
  assert.equal(list.hidden, true);
  h.setHandler(() => ({status:200, body:snapshot({alerts:{today:[
    {text:'멈춘 담당: <b>job</b>(codex)', sent:NOW - 60}, {text:42, sent:NOW}, null,
    {text:'x'.repeat(400), sent:'later'}]}})}));
  await h.button('새로고침').events.click();
  await h.settle();
  assert.equal(list.hidden, false);
  assert.equal(list.children.length, 2);  // non-text items are ignored
  const text = flatten(list);
  assert.match(text, /멈춘 담당: <b>job<\/b>\(codex\)/);  // textContent only, never markup
  assert.ok(!text.includes('x'.repeat(301)));
});

test('an unreadable alert record is reported without hiding the rest', async () => {
  const h = harness();
  h.setHandler(() => ({status:200, body:snapshot({alerts:{today:[], error:'alerts_unavailable'}})}));
  await h.connect('t');
  const list = find(h.panel, n => n.attributes['aria-label'] === '오늘 알림');
  assert.equal(list.hidden, false);
  assert.match(flatten(list), /알림 기록을 읽지 못했어요/);
  assert.match(flatten(h.panel), /켜짐/);
});

test('service status is told apart: not registered, stopped, identity and unknown are not all "stopped"', async () => {
  const h = harness();
  const list = () => find(h.panel, n => n.attributes['aria-label'] === '감시 경고');
  const show = async (service, changes = {}) => {
    h.setHandler(() => ({status:200, body:snapshot({service, watch:{findings:[], checked_at:NOW}, ...changes})}));
    if (h.requests.length) await h.button('새로고침').events.click(); else await h.connect('t');
    await h.settle();
    return flatten(h.panel);
  };
  const missing = {running:false, pid:null, started:null, status:'not_registered', reason:'missing_record', source:'observed'};
  let text = await show(missing);
  assert.match(text, /설정은 켜졌지만 서비스 미등록/);
  assert.doesNotMatch(text, /서비스 멈춤/);
  assert.equal(list().hidden, false);
  assert.match(flatten(list()), /서비스가 등록되지 않았어요/);
  text = await show(missing, {paused:true, effective_enabled:false});   // paused: no warning line
  assert.match(text, /서비스 미등록/);
  assert.doesNotMatch(text, /설정은 켜졌지만/);
  assert.equal(list().hidden, true);
  assert.match(await show({running:false, pid:null, started:100, status:'stopped', reason:'process_missing', source:'observed'}), /서비스 멈춤/);
  assert.match(await show({running:false, pid:null, started:100, status:'identity_mismatch', reason:'pid_reused', source:'observed'}),
    /서비스 신원 미확인/);
  text = await show({running:false, pid:null, started:null, status:'unknown', reason:'invalid_record', source:'observed'});
  assert.match(text, /서비스 상태 미확인/);
  assert.doesNotMatch(text, /서비스 멈춤/);
  assert.match(await show({running:false, pid:null, started:null}), /서비스 멈춤/);          // old server: boolean only
  assert.match(await show({running:false, pid:null, started:null, status:'<b>odd</b>'}), /서비스 상태 미확인/);
});
