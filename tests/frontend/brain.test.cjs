const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.events = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.textContent = ''; this.className = '';
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  contains(other) { return other === this || this.children.some(child => child.contains(other)); }
  focus() { this.focused = true; }
}
const all = (value, out = []) => { out.push(value); value.children.forEach(child => all(child, out)); return out; };
const text = value => all(value).map(item => item.textContent).join(' ');
const flush = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };

const LISTING = {
  selection: {provider_id: 'ollama', model_id: 'qwen3.5:9b', label: 'qwen3.5:9b', external: false},
  default: {provider_id: 'ollama', model_id: 'qwen3.5:9b', label: 'qwen3.5:9b', external: false}, scope: ['chat', 'voice'],
  providers: [
    {provider_id: 'ollama', label: '로컬 Ollama', external: false, models: [
      {model_id: 'qwen3.5:9b', label: 'qwen3.5:9b', available: true, reason: null},
      {model_id: 'nomic-embed:v1', label: 'nomic-embed:v1', available: false, reason: '대화용 모델이 아닙니다.'}]},
    {provider_id: 'claude-cli', label: 'Claude (구독)', external: true, models: [
      {model_id: 'sonnet', label: 'sonnet', available: true, reason: null}]}],
};

function harness(respond) {
  const requests = [], changes = [], listeners = {};
  const context = {
    document: {createElement: tag => new Element(tag), addEventListener: (name, fn) => { listeners[name] = fn; }},
    window: {},
    fetch: async (path, options) => {
      const body = options.body ? JSON.parse(options.body) : undefined;
      requests.push({path, method: options.method, body, auth: options.headers.Authorization});
      const [status, data] = respond(path, body);
      return {status, json: async () => data};
    },
  };
  vm.runInNewContext(fs.readFileSync('frontend/brain.js', 'utf8'), context);
  const button = new Element('button'), panel = new Element();
  const brain = context.window.JarvisBrain.create({button, panel, token: () => 't', onChange: value => changes.push(value)});
  const choices = () => all(panel).filter(item => item.className.startsWith('model-choice'));
  const byText = label => all(panel).find(item => item.tagName === 'BUTTON' && item.textContent === label);
  return {context, brain, button, panel, requests, changes, choices, byText, listeners};
}

test('the button names the current model and lists models with reasons', async () => {
  const h = harness(() => [200, LISTING]);
  await h.brain.refresh();
  assert.equal(h.button.textContent, 'qwen3.5:9b · 로컬');
  await h.button.events.click(); await flush();
  assert.equal(h.panel.hidden, false);
  assert.equal(h.button.attributes['aria-expanded'], 'true');
  assert.deepEqual(h.choices().map(item => [item.textContent, item.disabled]),
    [['qwen3.5:9b', true], ['nomic-embed:v1', true], ['sonnet', false]]);
  assert.match(text(h.panel), /대화용 모델이 아닙니다/);
  assert.match(text(h.panel), /채팅과 음성 답변에만/);
  assert.equal(h.requests[0].auth, 'Bearer t');
});

test('the first external switch shows what is sent and is confirmed explicitly', async () => {
  const sends = ['지금 대화(질문과 앞선 답변)'], never = ['메일', '로컬 문서'];
  const h = harness((path, body) => path === '/api/brain' ? [200, LISTING]
    : body.confirm_external ? [200, {selection: {provider_id: 'claude-cli', model_id: 'sonnet', label: 'Claude (구독) · sonnet', external: true}}]
    : [409, {error: 'confirm_required', provider: 'Claude (구독)', sends, never_sends: never}]);
  await h.button.events.click(); await flush();
  await h.byText('sonnet').events.click(); await flush();
  assert.equal(h.requests.at(-1).body.confirm_external, undefined);
  assert.match(text(h.panel), /처음 쓰는 외부 모델: Claude \(구독\)/);
  assert.match(text(h.panel), /로컬 문서/);
  assert.match(text(h.panel), /자동 전환하지 않아요/);
  assert.equal(h.button.textContent, 'qwen3.5:9b · 로컬');            // nothing changed yet
  await h.byText('확인하고 사용').events.click(); await flush();
  assert.equal(h.requests.at(-1).body.confirm_external, true);
  assert.equal(h.button.textContent, 'Claude (구독) · sonnet · 외부');
  assert.equal(h.changes.at(-1).external, true);
  assert.match(text(h.panel), /다음 질문부터 답하는 모델: Claude \(구독\) · sonnet/);
});

test('Escape or a click outside closes the panel; a click inside keeps it open', async () => {
  const h = harness(() => [200, LISTING]);
  await h.button.events.click(); await flush();
  h.listeners.pointerdown({target: h.byText('sonnet')});
  assert.equal(h.panel.hidden, false);
  h.listeners.pointerdown({target: new Element()});
  assert.equal(h.panel.hidden, true);
  assert.equal(h.button.attributes['aria-expanded'], 'false');
  await h.button.events.click(); await flush();
  h.listeners.keydown({key: 'Escape'});
  assert.equal(h.panel.hidden, true);
  assert.equal(h.button.focused, true);
});

test('cancelling the confirmation sends nothing more', async () => {
  const h = harness((path) => path === '/api/brain' ? [200, LISTING]
    : [409, {error: 'confirm_required', provider: 'Claude (구독)', sends: [], never_sends: []}]);
  await h.button.events.click(); await flush();
  await h.byText('sonnet').events.click(); await flush();
  const count = h.requests.length;
  await h.byText('취소').events.click(); await flush();
  assert.equal(h.requests.length, count);
  assert.ok(h.byText('sonnet'));
});

