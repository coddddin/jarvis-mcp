const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

class Element {
  constructor(){this.children=[];this.textContent='';this.className='';}
  append(...items){this.children.push(...items);}
  after(item){this.following=item;}
  setAttribute(){}
}
const text=node=>[node.textContent,...node.children.map(text)].join(' ');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function harness(){
  const anchor=new Element(), events={}, visibility={}, requests=[], intervals=[], routes={};
  // Default reply answers any path (old stub behaviour); per-path responders override it.
  let reply=async()=>({ok:true,json:async()=>({service:{running:true},config_enabled:true,paused:false,read_at:1000})});
  const context={document:{hidden:false,addEventListener:(name,fn)=>visibility[name]=fn},
    window:{addEventListener:(name,fn)=>events[name]=fn},
    $:()=>anchor,element:(tag,cls,value)=>Object.assign(new Element(),{tag,className:cls,textContent:value||''}),
    stamp:String,Date,Number,Error,AbortController,setTimeout,clearTimeout,setInterval:fn=>intervals.push(fn),
    fetch:(path,options)=>{requests.push({path,options});return (routes[path]||reply)();}};
  const source=fs.readFileSync('frontend/hud.js','utf8');
  const start=source.indexOf('const serviceOverview =');
  const end=source.indexOf('\nfunction connection(',start);
  vm.runInNewContext(source.slice(start,end)+'\nglobalThis.overview=serviceOverview;',context);
  return {context,requests,panel:anchor.following,intervals,setReply:fn=>reply=fn,route:(path,fn)=>routes[path]=fn,
    ollama:()=>text(anchor.following.children[2].children[0]),dispatch:()=>text(anchor.following.children[2].children[2]),
    connect:async token=>{events['jarvis-connection']({detail:{token}});await settle();},
    visible:async()=>{visibility.visibilitychange();await settle();}};
}
const ok=body=>async()=>({ok:true,status:200,json:async()=>body});
const observed=(status,model_installed,reason)=>({source:'observed',observed_at:1000,model:'qwen2.5-coder:7b',status,model_installed,reason});
const running={service:{running:true},config_enabled:true,paused:false,read_at:1000};
test('before connection roles are visible without network calls or invented model status',()=>{
  const h=harness(); assert.equal(h.requests.length,0);
  assert.match(text(h.panel),/Ollama만 켠다고 자동 업무가 시작되지는 않/);
  assert.match(text(h.panel),/확인하지 못하면 "알 수 없음"으로 보여요. 꺼졌다는 뜻은 아니/); // one footnote for the panel
  assert.match(h.ollama(),/연결하면 확인해요/);assert.match(h.ollama(),/알 수 없음/);
  h.context.overview.hud('연결됨','connected');
  assert.doesNotMatch(h.ollama(),/실행 중|연결 안 됨/); // HUD connection alone never implies model state.
});
test('running service is read with bearer token and distinguished from work success',async()=>{
  const h=harness();await h.connect('synthetic');
  assert.equal(h.requests[0].path,'/api/dispatcher');
  assert.equal(h.requests[0].options.headers.Authorization,'Bearer synthetic');
  assert.equal(h.requests[0].options.credentials,'omit');
  assert.equal(h.requests[0].options.method,undefined); // GET only, never launch/resume.
  assert.match(h.dispatch(),/실행 중/);assert.match(h.dispatch(),/작업 현황에서 배정 결과/);
});
test('enabled config without service observation does not report running',async()=>{
  const h=harness();h.setReply(async()=>({ok:true,json:async()=>({service:{running:false},config_enabled:true,paused:false,read_at:1000})}));
  await h.connect('synthetic');const body=h.dispatch();
  assert.match(body,/실행 안 보임/);assert.match(body,/설정 켜짐/);
  assert.doesNotMatch(body,/실행 중/);assert.match(body,/시작하지 않았거나 멈췄을 수/);
});
test('paused and disabled config remain distinct from service execution',async()=>{
  for(const [config_enabled,paused,label] of [[true,true,'일시정지'],[false,false,'설정 꺼짐']]){
    const h=harness();h.setReply(async()=>({ok:true,json:async()=>({service:{running:true},config_enabled,paused,read_at:1000})}));
    await h.connect('synthetic');assert.match(h.dispatch(),new RegExp(label));assert.match(h.dispatch(),/실행 중/);
  }
});
test('network failure and malformed data replace prior successful status with unknown',async()=>{
  for(const reply of [async()=>{throw Error('offline');},async()=>({ok:false}),async()=>({ok:true,json:async()=>({service:{running:'yes'}})})]){
    const h=harness();await h.connect('synthetic');h.setReply(reply);await h.intervals[0]();
    assert.doesNotMatch(h.dispatch(),/실행 중|멈춤/);assert.match(h.dispatch(),/알 수 없음/);assert.match(h.dispatch(),/가져오지 못했|이해하지 못했/);
  }
});
test('late response after disconnect cannot restore previous service state',async()=>{
  const h=harness();let resolve;
  h.setReply(()=>new Promise(r=>resolve=r));await h.connect('synthetic');await h.connect(null);
  resolve({ok:true,json:async()=>({service:{running:true},config_enabled:true,paused:false,read_at:1000})});await settle();
  assert.doesNotMatch(h.dispatch(),/실행 중/);assert.match(h.dispatch(),/연결하면 확인해요/);
});
test('HUD connection and demo never assert chat or model readiness',()=>{
  const h=harness();h.context.overview.hud('연결됨','connected');
  assert.match(text(h.panel),/연결됨/);assert.match(text(h.panel),/답변하려면 Ollama 카드도 준비돼야/);
  h.context.overview.hud('샘플 모드','demo');assert.match(text(h.panel),/실제 상태가 아니에요/);
  assert.doesNotMatch(text(h.panel),/답변하려면 Ollama 카드도/);
});
test('new observed status contract separates stopped, absent records, unknown and reused identity',async()=>{
  const cases=[['running','matching_pid','실행 중'],['stopped','process_missing','멈춤'],
    ['not_registered','missing_record','시작 안 함'],['unknown','invalid_record','기록의 형식'],
    ['unknown','unreadable_record','기록을 읽을 수 없'],['unknown','process_unreadable','프로그램을 확인할 수 없'],
    ['identity_mismatch','pid_reused','다른 프로그램일 수 있음']];
  for(const [status,reason,label] of cases){
    const h=harness();h.setReply(async()=>({ok:true,json:async()=>({
      service:{running:status==='running',status,reason,source:'observed'},config_enabled:true,paused:false,read_at:1000})}));
    await h.connect('synthetic');const body=h.dispatch();assert.ok(body.includes(label),status+': '+body);
    if(status!=='running')assert.doesNotMatch(body,/실행 중/);
    if(status!=='stopped')assert.doesNotMatch(body,/멈춤/);
    if(['unknown','identity_mismatch'].includes(status))assert.doesNotMatch(body,/시작 안 함/);
  }
});
test('explicit unknown, inconsistent or future status cannot fall back to a positive running flag',async()=>{
  for(const service of [
    {running:true,status:'unknown',reason:'process_unreadable',source:'observed'},
    {running:true,status:'future',reason:'matching_pid',source:'observed'},
    {running:true,status:'running',reason:'pid_reused',source:'observed'},
    {running:true,status:'running',reason:'matching_pid',source:'self_reported'},
    {running:true,status:null},
  ]){
    const h=harness();h.setReply(async()=>({ok:true,json:async()=>({service,config_enabled:true,paused:false,read_at:1000})}));
    await h.connect('synthetic');assert.match(h.dispatch(),/응답을 이해하지 못했/);assert.doesNotMatch(h.dispatch(),/실행 중/);
  }
});
test('Ollama card reads the model-status endpoint with GET after connection and shows the observed result',async()=>{
  const cases=[[observed('responding',true,'model_listed'),'실행 중 · 모델 있음'],
    [observed('responding',false,'model_not_listed'),'실행 중 · 모델 없음'],
    [observed('connection_refused',null,'connection_refused'),'연결 안 됨'],
    [observed('unknown',null,'timeout'),'알 수 없음']];
  for(const [body,label] of cases){
    const h=harness();h.route('/api/dispatcher',ok(running));h.route('/api/model-status',ok(body));
    await h.connect('synthetic');
    const request=h.requests.find(item=>item.path==='/api/model-status');
    assert.equal(request.options.headers.Authorization,'Bearer synthetic');
    assert.equal(request.options.credentials,'omit');assert.equal(request.options.cache,'no-store');
    assert.ok(h.requests.every(item=>item.options.method===undefined)); // no new POSTs
    const card=h.ollama();assert.ok(card.includes(label),label+': '+card);
    assert.match(card,/1000 확인/);
    if(label!=='실행 중 · 모델 있음')assert.doesNotMatch(card,/모델 있음/);
    if(label==='알 수 없음')assert.doesNotMatch(card,/연결 안 됨|실행 중/);   // unknown is never shown as off
  }
});
test('invalid model-status contracts and old servers are unknown, never a guess',async()=>{
  for(const reply of [ok(observed('responding',null,'model_listed')),ok(observed('responding',true,'model_not_listed')),
    ok(observed('responding',true,'timeout')),ok(observed('toString',true,'name')),ok(observed('responding','name','__proto__')),
    ok({...observed('responding',true,'model_listed'),source:'self_reported'}),ok({...observed('responding',true,'model_listed'),observed_at:'now'}),
    ok({...observed('responding',true,'model_listed'),model:''}),ok(null),ok(running),
    async()=>({ok:false,status:404}),async()=>({ok:false,status:503}),async()=>{throw Error('offline');}]){
    const h=harness();h.route('/api/dispatcher',ok(running));h.route('/api/model-status',reply);
    await h.connect('synthetic');const card=h.ollama();
    assert.match(card,/알 수 없음/);assert.doesNotMatch(card,/실행 중|연결 안 됨/);
  }
  const h=harness();h.route('/api/model-status',async()=>({ok:false,status:404}));await h.connect('synthetic');
  assert.match(h.ollama(),/예전 버전이에요/);
});
test('model status and dispatcher status succeed or fail independently',async()=>{
  let h=harness();h.route('/api/dispatcher',async()=>{throw Error('offline');});
  h.route('/api/model-status',ok(observed('responding',true,'model_listed')));await h.connect('synthetic');
  assert.match(h.ollama(),/실행 중 · 모델 있음/);assert.match(h.dispatch(),/알 수 없음/);assert.match(h.dispatch(),/가져오지 못했/);
  h=harness();h.route('/api/dispatcher',ok(running));h.route('/api/model-status',async()=>{throw Error('offline');});
  await h.connect('synthetic');
  assert.match(h.dispatch(),/실행 중/);assert.match(h.ollama(),/알 수 없음/);assert.doesNotMatch(h.ollama(),/실행 중/);
});
test('model status resets on disconnect and new token and ignores late responses',async()=>{
  const h=harness(), pending=[];h.route('/api/dispatcher',ok(running));
  h.route('/api/model-status',()=>new Promise(resolve=>pending.push(resolve)));
  await h.connect('first');await h.connect(null);
  pending[0]({ok:true,status:200,json:async()=>observed('responding',true,'model_listed')});await settle();
  assert.doesNotMatch(h.ollama(),/실행 중/);assert.match(h.ollama(),/연결하면 확인해요/);
  await h.connect('second');await h.connect('third');assert.match(h.ollama(),/확인하고 있어요/);assert.doesNotMatch(h.ollama(),/실행 중/); // reset at once
  pending[1]({ok:true,status:200,json:async()=>observed('responding',true,'model_listed')});await settle();
  assert.doesNotMatch(h.ollama(),/실행 중/); // second token's late reply is ignored
  pending[2]({ok:true,status:200,json:async()=>observed('connection_refused',null,'connection_refused')});await settle();
  assert.match(h.ollama(),/연결 안 됨/);
});
test('model status is not requested while hidden or disconnected and reuses the one 30s interval',async()=>{
  const h=harness();h.route('/api/dispatcher',ok(running));
  h.route('/api/model-status',ok(observed('responding',true,'model_listed')));
  await h.intervals[0]();assert.equal(h.requests.length,0);
  h.context.document.hidden=true;await h.connect('synthetic');
  assert.equal(h.requests.length,0);assert.doesNotMatch(h.ollama(),/실행 중/);
  h.context.document.hidden=false;await h.visible();
  assert.equal(h.requests.filter(item=>item.path==='/api/model-status').length,1);
  assert.match(h.ollama(),/실행 중 · 모델 있음/);
  assert.equal(h.intervals.length,1);await h.intervals[0]();
  assert.equal(h.requests.filter(item=>item.path==='/api/model-status').length,2);
  assert.equal(h.requests.filter(item=>item.path==='/api/dispatcher').length,2);
});
