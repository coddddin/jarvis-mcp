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
const flatten = value => typeof value === 'string' ? value : [value.textContent || '', ...value.children.map(flatten)].join(' ');
const all = (value, out = []) => { if (typeof value !== 'string') { out.push(value); value.children.forEach(child => all(child, out)); } return out; };
const deferred = () => { let resolve; const promise=new Promise(r=>resolve=r); return {promise,resolve}; };
async function harness(windowExtras = {}) {
  const ids=[...fs.readFileSync('frontend/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
  const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const events={}, documentEvents={}, requests=[], timers=[];
  let handler;
  const conversation={id:'c1',turns:[]};
  const context={document:{addEventListener:(name,fn)=>documentEvents[name]=fn,getElementById:id=>elements[id],createElement:tag=>Object.assign(new Element(),{tagName:tag.toUpperCase()}),querySelectorAll:()=>[]},
    window:{...windowExtras,addEventListener:(name,fn)=>events[name]=fn}, crypto:{randomUUID:()=> 'request-1'},
    setTimeout:(fn,ms)=>{const t={fn,ms};timers.push(t);return t;},clearTimeout:t=>{if(t)t.cancelled=true;},
    fetch:async(path,options)=>{requests.push({path,options});const data=handler ? await handler(path,options) :
      path.endsWith('/conversations') ? {model:'local',conversations:[{id:'c1',title:'test'}]} : conversation;
      return {ok:true,json:async()=>data};}};
  vm.runInNewContext(fs.readFileSync('frontend/chat.js','utf8'),context);
  await events['jarvis-connection']({detail:{token:'test-token'}});
  return {elements,events,documentEvents,context,requests,timers,conversation,setHandler:fn=>handler=fn};
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
  assert.match(text,/접근 확인 안 됨/);
  assert.match(h.elements.chatNotice.textContent,/중단/);
});

async function reply(assistant, user='question') {
  const h=await harness();h.elements.chatInput.value=user;
  h.setHandler((path,options)=>options.method==='POST'?{id:'c1',turns:[{user,assistant,status:'completed'}]}:{model:'local',conversations:[]});
  await h.elements.chatForm.events.submit({preventDefault(){}});
  const messages=h.elements.chatMessages.children.filter(child=>child.className.startsWith('chat-message'));
  return {user:messages[0],assistant:messages[1]};
}

test('chat.js never assigns HTML',()=>{
  assert.doesNotMatch(fs.readFileSync('frontend/chat.js','utf8'),/innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
});

test('reply markdown becomes elements: code block, heading, lists, bold, inline code',async()=>{
  const {assistant}=await reply('# Title\nText with **bold** and `x<y`.\n\n- one\n- two\n\n3. three\n4. four\n\n```js\nconst a = "<b>**no**</b>";\n```');
  const nodes=all(assistant), tags=nodes.map(n=>n.tagName);
  const body=assistant.children[1];
  assert.equal(body.className,'chat-body chat-markdown');
  assert.equal(nodes.find(n=>n.tagName==='H4').children.join(''),'Title');
  assert.equal(nodes.find(n=>n.tagName==='H4').className,'chat-md-heading');  // not the panel header's chat-heading
  assert.equal(nodes.find(n=>n.tagName==='STRONG').textContent,'bold');
  assert.equal(nodes.find(n=>n.className==='chat-inline-code').textContent,'x<y');
  assert.deepEqual(nodes.filter(n=>n.tagName==='UL')[0].children.map(li=>li.children.join('')),['one','two']);
  const ol=nodes.find(n=>n.tagName==='OL');
  assert.equal(ol.start,3);
  assert.deepEqual(ol.children.map(li=>li.children.join('')),['three','four']);
  const pre=nodes.find(n=>n.tagName==='PRE');
  assert.equal(pre.children[0].tagName,'CODE');
  assert.equal(pre.children[0].textContent,'const a = "<b>**no**</b>";');  // literal text, no inline parsing inside code
  assert.ok(!tags.includes('B') && !tags.includes('SCRIPT'));
});

test('only http(s) links become anchors; other schemes stay literal text',async()=>{
  const {assistant}=await reply('[docs](https://example.com/a) [bad](javascript:alert(1)) [file](file:///C:/x)');
  const links=all(assistant).filter(n=>n.tagName==='A');
  assert.equal(links.length,1);
  assert.equal(links[0].href,'https://example.com/a');
  assert.equal(links[0].rel,'noopener noreferrer');
  assert.equal(links[0].target,'_blank');
  assert.equal(links[0].title,'https://example.com/a');
  const text=flatten(assistant);
  assert.match(text,/\[bad\]\(javascript:alert\(1\)/);
  assert.match(text,/\[file\]\(file:\/\/\/C:\/x\)/);
});

test('an unclosed fence keeps the rest as code, and user text is never parsed',async()=>{
  const {user,assistant}=await reply('before\n```\nline1\n# not a heading','**mine** `raw`');
  const pre=all(assistant).find(n=>n.tagName==='PRE');
  assert.equal(pre.children[0].textContent,'line1\n# not a heading');
  assert.ok(!all(assistant).some(n=>n.tagName==='H4'));
  const body=user.children[1];
  assert.equal(body.tagName,'P');
  assert.equal(body.textContent,'**mine** `raw`');
});

test('a pending draft shows dimmed, labelled and unparsed; the final reply replaces it with Markdown',async()=>{
  const h=await harness(), post=deferred();
  h.elements.chatInput.value='question';
  const draft={id:'c1',turns:[{user:'question',assistant:'',status:'pending',draft:'**half** answer'}]};
  h.setHandler((path,options)=>options.method==='POST'?post.promise:path.endsWith('/conversations')?{model:'local',conversations:[]}:draft);
  const sending=h.elements.chatForm.events.submit({preventDefault(){}});
  assert.equal(h.timers[0].ms,400);
  await h.timers[0].fn();
  const pending=h.elements.chatMessages.children.find(child=>String(child.className).includes('chat-draft'));
  assert.ok(pending,'draft bubble shown while pending');
  assert.equal(pending.children[0].textContent,'J.A.R.V.I.S. · 작성 중 · 확인 전');
  assert.equal(pending.children[1].textContent,'**half** answer');          // plain text, not Markdown
  assert.ok(!all(pending).some(n=>n.tagName==='STRONG'));
  assert.equal(h.timers.at(-1).ms,400);                                     // keeps polling while pending
  post.resolve({id:'c1',turns:[{user:'question',assistant:'**final** answer',status:'completed',draft:''}]});
  await sending;
  const kids=h.elements.chatMessages.children;
  assert.ok(!kids.some(child=>String(child.className).includes('chat-draft')));
  assert.ok(all(kids.at(-1)).some(n=>n.tagName==='STRONG'));
});

test('a draft is never shown once the turn is no longer pending',async()=>{
  const h=await harness();h.elements.chatInput.value='q';
  h.setHandler((path,options)=>options.method==='POST'?{id:'c1',turns:[{user:'q',assistant:'',status:'failed',draft:'stale'}]}:{model:'local',conversations:[]});
  await h.elements.chatForm.events.submit({preventDefault(){}});
  assert.doesNotMatch(flatten(h.elements.chatMessages),/stale/);
});

async function voiceHarness({mute=false, local=true} = {}) {
  const readers=[], spoken=[];let cancels=0;
  class Recognition {
    constructor(){readers.push(this);}
    start(){this.started=true;}
    abort(){this.aborted=true;}
  }
  const h=await harness({SpeechRecognition:Recognition, SpeechSynthesisUtterance:class {constructor(text){this.text=text;}},
    speechSynthesis:{getVoices:()=>[{localService:local,lang:'ko-KR'}],speak:u=>spoken.push(u),cancel:()=>cancels++},
    location:{search:mute?'?mute=1':''}});
  const click=id=>h.elements[id].events.click();
  const consent=()=>{h.elements.voiceConsent.checked=true;h.elements.voiceConsent.events.change();};
  const result=(reader,phrases)=>reader.onresult({results:phrases.map(text=>({isFinal:true,0:{transcript:text}}))});
  return {...h,readers,spoken,cancels:()=>cancels,click,consent,result};
}

test('voice requires opt-in, buffers all final phrases into draft and never submits',async()=>{
  const h=await voiceHarness();
  h.click('voiceListen');assert.equal(h.readers.length,0);
  h.consent();h.elements.chatInput.value='메모';const requests=h.requests.length;
  h.click('voiceListen');const r=h.readers[0];assert.equal(r.lang,'ko-KR');assert.equal(r.continuous,true);
  h.result(r,['첫 문장']);h.result(r,['첫 문장','두 번째']);
  assert.equal(h.elements.chatInput.value,'메모 첫 문장 두 번째');
  assert.equal(h.requests.length,requests);assert.equal(h.elements.voiceListen.textContent,'입력 마치기');
  h.click('voiceListen');assert.ok(r.aborted);h.result(r,['늦은 입력']);
  assert.equal(h.elements.chatInput.value,'메모 첫 문장 두 번째');
});

test('disconnect and reconnect fence old voice results without restoring private input',async()=>{
  const h=await voiceHarness();h.consent();h.click('voiceListen');const old=h.readers[0];
  await h.events['jarvis-connection']({detail:{token:null}});
  assert.ok(old.aborted);assert.equal(h.elements.voiceListen.disabled,true);
  await h.events['jarvis-connection']({detail:{token:'new'}});
  h.click('voiceListen');h.result(old,['private']);assert.equal(h.elements.chatInput.value,'');
  h.result(h.readers[1],['새 입력']);assert.equal(h.elements.chatInput.value,'새 입력');
});

test('manual editing, hide, page exit and consent withdrawal abort recognition',async()=>{
  for(const stop of ['input','hidden','pagehide','consent','tasks','escape']){
    const h=await voiceHarness();h.consent();h.click('voiceListen');const r=h.readers[0];
    if(stop==='input'){h.elements.chatInput.value='수정';h.elements.chatInput.events.input();}
    if(stop==='hidden'){h.context.document.hidden=true;h.documentEvents.visibilitychange();}
    if(stop==='pagehide')h.events.pagehide();
    if(stop==='consent'){h.elements.voiceConsent.checked=false;h.elements.voiceConsent.events.change();}
    if(stop==='tasks')h.elements.tasksTab.events.click();
    if(stop==='escape')h.events.keydown({key:'Escape'});
    assert.ok(r.aborted,stop);const previous=h.elements.chatInput.value;
    h.result(r,['stale']);assert.equal(h.elements.chatInput.value,previous,stop);
  }
});

test('recognition timeout, errors and natural end never restart the microphone',async()=>{
  for(const end of ['timeout','error','natural']){
    const h=await voiceHarness();h.consent();h.click('voiceListen');const r=h.readers[0];
    if(end==='timeout'){assert.equal(h.timers.at(-1).ms,30000);h.timers.at(-1).fn();}
    if(end==='error')r.onerror({error:'not-allowed'});
    if(end==='natural')r.onend();
    assert.equal(h.readers.length,1);assert.equal(h.elements.voiceStop.disabled,true);
    h.result(r,['old']);assert.equal(h.elements.chatInput.value,'');
  }
});

test('voice input enforces the existing composer limit and ignores stale overflow callbacks',async()=>{
  const h=await voiceHarness();h.consent();h.click('voiceListen');const r=h.readers[0];
  h.result(r,['가'.repeat(2001)]);assert.equal(h.elements.chatInput.value.length,2000);assert.ok(r.aborted);
  assert.match(h.elements.voiceStatus.textContent,/2,000/);
});

test('read is manual, local Korean only, completed latest turn only and mute blocks it',async()=>{
  for(const options of [{},{mute:true},{local:false}]){
    const h=await voiceHarness(options);
    h.conversation.turns=[{user:'q',assistant:'완성된 답',status:'completed'}];
    assert.equal(h.spoken.length,0);h.click('voiceRead');
    assert.equal(h.spoken.length,options.mute||options.local===false?0:1);
    if(h.spoken.length){assert.equal(h.spoken[0].text,'완성된 답');assert.equal(h.spoken[0].voice.localService,true);
      h.click('voiceStop');assert.equal(h.cancels(),1);assert.equal(h.elements.voiceStop.disabled,true);}
    h.conversation.turns.push({user:'q2',assistant:'아직 확인 전',draft:'partial',status:'pending'});
    const count=h.spoken.length;h.click('voiceRead');assert.equal(h.spoken.length,count);
  }
});

test('sending cancels voice and speech; new conversation aborts before its request',async()=>{
  const h=await voiceHarness();h.consent();h.click('voiceListen');const reader=h.readers[0];
  h.elements.chatInput.value='질문';h.setHandler((path,options)=>options.method==='POST'?{id:'c1',turns:[{user:'질문',assistant:'답',status:'completed'}]}:{model:'local',conversations:[]});
  await h.elements.chatForm.events.submit({preventDefault(){}});assert.ok(reader.aborted);
  h.click('voiceRead');assert.equal(h.spoken.length,1);
  const pending=deferred();h.setHandler(()=>pending.promise);
  const creating=h.elements.newChat.events.click();assert.equal(h.cancels(),1);
  await h.events['jarvis-connection']({detail:{token:null}});pending.resolve({id:'new',turns:[],conversations:[]});await creating;
});

test('unsupported APIs keep text chat usable; failed microphone start is recoverable',async()=>{
  const plain=await harness();assert.equal(plain.elements.chatSend.disabled,false);assert.equal(plain.elements.voiceListen.disabled,true);
  const h=await harness({SpeechRecognition:class {start(){throw Error('denied');}abort(){}}});
  h.elements.voiceConsent.checked=true;h.elements.voiceConsent.events.change();h.elements.voiceListen.events.click();
  assert.match(h.elements.voiceStatus.textContent,/시작하지 못/);assert.equal(h.elements.chatSend.disabled,false);
});
