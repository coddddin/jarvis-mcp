const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.children=[]; this.events={}; this.value=''; this.hidden=false; this.classList={toggle(){}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener(name, fn) { this.events[name]=fn; }
  setAttribute() {}
  querySelectorAll() { return []; }
  focus() {}
}
const flatten = value => [value.textContent || '', ...value.children.map(flatten)].join(' ');
const deferred = () => { let resolve; const promise=new Promise(r=>resolve=r); return {promise,resolve}; };
async function harness() {
  const ids=[...fs.readFileSync('frontend/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
  const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const events={}, requests=[], timers=[];
  let handler;
  const conversation={id:'c1',turns:[]};
  const context={document:{getElementById:id=>elements[id],createElement:()=>new Element(),querySelectorAll:()=>[]},
    window:{addEventListener:(name,fn)=>events[name]=fn}, crypto:{randomUUID:()=> 'request-1'},
    setTimeout:fn=>{const t={fn};timers.push(t);return t;},clearTimeout:t=>{if(t)t.cancelled=true;},
    fetch:async(path,options)=>{requests.push({path,options});const data=handler ? await handler(path,options) :
      path.endsWith('/conversations') ? {model:'local',conversations:[{id:'c1',title:'test'}]} : conversation;
      return {ok:true,json:async()=>data};}};
  vm.runInNewContext(fs.readFileSync('frontend/chat.js','utf8'),context);
  await events['jarvis-connection']({detail:{token:'test-token'}});
  return {elements,events,requests,timers,conversation,setHandler:fn=>handler=fn};
}

test('chat initializes without requiring the user to select a mode',async()=>{
  const h=await harness();
  assert.equal(h.elements.chatMode,undefined);
  assert.equal(h.elements.chatSend.disabled,false);
});

test('mode is posted, progress is authenticated, late polls cannot overwrite completion',async()=>{
  const h=await harness(), post=deferred(), poll=deferred();
  h.elements.chatInput.value='proposal';
  const done={id:'c1',turns:[{user:'proposal',assistant:'answer',status:'completed',team:{status:'needs_user_input',corrections:2,stop_reason:'external_call_limit'}}]};
  h.setHandler((path,options)=>options.method==='POST'?post.promise:
    path.endsWith('/conversations')?{model:'local',conversations:[]}:poll.promise);
  const sending=h.elements.chatForm.events.submit({preventDefault(){}});
  const posted=h.requests.at(-1);assert.equal(JSON.parse(posted.options.body).mode,'auto');
  assert.equal(h.elements.chatInput.disabled,true);
  const polling=h.timers[0].fn();
  assert.equal(h.requests.at(-1).options.headers.Authorization,'Bearer test-token');
  post.resolve(done);await sending;
  poll.resolve({id:'c1',turns:[]});await polling;
  assert.match(flatten(h.elements.chatMessages),/협업 결과: 사용자 확인 필요/);
  assert.match(flatten(h.elements.chatMessages),/응답 전달: 완료/);
  assert.match(flatten(h.elements.chatMessages),/수정 횟수: 2/);
  assert.match(flatten(h.elements.chatMessages),/외부 호출 상한/);
  assert.match(h.elements.chatNotice.textContent,/사용자 확인/);
  assert.equal(h.elements.chatInput.disabled,false);
  assert.equal(h.timers.length,1);
});

test('disconnect prevents stale progress from restoring private conversation',async()=>{
  const h=await harness(), post=deferred(), poll=deferred();
  h.elements.chatInput.value='proposal';
  h.setHandler((path,options)=>options.method==='POST'?post.promise:poll.promise);
  const sending=h.elements.chatForm.events.submit({preventDefault(){}});
  const polling=h.timers[0].fn();
  await h.events['jarvis-connection']({detail:{token:null}});
  const old={id:'c1',turns:[{user:'private',assistant:'private',status:'completed'}]};
  poll.resolve(old);post.resolve(old);await Promise.all([polling,sending]);
  assert.equal(h.elements.chatMessages.children.length,0);
});

test('free dispatch displays no eligible provider separately from delivered response',async()=>{
  const h=await harness();h.elements.chatInput.value='free proposal';
  h.setHandler((path,options)=>options.method==='POST'?{id:'c1',turns:[{user:'free proposal',assistant:'No provider',status:'completed',
    dispatch:{status:'needs_user_input',selection:{status:'no_eligible_provider',rejected:[{id:'opencode-free',reason:'access_not_verified'}]},budget:{attempts:[]}}}]}:
    {model:'local',conversations:[]});
  await h.elements.chatForm.events.submit({preventDefault(){}});
  const text=flatten(h.elements.chatMessages);
  assert.match(text,/무료 자동 배정: 사용자 확인 필요/);
  assert.match(text,/응답 전달: 완료/);
  assert.match(text,/실제 접근 미확인/);
  assert.match(h.elements.chatNotice.textContent,/중단/);
});
