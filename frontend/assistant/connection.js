'use strict';
// Connection for the assistant view. Same protocol and tab storage as the HUD (#31): `hud --open` puts a one-time
// code in the URL fragment, which is removed from the address bar at once and traded at /api/connect for the token.
// The token lives in sessionStorage under the HUD's key, so moving between /assistant and the developer screen (/) in
// the same tab keeps the connection; closing the tab forgets it.
(() => {
  const KEY = 'jarvis-hud-connection';
  const TOKEN = /^[A-Za-z0-9_-]{43}$/;
  const saved = () => {try {return sessionStorage.getItem(KEY);} catch (_) {return null;}};
  const remember = token => {try {sessionStorage.setItem(KEY, token);} catch (_) { /* storage blocked: this load only */ }};
  let current = null;
  const listeners = new Set();
  const publish = value => {
    const next = typeof value === 'string' && TOKEN.test(value) ? value : null;
    if (next === current) return;
    current = next;
    for (const listener of listeners) listener(current);
  };
  const subscribe = listener => {listeners.add(listener); listener(current); return () => listeners.delete(listener);};
  const forget = () => {try {sessionStorage.removeItem(KEY);} catch (_) { /* nothing stored */ } publish(null);};

  async function exchange(code) {
    try {
      const response = await fetch('/api/connect', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code}), cache: 'no-store', credentials: 'omit'});
      const data = response.ok ? await response.json() : null;
      return typeof data?.token === 'string' && TOKEN.test(data.token) ? data.token : null;
    } catch (_) {
      return null;
    }
  }

  // Returns {token} or {token: null, codeFailed} when there is no usable connection.
  async function start() {
    const match = /^#connect=([A-Za-z0-9_-]{43})$/.exec(location.hash);
    if (location.hash.startsWith('#connect=')) history.replaceState(null, '', location.pathname);
    if (match) {
      const token = await exchange(match[1]);
      if (token) {remember(token); return {token};}
    }
    const stored = saved();
    if (stored && TOKEN.test(stored)) return {token: stored};
    if (stored) forget();
    return {token: null, codeFailed: Boolean(match)};
  }

  window.JarvisConnection = {start, forget, publish, subscribe};
})();
