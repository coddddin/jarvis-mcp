const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.events={}; this.hidden=false; this.disabled=false; this.textContent=''; this.style={}; this.attributes={}; this.className=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener(name, fn) { this.events[name]=fn; }
  setAttribute(name, value) { this.attributes[name]=value; }
}
const flatten = node => [node.textContent || '', ...node.children.map(flatten)].join(' ');
const find = (node, test) => test(node) ? node : node.children.map(child => find(child, test)).find(Boolean);
const all = (node, test) => [...(test(node) ? [node] : []), ...node.children.flatMap(child => all(child, test))];

function briefing(changes = {}) {
  return {connected:true, date:'2026-10-25', generated_at:'2026-10-25T09:00:00+09:00', partial:false,
    calendar:{status:'ok', has_more:false, events:[
      {summary:'<b>팀 회의</b>', start:'2026-10-25T10:00:00+09:00', end:'2026-10-25T11:00:00+09:00'},
      {summary:'휴가', start:'2026-10-25', end:'2026-10-26'}]},
    gmail:{status:'ok', has_more:true, unread_estimate:42, messages:[
      {from:'Alice <alice@example.com>', subject:'<img src=x onerror=alert(1)>', date:'Sat, 25 Oct'}]}, ...changes};
}

// Default is the older page with only #workspace (fallback); {common:true} adds index.html's
// #briefingHost beside the hidden-by-tab #chatPanel and #workspace.
function harness({common = false} = {}) {
  const elements = {workspace:new Element('section')};
  const root = new Element('main');
  if (common) {
    Object.assign(elements, {workspaceTabs:new Element('nav'), briefingHost:new Element('div'), chatPanel:new Element('section')});
    root.append(elements.workspaceTabs, elements.briefingHost, elements.chatPanel, elements.workspace);
  } else root.append(elements.workspace);
  const windowEvents = {}, requests = [], intervals = [];
  let handler = () => ({status:200, body:briefing()});
  const document = {hidden:false, getElementById:id => elements[id], createElement:tag => new Element(tag),
    addEventListener:() => {}};
  const context = {document, window:{addEventListener:(name, fn) => windowEvents[name]=fn},
    setInterval:fn => intervals.push(fn), Array, String, Error, Number, Object,
    fetch:async (path, options) => { requests.push({path, options}); const r = await handler(path, options);
      return {ok:r.status < 400, status:r.status, json:async () => r.body}; }};
  vm.runInNewContext(fs.readFileSync('frontend/briefing.js', 'utf8'), context);
  const panel = (elements.briefingHost || elements.workspace).children[0];
  const settle = () => new Promise(r => setImmediate(r));
  const connect = async token => { windowEvents['jarvis-connection']({detail:{token}}); await settle(); await settle(); };
  const button = text => find(panel, n => n.tag === 'button' && n.textContent === text);
  const list = label => find(panel, n => n.attributes['aria-label'] === label && n.tag === 'ul');
  // Like hud.js tab buttons: exactly one of chat/workspace is hidden; briefing.js is not told.
  const selectTab = name => { if (elements.chatPanel) elements.chatPanel.hidden = name !== 'chat'; elements.workspace.hidden = name !== 'tasks'; };
  const shown = (node = root) => !node.hidden && (node === panel || node.children.some(shown));
  const panels = () => all(root, n => n.id === 'briefingPanel').length;
  return {panel, elements, requests, intervals, connect, settle, button, list, selectTab, shown, panels, setHandler:fn => handler=fn};
}

test('hidden until connected, then one GET with the token and no polling', async () => {
  const h = harness();
  assert.equal(h.panel.id, 'briefingPanel');
  assert.equal(h.panel.hidden, true);
  assert.equal(h.requests.length, 0);
  await h.connect('tok');
  assert.equal(h.panel.hidden, false);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].path, '/api/briefing');
  assert.equal(h.requests[0].options.headers.Authorization, 'Bearer tok');
  assert.equal(h.requests[0].options.credentials, 'omit');
  assert.equal(h.intervals.length, 0);
});

test('calendar and mail render as plain text with times, counts and more-markers', async () => {
  const h = harness();
  await h.connect('t');
  const calendar = flatten(h.list('오늘 일정')), mail = flatten(h.list('안 읽은 메일'));
  assert.match(calendar, /<b>팀 회의<\/b>/);            // literal text, never parsed
  assert.match(calendar, /10:00–11:00/);
  assert.match(calendar, /휴가 +종일/);
  assert.match(mail, /<img src=x onerror=alert\(1\)>/);
  assert.match(mail, /Alice <alice@example.com>/);
  assert.match(mail, /메일이 더 있어요/);
  assert.match(flatten(h.panel), /안 읽은 메일 \(약 42통\)/);
  assert.equal(all(h.panel, n => n.tag === 'a').length, 0);  // no links
});

