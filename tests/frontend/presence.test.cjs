const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const context = {window: {}, Float32Array, Math, String, Number};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('frontend/assistant/presence.js', 'utf8'), context);
  return context.window.JarvisPresence;
}
function make() {
  const clock = {t: 0};
  let seed = 0;
  const presence = load().create({now: () => clock.t, random: () => (seed = (seed * 9301 + 49297) % 233280) / 233280});
  const seen = [];
  presence.subscribe((state, previous, info) => seen.push([state, info.linked]));
  return {presence, clock, seen};
}
const run = (p, steps) => steps.map(event => (p.dispatch(event), p.state));

test('a normal turn walks through the states and invalid events change nothing', () => {
  const {presence} = make();
  assert.equal(presence.state, 'offline');
  assert.equal(presence.dispatch('ask'), false, 'no question before a connection');
  assert.equal(presence.dispatch('nonsense'), false);
  assert.deepEqual(run(presence, ['connecting', 'connected', 'listen', 'ask', 'speak', 'speak', 'spoken']),
    ['connecting', 'idle', 'listening', 'thinking', 'speaking', 'speaking', 'idle']);
  assert.equal(presence.dispatch('spoken'), false);
});

test('errors dim until the next action, and a disconnect works from any state', () => {
  const {presence} = make();
  run(presence, ['connecting', 'connected', 'ask', 'fail']);
  assert.equal(presence.state, 'error');
  assert.equal(presence.dispatch('listen'), true);
  run(presence, ['ask', 'speak']);
  assert.equal(presence.dispatch('disconnected'), true);
  assert.equal(presence.state, 'offline');
  assert.equal(presence.linked, false);
});

test('the connection flag and model reach subscribers without a state change', () => {
  const {presence, seen} = make();
  presence.dispatch('connecting');
  presence.dispatch('connected', {model: 'qwen'});
  assert.equal(presence.model, 'qwen');
  presence.setLinked(false);
  presence.setLinked(false);  // no duplicate notice
  presence.setLinked(true);
  assert.deepEqual(seen, [['offline', false], ['connecting', false], ['idle', true], ['idle', false], ['idle', true]]);
});

test('live frames drive the output, then expire', () => {
  const {presence, clock} = make();
  const bins = Array.from({length: 40}, (_, i) => i / 39);
  presence.setLevel(1.7);
  presence.setSpectrum(bins);
  let out = presence.sample();
  assert.equal(out.level, 1, 'clamped');
  assert.equal(out.spectrum.length, presence.BINS);
  assert.ok(out.spectrum[31] > out.spectrum[0]);
  clock.t = 300;
  out = presence.sample();
  assert.equal(out.level, 0, 'old live frames no longer count');
});

test('pseudo speech comes in bursts, pauses at spaces and stops when speaking ends', () => {
  const {presence, clock} = make();
  run(presence, ['connecting', 'connected', 'ask', 'speak']);
  const ms = presence.speechText('안녕 하세요.');
  assert.equal(ms, Math.round(7 / 14 * 1000));
  const levels = [];
  for (let i = 0; i < 90; i++) {clock.t += 1000 / 60; levels.push(presence.sample().level);}
  const peaks = levels.filter((value, i) => i > 0 && i < levels.length - 1 && value > levels[i - 1] && value >= levels[i + 1] && value > .3);
  assert.ok(peaks.length >= 4 && peaks.length <= 6, 'about one burst per spoken character: ' + peaks.length);
  assert.ok(Math.min(...levels.slice(10, 60)) < .2, 'the level falls between bursts');
  presence.speechText('긴 문장이 이어집니다');
  presence.dispatch('spoken');
  for (let i = 0; i < 60; i++) {clock.t += 1000 / 60; presence.sample();}
  assert.ok(presence.sample().level < .01, 'nothing is queued after speaking ends');
});

test('the queue keeps only the latest text', () => {
  const {presence} = make();
  run(presence, ['connecting', 'connected', 'ask', 'speak']);
  assert.equal(presence.speechText('가'.repeat(500)), Math.round(80 / 14 * 1000));
});

test('a new question or the mic during speaking drops the queued pseudo speech', () => {
  const {presence, clock} = make();
  run(presence, ['connecting', 'connected', 'ask', 'speak']);
  presence.speechText('아주 긴 답이 계속 나오는 중이에요');
  assert.equal(presence.dispatch('ask'), true);
  assert.equal(presence.state, 'thinking');
  for (let i = 0; i < 30; i++) {clock.t += 1000 / 60; presence.sample();}
  assert.ok(presence.sample().level < .01, 'no leftover bursts while thinking');
  run(presence, ['speak']);
  assert.equal(presence.dispatch('listen'), true);
  assert.equal(presence.state, 'listening');
});
