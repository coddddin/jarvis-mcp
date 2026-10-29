const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
class Element{constructor(){this.value='';this.events={};this.children=[];}addEventListener(k,fn){this.events[k]=fn;}replaceChildren(...v){this.children=v;}append(...v){this.children.push(...v);}}
const defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};
const candidate={id:'b'.repeat(32),digest:'d'.repeat(64),status:'proposed',summary:'<script>not HTML</script>',facts:[{text:'로컬 모델',quote:'로컬 Ollama',line:1}],source:{id:'a'.repeat(32),title:'자료',sha256:'e'.repeat(64)}};
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
async function harness({embedded=false}={}){const ids=[...fs.readFileSync('frontend/wiki/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>m[1]);const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));const requests=[],events={},confirms=[];let handler,subscriber,answer=true;let shared=null;if(embedded)nodes.knowledgeSpace=new Element();const storage=new Map([['jarvis-hud-connection','x'.repeat(43)]]);const context={URLSearchParams,document:{getElementById:id=>nodes[id],createElement:tag=>Object.assign(new Element(),{tagName:tag.toUpperCase()})},window:{confirm:text=>{confirms.push(text);return answer;},JarvisConnection:{subscribe:fn=>{subscriber=fn;fn(shared);},forget:()=>{shared=null;subscriber?.(null);}},location:{hash:'',pathname:'/wiki',search:''},addEventListener:(name,fn)=>events[name]=fn},history:{replaceState(){}},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},crypto:{randomUUID:()=> 'a'.repeat(32)},fetch:async(path,opts)=>{requests.push({path,opts});const v=handler?await handler(path,opts):{};return typeof v?.status==='number'?{ok:false,status:v.status,json:async()=>({error:v.error})}:{ok:true,status:200,json:async()=>v};}};vm.runInNewContext(fs.readFileSync('frontend/wiki/wiki.js','utf8'),context);await tick();return {nodes,requests,events,storage,confirms,answer:value=>answer=value,publish:value=>{shared=value;subscriber?.(value);},set:fn=>handler=fn};}
test('prepare is only a proposal, explicit approval and safe text rendering',async()=>{const h=await harness();h.nodes.wikiSourceTitle.value='資料';h.nodes.wikiSourceText.value='로컬 Ollama';h.set((p)=>p.endsWith('/prepare')?candidate:{indexed:true});await h.nodes.wikiPrepare.events.click();assert.equal(h.nodes.wikiCandidate.hidden,false);assert.equal(h.nodes.wikiSummary.textContent,candidate.summary);assert.equal(h.requests.filter(x=>x.path.endsWith('/approve')).length,0);assert.equal(h.nodes.wikiApprove.disabled,false);await h.nodes.wikiApprove.events.click();assert.equal(h.nodes.wikiApprove.disabled,true);assert.match(h.nodes.wikiNotice.textContent,/저장/);assert.equal(h.nodes.wikiNotice.hidden,false);});
test('editing after proposal invalidates approval',async()=>{const h=await harness();h.nodes.wikiSourceTitle.value='자료';h.nodes.wikiSourceText.value='로컬';h.set(()=>candidate);await h.nodes.wikiPrepare.events.click();assert.equal(h.nodes.wikiCandidate.hidden,false);h.nodes.wikiSourceText.events.input();assert.equal(h.nodes.wikiApprove.disabled,true);assert.equal(h.nodes.wikiCandidate.hidden,true);});
test('late prepare result after disconnect never restores private proposal',async()=>{const h=await harness(),p=defer();h.nodes.wikiSourceTitle.value='자료';h.nodes.wikiSourceText.value='로컬';h.set(()=>p.promise);const running=h.nodes.wikiPrepare.events.click();await tick();h.nodes.wikiDisconnect.events.click();p.resolve(candidate);await running;assert.equal(h.nodes.wikiCandidate.hidden,true);assert.equal(h.nodes.wikiPrepare.disabled,true);assert.equal(h.storage.size,0);});
test('missing or failed search is not replaced with a fake answer',async()=>{const h=await harness();h.nodes.wikiSearchInput.value='모르는 내용';h.set(()=>({results:[]}));await h.nodes.wikiSearchButton.events.click();assert.equal(h.nodes.wikiResults.hidden,true);assert.match(h.nodes.wikiNotice.textContent,/찾지 못/);h.set(()=>({status:503,error:'private-exception'}));await h.nodes.wikiSearchButton.events.click();assert.doesNotMatch(h.nodes.wikiNotice.textContent,/private-exception/);});
test('markup includes required safe asset references and script never assigns HTML',()=>{const html=fs.readFileSync('frontend/wiki/index.html','utf8');assert.match(html,/\/assets\/wiki\/wiki.js/);assert.doesNotMatch(html,/<script(?![^>]*src=)[^>]*>/);assert.doesNotMatch(fs.readFileSync('frontend/wiki/wiki.js','utf8'),/\.innerHTML\s*=/);});


test('embedded knowledge uses the validated main connection without exchanging a second code',async()=>{
  const h=await harness({embedded:true});
  assert.equal(h.requests.length,0);assert.equal(h.nodes.wikiPrepare.disabled,true);
  h.publish('x'.repeat(43));assert.equal(h.nodes.wikiPrepare.disabled,false);
  h.nodes.wikiSourceTitle.value='자료';h.nodes.wikiSourceText.value='원문';h.set(()=>candidate);
  await h.nodes.wikiPrepare.events.click();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].path,'/api/wiki/prepare');
  h.publish(null);assert.equal(h.nodes.wikiPrepare.disabled,true);assert.equal(h.nodes.wikiCandidate.hidden,true);
  assert.equal(h.nodes.wikiSourceText.value,'원문');
});

test('embedded disconnect forgets shared connection and rejects an in-flight old proposal',async()=>{
  const h=await harness({embedded:true}),pending=defer();h.publish('x'.repeat(43));
  h.nodes.wikiSourceTitle.value='자료';h.nodes.wikiSourceText.value='원문';h.set(()=>pending.promise);
  const running=h.nodes.wikiPrepare.events.click();await tick();h.nodes.wikiDisconnect.events.click();
  pending.resolve(candidate);await running;
  assert.equal(h.nodes.wikiCandidate.hidden,true);assert.equal(h.nodes.wikiPrepare.disabled,true);
  assert.equal(h.nodes.wikiSourceText.value,'원문');
});

// ---- withdrawal ---------------------------------------------------------------------------
const wid='a'.repeat(32);
const owned={path:'JARVIS-Vault/JarvisWiki/'+wid+'.md',line:1,excerpt:'비공개 발췌',source_sha256:'f'.repeat(64),metadata:{id:'wiki-'+wid,source:'jarvis-wiki:'+wid,title:'자료'}};
const ordinary={path:'JARVIS-Vault/garden.md',line:1,excerpt:'토마토',source_sha256:'f'.repeat(64),metadata:{id:'note-1',source:'manual',title:'Garden'}};
const spoofs=[{...ordinary,metadata:{...owned.metadata}},{...owned,metadata:{...owned.metadata,id:'note-2'}},{...owned,metadata:{...owned.metadata,source:'manual'}}];
const publication={source_id:wid,note_id:'wiki-'+wid,candidate_id:'b'.repeat(32),digest:'d'.repeat(64),file_sha256:'c'.repeat(64),status:'approved',withdrawn:false};
const withdrawn={candidate_id:'b'.repeat(32),status:'withdrawn',source_id:wid,digest:'d'.repeat(64),file_sha256:'c'.repeat(64),withdrawn:true,indexed:false};
const buttons=node=>node.children.filter(child=>child.tagName==='BUTTON');
const posts=h=>h.requests.filter(r=>r.path==='/api/wiki/withdraw');
const route=(overrides={})=>p=>p in overrides?overrides[p]:p.endsWith('/search')?{results:[owned,ordinary,...spoofs]}:p.endsWith('/publication')?publication:p.endsWith('/withdraw')?withdrawn:{};
async function searched(h){h.nodes.wikiSearchInput.value='자료';await h.nodes.wikiSearchButton.events.click();return h.nodes.wikiResults.children;}

test('only a canonical wiki search hit gets a withdraw button; cancel sends no mutation',async()=>{
  const h=await harness();h.set(route());const items=await searched(h);
  assert.deepEqual(items.map(item=>buttons(item).length),[1,0,0,0,0]);
  h.answer(false);await buttons(items[0])[0].events.click();
  assert.equal(h.confirms.length,1);assert.match(h.confirms[0],/검색/);assert.match(h.confirms[0],/지우거나 고치지 않아요/);
  assert.equal(h.requests.filter(r=>r.path==='/api/wiki/sources/'+wid+'/publication').length,1);
  assert.equal(posts(h).length,0);assert.match(h.nodes.wikiNotice.textContent,/취소/);
  assert.equal(h.nodes.wikiResults.children.length,5);
});

test('confirmed withdrawal posts the exact looked-up identity and clears displayed excerpts',async()=>{
  const h=await harness();h.set(route());const items=await searched(h);
  await buttons(items[0])[0].events.click();
  assert.equal(posts(h).length,1);
  assert.deepEqual(JSON.parse(posts(h)[0].opts.body),{candidate_id:publication.candidate_id,digest:publication.digest,expected_file_sha256:publication.file_sha256});
  assert.equal(h.nodes.wikiResults.children.length,0);assert.equal(h.nodes.wikiResults.hidden,true);
  assert.match(h.nodes.wikiNotice.textContent,/제외했어요/);assert.doesNotMatch(h.nodes.wikiNotice.textContent,/비공개 발췌/);
});

test('approved candidate withdraws once, stays withdrawn and is never re-approved automatically',async()=>{
  const h=await harness();h.nodes.wikiSourceTitle.value='자료';h.nodes.wikiSourceText.value='로컬 Ollama';
  h.set(route({'/api/wiki/prepare':candidate,'/api/wiki/approve':{indexed:true}}));
  await h.nodes.wikiPrepare.events.click();await h.nodes.wikiPrepare.events.click();
  const [button]=buttons(h.nodes.wikiCandidate);assert.equal(buttons(h.nodes.wikiCandidate).length,1);
  assert.equal(button.hidden,true);
  await h.nodes.wikiApprove.events.click();assert.equal(button.hidden,false);assert.equal(button.disabled,false);
  const before=h.requests.length;await button.events.click();
  assert.deepEqual(h.requests.slice(before).map(r=>r.path),['/api/wiki/sources/'+wid+'/publication','/api/wiki/withdraw']);
  assert.equal(h.nodes.wikiReviewState.textContent,'검색에서 제외됨');
  assert.equal(h.nodes.wikiApprove.disabled,true);assert.equal(button.hidden,true);
  await button.events.click();assert.equal(posts(h).length,1);
});

test('failed or malformed withdrawal is not reported as success and is not retried',async()=>{
  for(const reply of [{status:400,error:'wiki_request_refused'},{status:503,error:'private-detail'},{...withdrawn,withdrawn:false}]){
    const h=await harness();h.set(route({'/api/wiki/withdraw':reply}));const items=await searched(h);
    await buttons(items[0])[0].events.click();
    assert.equal(posts(h).length,1);assert.match(h.nodes.wikiNotice.textContent,/확인하지 못했어요/);
    assert.doesNotMatch(h.nodes.wikiNotice.textContent,/제외했어요|private-detail/);
  }
});

test('late lookup or withdrawal reply after a connection change is never applied',async()=>{
  const lookup=defer(),h=await harness();h.set(route());const items=await searched(h);
  h.set(route({['/api/wiki/sources/'+wid+'/publication']:lookup.promise}));
  const running=buttons(items[0])[0].events.click();await tick();h.nodes.wikiDisconnect.events.click();
  lookup.resolve(publication);await running;
  assert.equal(h.confirms.length,0);assert.equal(posts(h).length,0);

  const late=defer(),e=await harness({embedded:true});e.publish('x'.repeat(43));
  e.nodes.wikiSourceTitle.value='자료';e.nodes.wikiSourceText.value='원문';
  e.set(route({'/api/wiki/prepare':{...candidate,status:'approved'}}));await e.nodes.wikiPrepare.events.click();
  e.set(route({'/api/wiki/prepare':{...candidate,status:'approved'},'/api/wiki/withdraw':late.promise}));
  const pending=buttons(e.nodes.wikiCandidate)[0].events.click();await tick();await tick();
  assert.equal(posts(e).length,1);
  e.publish('y'.repeat(43));late.resolve(withdrawn);await pending;
  assert.equal(posts(e).length,1);assert.doesNotMatch(e.nodes.wikiNotice.textContent,/제외했어요/);
  assert.equal(e.nodes.wikiCandidate.hidden,true);assert.notEqual(e.nodes.wikiReviewState.textContent,'검색에서 제외됨');
});
