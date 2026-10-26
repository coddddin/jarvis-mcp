'use strict';
// The assistant's presence in one place: a small state machine plus the live input the orb draws.
// States: offline, connecting, idle, listening, thinking, speaking, error. The page sends events; the orb and the
// status line only subscribe. Input: setLevel(0..1) and setSpectrum(bins 0..1) from the mic today, and later from a
// native voice service that sends level/spectrum frames.
(() => {
  const BINS = 32;
  const LIVE_MS = 250;  // a live frame older than this no longer drives the orb
  const TRANSITIONS = {
    connecting: {offline: 'connecting'},
    connected: {offline: 'idle', connecting: 'idle'},
    disconnected: null,  // from any state
    listen: {idle: 'listening', error: 'listening'},
    listenEnd: {listening: 'idle'},
    ask: {idle: 'thinking', listening: 'thinking', error: 'thinking'},
    draft: {thinking: 'speaking', speaking: 'speaking'},
    reply: {thinking: 'speaking', speaking: 'speaking'},
    spoken: {speaking: 'idle'},
    fail: {idle: 'error', listening: 'error', thinking: 'error', speaking: 'error'},
  };
  const clamp = value => (value > 1 ? 1 : value > 0 ? value : 0);

  function create({random = Math.random, now = () => performance.now()} = {}) {
    let state = 'offline';
    const listeners = new Set();
    const live = {level: 0, spectrum: new Float32Array(BINS), at: -Infinity};
    const out = {level: 0, spectrum: new Float32Array(BINS)};
    const jitter = new Float32Array(BINS).map(() => random());
    let queued = 0, energy = 0, last = null;

    function dispatch(event) {
      if (!(event in TRANSITIONS)) return false;
      const rule = TRANSITIONS[event];
      const next = rule === null ? 'offline' : rule[state];
      if (!next) return false;
      if (next !== state) {
        const previous = state;
        state = next;
        if (state !== 'speaking') queued = 0;
        listeners.forEach(listener => listener(state, previous));
      }
      return true;
    }
    function subscribe(listener) {
      listeners.add(listener);
      listener(state, null);
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
    // Pseudo speech while there is no audio of the reply: each streamed character is a small burst, played back at
    // about 14 characters a second. Replace with setLevel/setSpectrum frames from the voice service when it exists.
    function speechText(text) {
      queued = Math.min(60, queued + String(text || '').replace(/\s+/g, '').length);
      return Math.round(queued / 14 * 1000);
    }
    // Called by the renderer once per frame.
    function sample() {
      const t = now(), dt = last === null ? 0 : Math.min(.1, (t - last) / 1000);
      last = t;
      if (t - live.at < LIVE_MS) {
        out.level = live.level;
        out.spectrum.set(live.spectrum);
        return out;
      }
      const kicks = Math.min(queued, dt * 14);
      queued -= kicks;
      energy = clamp(energy * Math.pow(.02, dt) + kicks * (.45 + .3 * random()));
      for (let i = 0; i < BINS; i++) {
        jitter[i] += (random() - jitter[i]) * Math.min(1, dt * 9);
        const voice = Math.exp(-Math.pow(i / BINS - .22, 2) / .04);  // most energy low-mid, like speech
        out.spectrum[i] = clamp(energy * (.35 + .65 * voice) * (.55 + .45 * jitter[i]));
      }
      out.level = energy;
      return out;
    }
    return {get state() {return state;}, dispatch, subscribe, setLevel, setSpectrum, speechText, sample, BINS};
  }

  window.JarvisPresence = {create};
})();
