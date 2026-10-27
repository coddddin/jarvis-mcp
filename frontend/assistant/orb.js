// JARVIS presence orb: one full-screen WebGL triangle and a fragment shader.
// States: idle (slow breath), listening (follows the mic level), thinking (faster orbit, warm tint),
// speaking (pulses with the reply). No library; falls back to a CSS glow when WebGL is unavailable.
// Technique notes (analyser bins, attack/release easing, DPR cap) were learned from MIT-licensed
// community orbs on 21st.dev; the shader below is written for this project.
(() => {
  const VERTEX = 'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}';
  const FRAGMENT = `precision highp float;
uniform vec2 uRes;uniform float uTime,uLevel,uThink,uSpeak;
uniform vec3 uIon,uArc,uEmber;
float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x),f.y);}
void main(){
  vec2 p=(gl_FragCoord.xy-.5*uRes)/min(uRes.x,uRes.y);
  float r=length(p),ang=atan(p.y,p.x),L=uLevel;
  float R=.24+.035*L+.012*sin(uTime*1.3);
  // Shell: a ring whose edge is pushed by noise, more when there is sound.
  float wob=noise(vec2(ang*2.2+uTime*.35,uTime*.25))-.5;
  float shell=abs(r-R*(1.+wob*(.06+.22*L)));
  vec3 col=uArc*exp(-shell*38.)*(.55+.9*L);
  col+=uIon*exp(-shell*140.)*(.9+.8*L);
  // Filaments: thin arcs orbiting the core; thinking speeds them up.
  for(int i=0;i<5;i++){
    float fi=float(i);
    float speed=(.18+.07*fi)*(1.+2.6*uThink);
    float a=ang+uTime*speed*(mod(fi,2.)<1.?1.:-1.)+fi*1.9;
    float rr=R*(.62+.11*fi+.05*sin(uTime*.7+fi));
    float arc=smoothstep(.0,.9,sin(a*1.+fi)*.5+.5);
    float line=exp(-abs(r-rr)*(260.-30.*fi))*arc;
    col+=mix(uIon,uEmber,uThink*.75)*line*(.35+.5*L+.35*uThink);
  }
  // Core: soft light that swells while speaking.
  float core=exp(-r*r/(R*R*(.10+.10*L+.12*uSpeak)));
  col+=mix(uIon,vec3(1.),.35)*core*(.55+.6*L+.5*uSpeak);
  // Outer haze, kept inside the canvas.
  col+=uArc*.10*exp(-max(r-R,0.)*7.)*(1.+L);
  col*=smoothstep(.5,.40,r);
  col=1.-exp(-col*1.25);
  gl_FragColor=vec4(col,max(col.r,max(col.g,col.b)));
}`;

  const hex = value => {
    const n = parseInt(value.replace('#', ''), 16);
    return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
  };

  function create(canvas) {
    const gl = canvas.getContext('webgl', {premultipliedAlpha: false, antialias: true});
    const state = {level: 0, target: 0, think: 0, thinkTarget: 0, speak: 0, speakTarget: 0, mode: 'idle', source: null};
    if (!gl) { canvas.dataset.fallback = 'true'; return {setMode() {}, setSource() {}, ok: false}; }
    const compile = (type, text) => { const s = gl.createShader(type); gl.shaderSource(s, text); gl.compileShader(s); return s; };
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { canvas.dataset.fallback = 'true'; return {setMode() {}, setSource() {}, ok: false}; }
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'a');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    const u = name => gl.getUniformLocation(program, name);
    const loc = {res: u('uRes'), time: u('uTime'), level: u('uLevel'), think: u('uThink'), speak: u('uSpeak'),
      ion: u('uIon'), arc: u('uArc'), ember: u('uEmber')};
    const css = getComputedStyle(document.documentElement);
    gl.uniform3fv(loc.ion, hex(css.getPropertyValue('--ion').trim() || '#6fe3ff'));
    gl.uniform3fv(loc.arc, hex(css.getPropertyValue('--arc').trim() || '#3b8cff'));
    gl.uniform3fv(loc.ember, hex(css.getPropertyValue('--ember').trim() || '#ffb454'));

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2), box = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(box.width * ratio));
      canvas.height = Math.max(1, Math.round(box.height * ratio));
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    resize();
    new ResizeObserver(resize).observe(canvas);
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let clock = 0, last = performance.now();
    const ease = (value, target, up, down) => value + (target - value) * (target > value ? up : down);

    function frame(now) {
      const dt = Math.min(.05, (now - last) / 1000); last = now;
      state.target = state.source ? Math.max(0, Math.min(1, state.source())) : 0;
      state.level = ease(state.level, state.target, .3, .06);
      state.think = ease(state.think, state.thinkTarget, .05, .04);
      state.speak = ease(state.speak, state.speakTarget, .2, .05);
      clock += dt * (reduced.matches ? .15 : 1 + state.level * 2.2 + state.think);
      gl.uniform2f(loc.res, canvas.width, canvas.height);
      gl.uniform1f(loc.time, clock);
      gl.uniform1f(loc.level, state.level);
      gl.uniform1f(loc.think, state.think);
      gl.uniform1f(loc.speak, state.speak);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
    return {
      ok: true,
      // source: a function returning 0..1 each frame (mic level, speech pulse) or null.
      setSource(source) { state.source = source; },
      setMode(mode) {
        state.mode = mode;
        state.thinkTarget = mode === 'thinking' ? 1 : 0;
        state.speakTarget = mode === 'speaking' ? 1 : 0;
      },
    };
  }

  window.JarvisOrb = {create};
})();
