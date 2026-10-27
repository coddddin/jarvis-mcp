'use strict';
// The assistant's presence in one place: a small state machine plus the live input the orb draws.
// States: offline, connecting, idle, listening, thinking, speaking, error. The page sends events; the orb, the caption
// and the status line only subscribe. Input: setLevel(0..1) and setSpectrum(bins 0..1) come from the browser mic
// today and later from a native voice service that streams level/spectrum frames.
(() => {
  const BINS = 32;
  const LIVE_MS = 250;  // a live frame older than this no longer drives the orb
  const CONNECTED = ['idle', 'listening', 'thinking', 'speaking', 'error'];
  const TRANSITIONS = {
    connecting: {offline: 'connecting', error: 'connecting'},
    connected: {connecting: 'idle', error: 'idle'},
    disconnected: null,  // from any state
    // Speaking can be cut off by the mic or a new question; leaving speaking drops the queued pseudo speech.
    listen: {idle: 'listening', error: 'listening', speaking: 'listening'},
    listenEnd: {listening: 'idle'},
    ask: {idle: 'thinking', listening: 'thinking', error: 'thinking', speaking: 'thinking'},
    speak: {thinking: 'speaking', speaking: 'speaking'},
    spoken: {speaking: 'idle'},
    fail: {connecting: 'error', idle: 'error', listening: 'error', thinking: 'error', speaking: 'error', error: 'error'},
  };
  const SYLLABLE_S = 1 / 14;  // pseudo speech plays about 14 characters a second
  const QUEUE_MAX = 80;
  const clamp = value => (value > 1 ? 1 : value > 0 ? value : 0);

  function create({random = Math.random, now = () => performance.now()} = {}) {
    let state = 'offline', model = '', linked = false;
    const listeners = new Set();
    const live = {level: 0, spectrum: new Float32Array(BINS), at: -Infinity};
    const out = {level: 0, spectrum: new Float32Array(BINS)};
    const shape = new Float32Array(BINS);
    let queue = '', energy = 0, nextAt = 0, last = null;

    const info = () => ({model, linked, connected: CONNECTED.includes(state)});
    const notify = previous => listeners.forEach(listener => listener(state, previous, info()));

    // Returns true when the event applies in the current state. detail.model is kept from 'connected'.
    function dispatch(event, detail = {}) {
      if (!(event in TRANSITIONS)) return false;
      const rule = TRANSITIONS[event];
      const next = rule === null ? 'offline' : rule[state];
      if (!next) return false;
      if (event === 'connected') {model = String(detail.model || ''); linked = true;}
      if (next === 'offline') {model = ''; linked = false;}
      const previous = state;
      state = next;
      if (state !== 'speaking') queue = '';
      if (next !== previous) notify(previous);
      return true;
    }
    // Whether the server answered the last request; the status line follows it without changing the state.
    function setLinked(value) {
      if (linked === Boolean(value) || state === 'offline') return;
      linked = Boolean(value);
      if (state !== 'connecting') notify(state);  // the line says '연결 중' until the connection settles
    }
    function subscribe(listener) {
      listeners.add(listener);
      listener(state, null, info());
      return () => listeners.delete(listener);
    }
    function setLevel(value) {
      live.level = clamp(Number(value) || 0);
      live.at = now();
    }
    function setSpectrum(bins) {
      const count = Math.min(BINS, bins?.length || 0);
      for (let i = 0; i < BINS; i++) live.spectrum[i] = i < count ? clamp(Number(bins[i]) || 0) : 0;
      live.at = now();
    }
    // Pseudo speech while the reply has no audio: each streamed character is one syllable-like burst, spaces and
    // punctuation are pauses. Returns about how long the queued text takes, in ms.
    // REPLACE: when the voice service plays real audio, feed its setLevel/setSpectrum frames and drop this.
    function speechText(text) {
      queue = (queue + String(text || '')).slice(-QUEUE_MAX);
      return Math.round(queue.length * SYLLABLE_S * 1000);
    }
    // Called by the renderer once per frame.
    function sample() {
      const t = now() / 1000, dt = last === null ? 0 : Math.min(.1, t - last);
      last = t;
      if (t * 1000 - live.at < LIVE_MS) {
        out.level = live.level;
        out.spectrum.set(live.spectrum);
        return out;
      }
      if (queue && t >= nextAt) {
        const char = queue[0];
        queue = queue.slice(1);
        const pause = /\s/.test(char) ? 1.4 : /[.,!?。、…]/.test(char) ? 3 : 0;
        if (!pause) {
          energy = Math.max(energy, .55 + .45 * random());
          for (let i = 0; i < BINS; i++) shape[i] = random();
          for (let i = 1; i < BINS - 1; i++) shape[i] = (shape[i - 1] + 2 * shape[i] + shape[i + 1]) / 4;  // no lone spikes
        }
        nextAt = t + SYLLABLE_S * (pause || .7 + .6 * random());
      }
      energy *= Math.pow(.004, dt);  // a burst fades in about a tenth of a second
      for (let i = 0; i < BINS; i++) {
        const voice = Math.exp(-Math.pow(i / BINS - .2, 2) / .05);  // most energy low-mid, like speech
        out.spectrum[i] = clamp(energy * (.45 + .55 * voice) * (.3 + .7 * shape[i]));
      }
      out.level = energy;
      return out;
    }
    return {
      get state() {return state;}, get linked() {return linked;}, get model() {return model;},
      dispatch, setLinked, subscribe, setLevel, setSpectrum, speechText, sample, BINS,
    };
  }

  window.JarvisPresence = {create};
})();
