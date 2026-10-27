'use strict';
// Mini ring: the /assistant orb, driven by the state the app forwards from the main window (no server connection).
// A click opens the main window; a drag moves this window.
(() => {
  const STATES = ['offline', 'connecting', 'idle', 'listening', 'thinking', 'speaking', 'error'];
  const BINS = 32, quiet = {level: 0, spectrum: new Float32Array(BINS)};
  const listeners = new Set();
  let state = 'offline';
  const presence = {
    BINS, get state() {return state;}, sample: () => quiet,
    subscribe(listener) {listeners.add(listener); listener(state, null, {}); return () => listeners.delete(listener);},
  };
  window.jarvisMini = {
    set(next) {
      if (!STATES.includes(next) || next === state) return;
      const previous = state;
      state = next;
      listeners.forEach(listener => listener(state, previous, {}));
    },
  };
  const canvas = document.getElementById('orb');
  window.JarvisOrb.create(canvas, presence);
  const invoke = command => window.__TAURI_INTERNALS__.invoke(command).catch(() => {});
  let down = null;
  canvas.addEventListener('pointerdown', event => {if (event.button === 0) down = {x: event.screenX, y: event.screenY};});
  canvas.addEventListener('pointermove', event => {
    if (down && Math.hypot(event.screenX - down.x, event.screenY - down.y) > 4) {down = null; invoke('drag');}
  });
  canvas.addEventListener('pointerup', () => {if (down) invoke('expand'); down = null;});
})();
