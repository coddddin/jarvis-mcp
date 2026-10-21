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
  const anchor=new Element(), events={}, visibility={}, requests=[], intervals=[];
  let reply=async()=>({ok:true,json:async()=>({service:{running:true},config_enabled:true,paused:false,read_at:1000})});
  const context={document:{hidden:false,addEventListener:(name,fn)=>visibility[name]=fn},
    window:{addEventListener:(name,fn)=>events[name]=fn},
    $:()=>anchor,element:(tag,cls,value)=>Object.assign(new Element(),{tag,className:cls,textContent:value||''}),
    stamp:String,Date,Number,Error,AbortController,setTimeout,clearTimeout,setInterval:fn=>intervals.push(fn),
    fetch:(path,options)=>{requests.push({path,options});return reply();}};
  const source=fs.readFileSync('frontend/hud.js','utf8');
  const start=source.indexOf('const serviceOverview =');
  const end=source.indexOf('\nfunction connection(',start);
  vm.runInNewContext(source.slice(start,end)+'\nglobalThis.overview=serviceOverview;',context);
  return {context,requests,panel:anchor.following,intervals,setReply:fn=>reply=fn,
    connect:async token=>{events['jarvis-connection']({detail:{token}});await settle();}};
}
test('before connection roles are visible without network calls or invented model status',()=>{
  const h=harness(); assert.equal(h.requests.length,0);
  assert.match(text(h.panel),/Ollama만 켜면 자동 업무가 시작되는 것은 아니/);
  assert.match(text(h.panel),/모델 서버를 직접 확인하는 정보가 없/);
});
test('running service is read with bearer token and distinguished from work success',async()=>{
  const h=harness();await h.connect('synthetic');
  assert.equal(h.requests[0].path,'/api/dispatcher');
  assert.equal(h.requests[0].options.headers.Authorization,'Bearer synthetic');
  assert.equal(h.requests[0].options.credentials,'omit');
  assert.equal(h.requests[0].options.method,undefined); // GET only, never launch/resume.
  assert.match(text(h.panel),/서비스 실행 관찰/);assert.match(text(h.panel),/실제 업무 진행은 작업 기록/);
});
test('enabled config without service observation does not report running',async()=>{
  const h=harness();h.setReply(async()=>({ok:true,json:async()=>({service:{running:false},config_enabled:true,paused:false,read_at:1000})}));
  await h.connect('synthetic');const body=text(h.panel);
  assert.match(body,/실행 확인 안 됨/);assert.match(body,/자동 배정 설정 켜짐/);
  assert.doesNotMatch(body,/서비스 실행 관찰/);assert.match(body,/미등록 또는 중지/);
});
test('paused and disabled config remain distinct from service execution',async()=>{
  for(const [config_enabled,paused,label] of [[true,true,'일시정지'],[false,false,'설정 꺼짐']]){
    const h=harness();h.setReply(async()=>({ok:true,json:async()=>({service:{running:true},config_enabled,paused,read_at:1000})}));
    await h.connect('synthetic');assert.match(text(h.panel),new RegExp(label));assert.match(text(h.panel),/서비스 실행 관찰/);
  }
});
test('network failure and malformed data replace prior successful status with unknown',async()=>{
  for(const reply of [async()=>{throw Error('offline');},async()=>({ok:false}),async()=>({ok:true,json:async()=>({service:{running:'yes'}})})]){
    const h=harness();await h.connect('synthetic');h.setReply(reply);await h.intervals[0]();
    assert.doesNotMatch(text(h.panel),/서비스 실행 관찰/);assert.match(text(h.panel),/꺼졌다고 단정할 수는 없/);
  }
});
test('late response after disconnect cannot restore previous service state',async()=>{
  const h=harness();let resolve;
  h.setReply(()=>new Promise(r=>resolve=r));await h.connect('synthetic');await h.connect(null);
  resolve({ok:true,json:async()=>({service:{running:true},config_enabled:true,paused:false,read_at:1000})});await settle();
  assert.doesNotMatch(text(h.panel),/서비스 실행 관찰/);assert.match(text(h.panel),/실제 서비스에 연결하지 않았/);
});
test('HUD connection and demo never assert chat or model readiness',()=>{
  const h=harness();h.context.overview.hud('연결됨','connected');
  assert.match(text(h.panel),/작업 기록 연결 확인/);assert.match(text(h.panel),/채팅 응답 준비를 확인한 것은 아니/);
  h.context.overview.hud('샘플 모드','demo');assert.match(text(h.panel),/샘플 · 실제 상태 미확인/);
  assert.doesNotMatch(text(h.panel),/작업 기록 연결 확인/);
});
