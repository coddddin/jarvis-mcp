const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JarvisTaskStream,JarvisDisplayTimings}=require('../../frontend/task-stream.js');

function harness({latest=0,terminal=false}={}){
  const sockets=[],timers=[],events=[],states=[],errors=[];
  const stream=new JarvisTaskStream({socket:url=>{const s={url,send(value){this.sent=value;},close(){this.closed=true;}};sockets.push(s);return s;},
    later:(run,delay)=>{const timer={run,delay};timers.push(timer);return timer;},cancel:t=>{if(t)t.cancelled=true;},now:()=>10000,
    onEvent:(event,meta)=>events.push({event,meta}),onState:state=>states.push(state),onRecovery:reason=>errors.push(reason)});
  stream.start({url:'ws://127.0.0.1/tasks/test/events',token:'private-connection',taskId:'test',after:0,latest,isTerminal:()=>terminal});
  const next=()=>{const timer=timers.find(t=>!t.cancelled&&!t.used);assert.ok(timer);timer.used=true;timer.run();return sockets.at(-1);};
  return {stream,sockets,timers,events,states,errors,next};
}
function event(sequence,changes={}){return {version:1,task_id:'test',kind:'task.state',sequence,timestamp:10.01,agent_id:'coder',
  payload:{status:'running',role:'coder',steps_started:2,correction_retries:0,generation_tokens_reserved:1024},...changes};}
const send=(socket,data)=>socket.onmessage({data:JSON.stringify(data)});

test('healthy idle rotations continue beyond the failure limit without resetting the cursor',()=>{
  const h=harness();let s=h.sockets[0];s.onopen();send(s,event(1));
  for(let i=0;i<5;i++){send(s,{kind:'stream.end'});s.onclose({code:1000});s=h.next();assert.match(s.url,/after=1$/);s.onopen();}
  assert.equal(h.stream.retries,0);assert.deepEqual(h.errors,[]);assert.equal(h.events.length,1);
});
test('abnormal disconnect retries three times and then exposes offline state',()=>{
  const h=harness();let s=h.sockets[0];
  for(let i=0;i<3;i++){s.onclose({code:1006});s=h.next();}
  s.onclose({code:1006});assert.equal(h.sockets.length,4);assert.equal(h.states.at(-1),'offline');
});
test('partial replay of a completed snapshot reconnects until its final sequence arrives',()=>{
  const h=harness({latest:3,terminal:true});let s=h.sockets[0];s.onopen();send(s,event(1));s.onclose({code:1006});
  s=h.next();assert.match(s.url,/after=1$/);s.onopen();send(s,event(2));send(s,event(3));s.onclose({code:1000});
  assert.equal(h.states.at(-1),'synced');assert.equal(h.events.length,3);
  assert.equal(h.timers.filter(t=>!t.used&&!t.cancelled).length,0);
});
test('duplicate replay is ignored; a gap stops and requests explicit snapshot recovery',()=>{
  const h=harness();const s=h.sockets[0];s.onopen();send(s,event(1));send(s,event(1));send(s,event(3));
  assert.equal(h.events.length,1);assert.deepEqual(h.errors,['gap']);assert.ok(s.closed);assert.equal(h.stream.config,null);
});
test('bad payload and denied credentials fail closed without retry loops',()=>{
  const malformed=harness();send(malformed.sockets[0],event(1,{payload:{status:'imaginary'}}));assert.deepEqual(malformed.errors,['format']);
  const denied=harness();denied.sockets[0].onclose({code:1008});assert.deepEqual(denied.errors,['denied']);assert.equal(denied.timers.length,0);
});
test('disconnect releases credentials and invalidates late callbacks and pending retries',()=>{
  const h=harness();const s=h.sockets[0];s.onclose({code:1006});h.stream.stop();
  s.onopen();send(s,event(1));h.timers[0].run();
  assert.equal(s.sent,undefined);assert.equal(h.events.length,0);assert.equal(h.sockets.length,1);assert.equal(h.stream.config,null);
});
test('live timing excludes snapshot replay and records published events after connection',()=>{
  const h=harness({latest:1});const s=h.sockets[0];s.onopen();send(s,event(1));send(s,event(2,{timestamp:9.9}));send(s,event(3));
  assert.deepEqual(h.events.map(e=>e.meta.live),[false,false,true]);assert.equal(JSON.parse(s.sent).token,'private-connection');
  assert.ok(!s.url.includes('private-connection'));
});
test('display percentile uses bounded valid observations',()=>{
  const timings=new JarvisDisplayTimings();timings.record(-1);timings.record(NaN);assert.equal(timings.summary().count,0);
  for(let i=1;i<=100;i++)timings.record(i);
  assert.deepEqual(timings.summary(),{count:64,p95:97});
});
