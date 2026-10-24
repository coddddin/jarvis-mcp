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
async function harness() {
  const ids=[...fs.readFileSync('frontend/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
  const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const events={}, requests=[], timers=[];
  let handler;
  const conversation={id:'c1',turns:[]};
  const context={document:{getElementById:id=>elements[id],createElement:tag=>Object.assign(new Element(),{tagName:tag.toUpperCase()}),querySelectorAll:()=>[]},
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
