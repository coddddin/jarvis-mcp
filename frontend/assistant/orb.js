'use strict';
// JARVIS presence orb: one full-screen WebGL triangle and a fragment shader, driven by JarvisPresence.
// Looks: idle = calm blue ring, listening = sky blue ring that swells with the mic level, thinking = orange ring with
// sweeping arcs and orbiting sparks, speaking = circular spectrum (outward waveform with two lagging outlines),
// error/offline = dimmed. The ring works on (cos, sin) of the angle, never the angle itself, so it has no seam.
// Budget: 60 fps only while something moves, 24 fps at rest, nothing while hidden, one still frame for
// prefers-reduced-motion, device pixel ratio capped at 2. No library; a CSS glow stands in without WebGL.
// Technique notes (analyser bins, attack/release easing, DPR cap) were learned from MIT-licensed
// community orbs on 21st.dev; the shader below is written for this project.
(() => {
  const VERTEX = 'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}';
  const FRAGMENT = `precision highp float;
uniform vec2 uRes;uniform float uTime,uLevel,uListen,uThink,uSpeak,uDim;
uniform vec3 uIon,uArc,uEmber,uSky,uMean;uniform sampler2D uSpec;
float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x),f.y);}
void main(){
  vec2 p=(gl_FragCoord.xy-.5*uRes)/min(uRes.x,uRes.y);
  float r=length(p)+1e-5,L=uLevel;
  vec2 dir=p/r;
  // Spectrum position: 0 at the top to 1 at the bottom, mirrored left and right, so low (loud) bins sit on top.
  float m=acos(clamp(dir.y,-1.,1.))/3.14159265;
  float x=.02+.96*m;
  vec3 sp=(texture2D(uSpec,vec2(x-.015,.5)).rgb+2.*texture2D(uSpec,vec2(x,.5)).rgb+texture2D(uSpec,vec2(x+.015,.5)).rgb)*.25;
  // Waveform push: a little of the overall loudness plus the ripple around it.
  vec3 wave=.25*uMean+1.1*max(sp-.6*uMean,0.);
  float R=.35+.008*sin(uTime*1.1)+.035*L*uListen;
  float wob=R*((noise(dir*1.8+vec2(uTime*.21,-uTime*.17))-.5)*(.05+.16*L*uListen)
    +(noise(dir*3.6+vec2(-uTime*.33,uTime*.12))-.5)*.025)*(1.-.7*uSpeak);
  float d=r-(R+wob+R*.5*wave.r*uSpeak);
  float w=.009+.008*uListen*(.4+L)+.002*uThink;
  vec3 base=mix(mix(uArc,uIon,.25),uSky,uListen);
  base=mix(base,uEmber,uThink);
  base=mix(base,mix(uIon,vec3(1.),.25),uSpeak);
  float line=exp(-d*d/(w*w)),glow=exp(-abs(d)*(26.-10.*L));
  vec3 col=base*(2.3*line+.7*glow*(1.+L))+vec3(.4)*exp(-d*d/(w*w*.18));
  // Listening: an echo ring just outside that grows with the mic level.
  float de=r-R*(1.05+.10*L);
  col+=uSky*uListen*L*.9*exp(-de*de/(w*w*1.6));
  if(uSpeak>.01){
    float d2=r-(R+wob+R*.40*wave.g),d3=r-(R+wob+R*.30*wave.b);
    col+=uSpeak*(uArc*1.1*exp(-d2*d2/(w*w*.9))+uSky*.8*exp(-d3*d3/(w*w*.8)));
  }
  if(uThink>.01){
    vec2 a1=vec2(cos(uTime*2.3),sin(uTime*2.3));
    float sweep=pow(max(dot(dir,a1),0.),10.)+.6*pow(max(-dot(dir,a1),0.),18.);
    col+=uEmber*uThink*sweep*(1.4*line+.6*glow);
    for(int i=0;i<12;i++){
      float fi=float(i);
      float th=uTime*(1.1+.17*fi)*(mod(fi,2.)<1.?1.:-.8)+fi*.524;
      vec2 q=p-R*(1.08+.07*sin(fi*1.7+uTime*.9))*vec2(cos(th),sin(th));
      col+=mix(uEmber,vec3(1.),.3)*uThink*(1.6*exp(-dot(q,q)/.00009)+.35*exp(-dot(q,q)/.0006));
    }
  }
  col+=base*.16*exp(-r*r/(R*R*.35))*(1.+L+.6*uSpeak);
  col+=base*.12*exp(-max(r-R,0.)*8.);
  float g=dot(col,vec3(.3,.59,.11));
  col=mix(col,g*vec3(.75,.85,1.),.7*uDim)*(1.-.62*uDim);
  col*=smoothstep(.5,.46,r);
  col=1.-exp(-col*1.3);
  gl_FragColor=vec4(col,max(col.r,max(col.g,col.b)));
}`;
  // Look per state: listen, think, speak, dim (0..1 each), eased between states.
  const LOOKS = {
    offline: [0, 0, 0, .75], connecting: [0, 0, 0, .45], idle: [0, 0, 0, 0], listening: [1, 0, 0, 0],
    thinking: [0, 1, 0, 0], speaking: [0, 0, 1, 0], error: [0, 0, 0, 1],
  };
  const MOVING = new Set(['listening', 'thinking', 'speaking']);
  const REST_FPS = 24;
  const LAG = [30, 9, 4];  // spectrum layers: live, lagging, slow (per second)

  const hex = value => {
    const n = parseInt(value.replace('#', ''), 16);
    return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
  };
  const approach = (value, target, rate, dt) => value + (target - value) * (1 - Math.exp(-rate * dt));

  function create(canvas, presence) {
    let state = presence.state;
    const fallback = () => {canvas.dataset.fallback = 'true'; return {ok: false};};
    const gl = canvas.getContext('webgl', {premultipliedAlpha: false, antialias: true});
    if (!gl) {presence.subscribe(next => {canvas.dataset.state = next;}); return fallback();}
    const compile = (type, text) => { const s = gl.createShader(type); gl.shaderSource(s, text); gl.compileShader(s); return s; };
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {presence.subscribe(next => {canvas.dataset.state = next;}); return fallback();}
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'a');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    const u = name => gl.getUniformLocation(program, name);
    const loc = {res: u('uRes'), time: u('uTime'), level: u('uLevel'), listen: u('uListen'), think: u('uThink'),
      speak: u('uSpeak'), dim: u('uDim'), mean: u('uMean')};
    const css = getComputedStyle(document.documentElement);
    const color = (name, value) => gl.uniform3fv(u(name), hex(css.getPropertyValue(value[0]).trim() || value[1]));
    color('uIon', ['--ion', '#6fe3ff']); color('uArc', ['--arc', '#3b8cff']);
    color('uEmber', ['--ember', '#ffb454']); color('uSky', ['--sky', '#5cc8ff']);

    // Spectrum texture: 32 x 1 RGB, one channel per layer.
    const bins = presence.BINS, bytes = new Uint8Array(bins * 3), layers = [0, 1, 2].map(() => new Float32Array(bins));
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    for (const [key, value] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, key, value);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, bins, 1, 0, gl.RGB, gl.UNSIGNED_BYTE, bytes);
    gl.uniform1i(u('uSpec'), 0);

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2), box = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(box.width * ratio));
      canvas.height = Math.max(1, Math.round(box.height * ratio));
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    const look = [...LOOKS[state]];
    let level = 0, clock = 3, last = null, lastDraw = -Infinity, frame = 0;

    function draw(dt, still) {
      const sample = presence.sample(), target = LOOKS[state];
      for (let i = 0; i < 4; i++) look[i] = still ? target[i] : approach(look[i], target[i], 5, dt);
      const heard = MOVING.has(state) && !still ? sample.level : 0;
      level = approach(level, heard, heard > level ? 22 : 5, dt);
      const mean = [0, 0, 0];
      for (let k = 0; k < 3; k++) {
        for (let i = 0; i < bins; i++) {
          const value = still || state !== 'speaking' ? 0 : sample.spectrum[i];
          layers[k][i] = approach(layers[k][i], value, LAG[k], dt);
          bytes[i * 3 + k] = Math.round(layers[k][i] * 255);
          mean[k] += layers[k][i] / bins;
        }
      }
      gl.uniform3fv(loc.mean, mean);
      if (!still) clock += dt * (1 + level * 1.5 + look[1] * .6);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, bins, 1, gl.RGB, gl.UNSIGNED_BYTE, bytes);
      gl.uniform2f(loc.res, canvas.width, canvas.height);
      gl.uniform1f(loc.time, clock);
      gl.uniform1f(loc.level, level);
      gl.uniform1f(loc.listen, look[0]); gl.uniform1f(loc.think, look[1]);
      gl.uniform1f(loc.speak, look[2]); gl.uniform1f(loc.dim, look[3]);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    const settled = () => LOOKS[state].every((value, i) => Math.abs(look[i] - value) < .01) && level < .01;
    function tick(now) {
      frame = 0;
      if (document.hidden || reduced.matches) {last = null; return;}
      const busy = MOVING.has(state) || !settled();
      if (now - lastDraw >= 1000 / (busy ? 60 : REST_FPS) - 2) {
        draw(last === null ? 0 : Math.min(.1, (now - last) / 1000), false);
        last = lastDraw = now;
      }
      frame = requestAnimationFrame(tick);
    }
    function wake() {
      if (document.hidden) return;
      if (reduced.matches) {draw(0, true); return;}
      if (!frame) frame = requestAnimationFrame(tick);
    }
    resize();
    new ResizeObserver(() => {resize(); if (reduced.matches) draw(0, true);}).observe(canvas);
    reduced.addEventListener('change', wake);
    document.addEventListener('visibilitychange', wake);
    presence.subscribe(next => {state = next; canvas.dataset.state = next; wake();});
    return {ok: true};
  }

  window.JarvisOrb = {create};
})();
