'use strict';
// Local voice service client (hud --voice). One WebSocket at /voice; the HUD token is the first message.
// The service only moves audio: it opens the mic, sends back a transcript, and speaks the reply text it is given.
// When the HUD runs without --voice the socket closes at once and the page keeps its browser-only behaviour.
(() => {
  const CHUNK = 250;  // characters per speak message; the server reads at most 1 KB per message

  // handlers: {hello(status), state(state, error), transcript(text), frame(level, bins), error(code), closed()}
  function connect(token, handlers) {
    let socket = null, open = false, closed = false;
    try {
      socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/voice');
    } catch (_) {
      closed = true;
      queueMicrotask(() => handlers.closed?.());
    }
    const send = message => {
      if (!open) return false;
      try {socket.send(JSON.stringify(message)); return true;} catch (_) {return false;}
    };
    if (socket) {
      socket.onopen = () => {open = true; socket.send(JSON.stringify({token}));};
      socket.onmessage = event => {
        let message;
        try {message = JSON.parse(event.data);} catch (_) {return;}
        if (message.type === 'hello') handlers.hello?.(message);
        else if (message.type === 'state') handlers.state?.(message.state, message.error);
        else if (message.type === 'transcript' && typeof message.text === 'string') handlers.transcript?.(message.text);
        else if (message.type === 'frame' && Array.isArray(message.bins)) handlers.frame?.(message.level, message.bins);
        else if (message.type === 'error') handlers.error?.(String(message.code || ''));
      };
      socket.onclose = () => {
        open = false;
        if (!closed) {closed = true; handlers.closed?.();}
      };
    }

    // Reply text in order; final ends the reply. Control characters would only grow the message, so they go.
    function speak(text, final) {
      const clean = String(text || '').replace(/[\u0000-\u0009\u000b-\u001f\u007f]/g, ' ');
      let rest = clean;
      while (rest.length > CHUNK) {
        if (!send({op: 'speak', text: rest.slice(0, CHUNK), final: false})) return false;
        rest = rest.slice(CHUNK);
      }
      return send({op: 'speak', text: rest, final: Boolean(final)});
    }
    return {
      listen: () => send({op: 'listen'}),
      stop: () => send({op: 'stop'}),
      cancel: () => send({op: 'cancel'}),
      hush: () => send({op: 'hush'}),
      speak,
      close() {closed = true; open = false; try {socket?.close();} catch (_) { /* already closed */ }},
    };
  }

  window.JarvisVoice = {connect};
})();