test('a failed switch keeps the old model and shows the reason', async () => {
  const listing = {...LISTING, providers: [{...LISTING.providers[0], models: [
    LISTING.providers[0].models[0], {model_id: 'gemma3:4b', label: 'gemma3:4b', available: true, reason: null}]}]};
  const h = harness(path => path === '/api/brain' ? [200, listing]
    : [400, {error: 'model_unavailable', reason: 'Ollama가 응답하지 않습니다.'}]);
  await h.button.events.click(); await flush();
  await h.byText('gemma3:4b').events.click(); await flush();
  assert.equal(h.button.textContent, 'qwen3.5:9b · 로컬');
  assert.match(text(h.panel), /바꾸지 못했어요.*Ollama가 응답하지 않습니다/);
});

test('answeredBy names the model and a local takeover reason', () => {
  const h = harness(() => [200, LISTING]);
  const {answeredBy} = h.context.window.JarvisBrain;
  assert.equal(answeredBy({}), '');
  assert.equal(answeredBy({model: {label: 'qwen3.5:9b', external: false}}), '답한 모델: qwen3.5:9b (로컬)');
  assert.equal(answeredBy({model: {label: 'qwen3.5:9b', external: false, local_reason: '메일 내용은 외부 모델에 보내지 않아서 로컬 모델이 답했습니다.'}}),
    '답한 모델: qwen3.5:9b (로컬) · 메일 내용은 외부 모델에 보내지 않아서 로컬 모델이 답했습니다.');
});

test('reset forgets the selection after a disconnect', async () => {
  const h = harness(() => [200, LISTING]);
  await h.brain.refresh();
  h.brain.reset();
  assert.equal(h.button.textContent, '모델 확인 필요');
  assert.equal(h.panel.hidden, true);
  assert.equal(h.brain.state, null);
});

// --- brain P3: API providers with a key --------------------------------------------------------------------------
const KEYED = (connected, models = []) => ({...LISTING, providers: [...LISTING.providers,
  {provider_id: 'openai-api', label: 'GPT (API)', external: true, needs_key: true, connected, custom_model: true, models}]});
const forms = (h, name) => all(h.panel).filter(item => item.className === name);

test('a provider without a key shows a password field and the key leaves the page once', async () => {
  let connected = false;
  const h = harness((path, body) => {
    if (path === '/api/brain/key') { connected = true; return [200, {provider_id: body.provider_id, connected: true}]; }
    return [200, KEYED(connected, connected ? [{model_id: 'gpt-x', label: 'gpt-x', available: true, reason: null}] : [])];
  });
  await h.button.events.click(); await flush();
  const [form] = forms(h, 'model-key');
  const input = all(form).find(item => item.tagName === 'INPUT');
  assert.equal(input.type, 'password');
  assert.equal(input.autocomplete, 'off');
  assert.match(text(h.panel), /자격 증명 관리자/);
  input.value = '  sk-secret-value-123  ';
  form.events.submit({preventDefault() {}}); await flush();
  assert.equal(input.value, '');                                                // cleared before the request
  const sent = h.requests.find(item => item.path === '/api/brain/key');
  assert.deepEqual(sent.body, {provider_id: 'openai-api', key: 'sk-secret-value-123'});
  assert.match(text(h.panel), /키를 연결했어요/);
  assert.ok(h.byText('gpt-x'));
  assert.doesNotMatch(text(h.panel), /sk-secret/);
});

test('a rejected key shows the reason and an empty field sends nothing', async () => {
  const h = harness(path => path === '/api/brain/key' ? [400, {error: 'key_rejected', reason: 'OpenAI 키가 거부되었습니다.'}]
    : [200, KEYED(false)]);
  await h.button.events.click(); await flush();
  let [form] = forms(h, 'model-key');
  form.events.submit({preventDefault() {}}); await flush();
  assert.equal(h.requests.filter(item => item.path === '/api/brain/key').length, 0);
  assert.match(text(h.panel), /키를 입력해 주세요/);
  [form] = forms(h, 'model-key');
  all(form).find(item => item.tagName === 'INPUT').value = 'sk-wrong-key-000';
  form.events.submit({preventDefault() {}}); await flush();
  assert.match(text(h.panel), /처리하지 못했어요.*키가 거부되었습니다/);
});

test('a connected provider takes a typed model id and can delete its key', async () => {
  const h = harness((path, body) => path === '/api/brain' ? [200, KEYED(true)]
    : path === '/api/brain/key/delete' ? [200, {provider_id: body.provider_id, connected: false}]
    : [409, {error: 'confirm_required', provider: 'GPT (API)', sends: [], never_sends: []}]);
  await h.button.events.click(); await flush();
  assert.equal(forms(h, 'model-key').length, 0);
  const [form] = forms(h, 'model-custom');
  all(form).find(item => item.tagName === 'INPUT').value = ' gpt-new-model ';
  form.events.submit({preventDefault() {}}); await flush();
  assert.deepEqual(h.requests.at(-1).body, {provider_id: 'openai-api', model_id: 'gpt-new-model'});
  assert.match(text(h.panel), /처음 쓰는 외부 모델: GPT \(API\)/);
  await h.byText('취소').events.click(); await flush();
  await h.byText('키 삭제').events.click(); await flush();
  assert.deepEqual(h.requests.find(item => item.path === '/api/brain/key/delete').body, {provider_id: 'openai-api'});
  assert.match(text(h.panel), /키를 지웠어요/);
});