test('a failing side shows its own error while the other side still renders', async () => {
  const h = harness();
  h.setHandler(() => ({status:200, body:briefing({partial:true,
    gmail:{status:'error', error:'Google API returned HTTP 403; check consent and enabled APIs.'}})}));
  await h.connect('t');
  assert.match(flatten(h.list('안 읽은 메일')), /메일 불러오기 실패: Google API returned HTTP 403/);
  assert.match(flatten(h.list('오늘 일정')), /휴가/);
  assert.match(flatten(h.panel), /일부만/);
});

test('empty days and inboxes say so', async () => {
  const h = harness();
  h.setHandler(() => ({status:200, body:briefing({calendar:{status:'ok', has_more:false, events:[]},
    gmail:{status:'ok', has_more:false, unread_estimate:0, messages:[]}})}));
  await h.connect('t');
  assert.match(flatten(h.list('오늘 일정')), /오늘 일정 없음/);
  assert.match(flatten(h.list('안 읽은 메일')), /안 읽은 메일 없음/);
});

test('not connected and expired connections explain the next step', async () => {
  const h = harness();
  h.setHandler(() => ({status:200, body:{connected:false}}));
  await h.connect('t');
  assert.match(flatten(h.panel), /google_auth_setup\.py/);
  assert.equal(h.list('오늘 일정').hidden, true);
  h.setHandler(() => ({status:200, body:{connected:true, error:'Invalid Google credentials; reconnect using setup helper.'}}));
  await h.button('새로고침').events.click();
  assert.match(flatten(h.panel), /Google 연결을 확인해주세요: Invalid Google credentials/);
});

test('HTTP failures show a fixed message', async () => {
  for (const [status, text] of [[401, /연결이 만료/], [503, /불러올 수 없어요/], [500, /요청이 실패/]]) {
    const h = harness();
    h.setHandler(() => ({status, body:{error:'x'}}));
    await h.connect('t');
    assert.match(flatten(h.panel), text);
  }
});

test('refresh reloads on demand and a disconnect hides the card', async () => {
  const h = harness();
  await h.connect('t');
  await h.button('새로고침').events.click();
  assert.equal(h.requests.length, 2);
  await h.connect(null);
  assert.equal(h.panel.hidden, true);
  assert.equal(h.requests.length, 2);
});

test('pages without #briefingHost still append the one card to #workspace', () => {
  const h = harness();
  assert.equal(h.elements.workspace.children.length, 1);
  assert.equal(h.panels(), 1);
});

test('one card in the common host is visible on both tabs with one load and no requests on tab switches', async () => {
  const h = harness({common:true});
  assert.equal(h.elements.briefingHost.children.length, 1);
  assert.equal(h.elements.workspace.children.length, 0);
  assert.equal(h.elements.chatPanel.children.length, 0);
  assert.equal(h.panels(), 1);
  h.selectTab('chat');
  assert.equal(h.shown(), false);                       // still hidden before a connection
  await h.connect('tok');
  assert.equal(h.requests.length, 1);
  for (const tab of ['chat', 'tasks', 'chat', 'tasks']) {
    h.selectTab(tab); await h.settle();
    assert.equal(h.shown(), true, tab);
    assert.match(flatten(h.list('오늘 일정')), /휴가/);
  }
  await h.connect('tok');                               // same connection again: no reload
  assert.equal(h.requests.length, 1);
  assert.equal(h.panels(), 1);
  assert.equal(h.intervals.length, 0);
});

test('disconnect clears private contents on either tab and a late response cannot restore them', async () => {
  const h = harness({common:true});
  await h.connect('tok');
  h.selectTab('chat');
  assert.match(flatten(h.panel), /팀 회의/);
  await h.connect(null);
  assert.equal(h.shown(), false);
  assert.doesNotMatch(flatten(h.panel), /팀 회의|휴가|alice@example\.com|42통/);

  let release;
  h.setHandler(() => new Promise(r => { release = () => r({status:200, body:briefing()}); }));
  await h.connect('late');
  assert.equal(h.requests.length, 2);
  await h.connect(null);                                // disconnect while the GET is in flight
  release(); await h.settle(); await h.settle();
  h.selectTab('tasks');
  assert.equal(h.shown(), false);
  assert.doesNotMatch(flatten(h.panel), /팀 회의|휴가|alice@example\.com|42통/);
  assert.equal(h.requests.length, 2);
  assert.equal(h.panels(), 1);
});
