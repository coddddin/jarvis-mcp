const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const CODE='c'.repeat(43), TOKEN='t'.repeat(43);
// Loads the storage helpers and autoConnect from hud.js with stubbed page functions.
function harness({hash='', stored=null, reply, storageThrows=false, connectResult=true}={}){
  const source=fs.readFileSync('frontend/hud.js','utf8');
  const helpers=source.slice(source.indexOf('const SAVED_CONNECTION'),source.indexOf('function disconnect('));
  const start=source.indexOf('async function exchange');
  const auto=source.slice(start,source.indexOf("document.addEventListener('DOMContentLoaded',autoConnect);",start));
  const store=new Map(stored?[['jarvis-hud-connection',stored]]:[]);
  const log={connected:[],labels:[],notices:[],requests:[],replaced:[]};
  const sessionStorage=storageThrows?{getItem(){throw Error('blocked');},setItem(){throw Error('blocked');},removeItem(){throw Error('blocked');}}
    :{getItem:key=>store.get(key)??null,setItem:(key,value)=>store.set(key,value),removeItem:key=>store.delete(key)};
  const context={state:{token:null,demo:false},sessionStorage,JSON,AbortSignal:{timeout:()=>undefined},
    location:{hash,pathname:'/'},history:{replaceState:(a,b,url)=>{log.replaced.push(url);context.location.hash='';}},
    connection:text=>log.labels.push(text),notice:text=>log.notices.push(text),
    connect:async(token,fromSaved=false)=>{log.connected.push(fromSaved?'saved:'+token:token);return typeof connectResult==='function'?connectResult(token):connectResult;},
    fetch:async(path,options)=>{log.requests.push({path,options});return reply?reply():{ok:true,json:async()=>({token:TOKEN})};}};
  vm.runInNewContext(helpers+auto+'\nglobalThis.run=autoConnect;globalThis.helpers={remember,forget,saved};',context);
  return {context,log,store};
}

test('a one-time code in the fragment is removed at once and traded for the connection',async()=>{
  const h=harness({hash:'#connect='+CODE});
  const pending=h.context.run();
  assert.deepEqual(h.log.replaced,['/']);                       // cleared before any network call returns
  await pending;
  assert.equal(h.log.requests.length,1);
  const request=h.log.requests[0];
  assert.equal(request.path,'/api/connect');
  assert.equal(request.options.method,'POST');
  assert.equal(request.options.credentials,'omit');
  assert.deepEqual(JSON.parse(request.options.body),{code:CODE});
  assert.deepEqual(h.log.connected,[TOKEN]);
});

test('an expired or reused code falls back to the file picker with a short notice',async()=>{
  const h=harness({hash:'#connect='+CODE,reply:()=>({ok:false,status:410})});
  await h.context.run();
  assert.deepEqual(h.log.connected,[]);
  assert.match(h.log.notices.at(-1),/연결 파일을 선택/);
  const offline=harness({hash:'#connect='+CODE,reply:()=>{throw Error('offline');}});
  await offline.context.run();
  assert.match(offline.log.notices.at(-1),/연결 파일을 선택/);
});

test('a reload reconnects from this tab only and needs no code',async()=>{
  const h=harness({stored:TOKEN});
  await h.context.run();
  assert.deepEqual(h.log.connected,['saved:'+TOKEN]);
  assert.equal(h.log.requests.length,0);
});

test('a fresh code is tried before the saved connection',async()=>{
  const h=harness({hash:'#connect='+CODE,stored:'s'.repeat(43)});
  await h.context.run();
  assert.deepEqual(h.log.connected,[TOKEN]);                     // saved value never used when the code works
});

test('a spent code falls back to the saved connection',async()=>{
  const h=harness({hash:'#connect='+CODE,stored:'s'.repeat(43),reply:()=>({ok:false,status:410})});
  await h.context.run();
  assert.deepEqual(h.log.connected,['saved:'+'s'.repeat(43)]);
});

// connect() itself, with the page functions stubbed: a saved value that fails is forgotten.
function connectHarness(load){
  const source=fs.readFileSync('frontend/hud.js','utf8');
  const start=source.indexOf('async function connect(');
  const body=source.slice(start,source.indexOf('\n// `hud --open`',start));
  const log={forgot:0,remembered:[]};
  const context={state:{token:null,demo:false,cursors:new Map(),feeds:new Map(),recovery:new Set()},
    stopStream(){},resetTimings(){},notice(){},connection(){},watch(){},loadJobs:load,
    forget:()=>log.forgot++,remember:token=>log.remembered.push(token)};
  vm.runInNewContext(body+'\nglobalThis.connect=connect;',context);
  return {connect:context.connect,log};
}

test('a saved connection that fails is forgotten; a picked file that fails is not',async()=>{
  const failing=connectHarness(async()=>{throw Error('자비스 상태를 가져오지 못했어요.');});
  assert.equal(await failing.connect(TOKEN,true),false);assert.equal(failing.log.forgot,1);
  assert.equal(await failing.connect(TOKEN),false);assert.equal(failing.log.forgot,1);
  const malformed=connectHarness(async()=>{});
  assert.equal(await malformed.connect('bad',true),false);assert.equal(malformed.log.forgot,1);
  const ok=connectHarness(async()=>{});
  assert.equal(await ok.connect(TOKEN,true),true);assert.equal(ok.log.forgot,0);assert.deepEqual(ok.log.remembered,[TOKEN]);
});

test('no fragment and nothing saved does nothing',async()=>{
  const h=harness();
  await h.context.run();
  assert.deepEqual(h.log.connected,[]);assert.equal(h.log.requests.length,0);assert.deepEqual(h.log.replaced,[]);
});

test('other fragments are left alone and malformed codes are never sent',async()=>{
  const h=harness({hash:'#connect=short'});
  await h.context.run();
  assert.equal(h.log.requests.length,0);
  assert.deepEqual(h.log.replaced,['/']);                       // still removed from the address bar
  const other=harness({hash:'#section'});
  await other.context.run();
  assert.deepEqual(other.log.replaced,[]);
});

test('storage helpers keep, forget and survive blocked storage',()=>{
  const h=harness();
  h.context.helpers.remember(TOKEN);assert.equal(h.context.helpers.saved(),TOKEN);
  h.context.helpers.forget();assert.equal(h.context.helpers.saved(),null);
  const blocked=harness({storageThrows:true});
  blocked.context.helpers.remember(TOKEN);blocked.context.helpers.forget();
  assert.equal(blocked.context.helpers.saved(),null);
});

test('disconnect and sample mode forget the saved connection; the file picker shares connect()',()=>{
  const source=fs.readFileSync('frontend/hud.js','utf8');
  assert.match(source,/function disconnect\(\)\{forget\(\);/);
  assert.match(source,/forget\(\); stopStream\(\); state\.token=null; state\.demo=true;/);
  assert.match(source,/await connect\(token\);\n\}\);/);
  assert.doesNotMatch(source,/localStorage/);
});
