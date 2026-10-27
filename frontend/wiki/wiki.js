'use strict';
(() => {
  const byId = id => document.getElementById(id), KEY = 'jarvis-hud-connection';
  const embedded = Boolean(byId('knowledgeSpace'));
  let token = null, generation = 0, busy = false, candidate = null, requestId = null;
  const errors = {connection_required:'연결이 만료됐어요. 자비스에서 새 연결 링크를 열어주세요.',
    wiki_preparation_failed:'초안을 만들지 못했어요. 입력은 남겨뒀어요. 다시 초안 만들기를 누르면 새 요청을 시작해요.',
    wiki_preparation_unknown:'이전 요청의 완료 여부를 확인하지 못했어요. 중복 호출을 피하려고 새로 실행하지 않아요.',
    wiki_busy:'다른 초안을 준비 중이에요. 자동으로 다시 실행하지 않아요.',
    wiki_request_refused:'자료나 변경안이 현재 상태와 맞지 않아요. 원본과 내용을 확인해주세요.',
    wiki_unavailable:'위키 작업을 완료하지 못했어요. 저장/호출 상태를 확인한 뒤 새로 요청해주세요.',
    origin_denied:'이 주소에서는 연결할 수 없어요.', json_required:'요청 형식을 확인해주세요.'};
  const notice = text => {byId('wikiNotice').textContent = text;byId('wikiNotice').hidden=!text;};
  const element = (tag,text) => {const node=document.createElement(tag);node.textContent=text;return node;};
  function clearCandidate(){candidate=null;byId('wikiCandidate').hidden=true;byId('wikiFacts').replaceChildren();byId('wikiSummary').textContent='';byId('wikiSourceRef').textContent='';}
  function sync(){
    byId('wikiConnection').textContent=token?'자비스에 연결됨':'연결 전';
    for(const id of ['wikiPrepare','wikiSearchButton'])byId(id).disabled=!token||busy;
    for(const id of ['wikiSourceTitle','wikiSourceText','wikiSearchInput'])byId(id).disabled=busy;
    byId('wikiApprove').disabled=!token||busy||!candidate||candidate.status!=='proposed';
    byId('wikiDisconnect').disabled=!token;
    byId('wikiConnectButton').disabled=busy;
  }
  function disconnect(){generation++;token=null;candidate=null;requestId=null;busy=false;try{sessionStorage.removeItem(KEY);}catch(_){}clearCandidate();if(embedded)window.JarvisConnection.forget();byId('wikiResults').replaceChildren();byId('wikiResults').hidden=true;sync();}
  async function api(path,data){
    const version=generation, selected=token;
    const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+selected,...(data===undefined?{}:{'Content-Type':'application/json'})},body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',credentials:'omit'});
    if(version!==generation||selected!==token)throw Error('연결이 변경됐어요. 이전 응답은 적용하지 않아요.');
    if(r.status===401){disconnect();throw Error(errors.connection_required);}
    let value;try{value=await r.json();}catch(_){throw Error('응답을 읽지 못했어요. 자동으로 다시 실행하지 않아요.');}
    if(version!==generation||selected!==token)throw Error('연결이 변경됐어요. 이전 응답은 적용하지 않아요.');
    if(!r.ok){if(path==='/api/wiki/prepare'&&value?.error==='wiki_preparation_failed'&&value?.retry==='manual_new_request')requestId=null;throw Error(errors[value?.error]||'요청을 완료하지 못했어요. 자동 재시도하지 않아요.');}
    return value;
  }
  async function connect(code){
    const version=++generation;token=null;busy=true;clearCandidate();sync();
    try{
      if(code){
        if(!/^[A-Za-z0-9_-]{43}$/.test(code))throw Error('연결 코드 형식을 확인해주세요.');
        const r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code}),cache:'no-store',credentials:'omit'});
        if(version!==generation)return;
        if(!r.ok)throw Error('연결 링크가 만료됐거나 사용됐어요. 자비스에서 새 링크를 열어주세요.');
        const value=await r.json();if(version!==generation)return;
        if(!/^[A-Za-z0-9_-]{43}$/.test(value.token||''))throw Error('연결 응답을 확인해주세요.');
        token=value.token;
      }else{try{token=sessionStorage.getItem(KEY);}catch(_){}if(!/^[A-Za-z0-9_-]{43}$/.test(token||'')){token=null;notice('자비스에서 이 화면의 연결 링크를 열어주세요. 연결 파일은 필요하지 않아요.');return;}}
      await api('/api/tasks');if(version!==generation)return;
      try{sessionStorage.setItem(KEY,token);}catch(_){}
      byId('wikiConnectCode').value='';notice('자료를 붙여 넣으면 로컬 AI가 검토할 초안을 만들어요.');
    }catch(error){if(version===generation){token=null;try{sessionStorage.removeItem(KEY);}catch(_){}notice(error.message);}}
    finally{if(version===generation){busy=false;sync();}}
  }
  async function run(message,work){if(!token||busy)return;const version=generation;busy=true;notice(message);sync();try{await work(version);}catch(error){if(version===generation)notice(error.message);}finally{if(version===generation){busy=false;sync();}}}
  byId('wikiConnectButton').addEventListener('click',()=>{if(!embedded)connect(byId('wikiConnectCode').value.trim());});
  byId('wikiDisconnect').addEventListener('click',()=>{disconnect();notice('연결을 해제했어요. 이미 시작한 서버 작업이 취소됐다는 뜻은 아니에요.');});
  for(const id of ['wikiSourceTitle','wikiSourceText'])byId(id).addEventListener('input',()=>{requestId=null;clearCandidate();sync();});
  byId('wikiPrepare').addEventListener('click',()=>run('로컬 AI가 초안을 준비하고 있어요. 아직 검색에 저장하지 않았어요.',async version=>{
    const title=byId('wikiSourceTitle').value.trim(),text=byId('wikiSourceText').value;
    if(!title||title.length>160||!text.trim()||text.length>16000)throw Error('제목은 160자, 자료는 16,000자 안에서 입력해주세요.');
    requestId=requestId||crypto.randomUUID().replaceAll('-','');
    const result=await api('/api/wiki/prepare',{request_id:requestId,title,text});if(version!==generation)return;
    candidate=result;if(byId('wikiReviewState'))byId('wikiReviewState').textContent=result.status==='approved'?'검토 후 저장됨':'아직 검색되지 않음';byId('wikiSummary').textContent=result.summary;
    byId('wikiSourceRef').textContent='원본 '+result.source.title+' · '+result.source.id+' · SHA256 '+result.source.sha256;
    byId('wikiFacts').replaceChildren(...result.facts.map(f=>{const item=element('article','');item.append(element('p',f.text),element('blockquote',f.quote),element('small','원본 '+f.line+'행'));return item;}));
    byId('wikiCandidate').hidden=false;notice(result.status==='approved'?'이미 저장한 변경안이에요. 아래에서 검색할 수 있어요.':'초안이에요. 요약·사실과 원문 인용이 맞는지 확인한 뒤 저장해주세요.');
  }));
  byId('wikiApprove').addEventListener('click',()=>run('확인한 변경안을 저장하고 검색 색인을 갱신하고 있어요.',async version=>{
    if(!candidate||candidate.status!=='proposed')return;
    const selected=candidate;
    const result=await api('/api/wiki/approve',{candidate_id:selected.id,digest:selected.digest});if(version!==generation)return;
    candidate={...selected,status:'approved'};if(byId('wikiReviewState'))byId('wikiReviewState').textContent=result.indexed?'검토 후 저장됨':'저장됨 · 검색 갱신 미완료';
    notice(result.indexed?'검토한 위키가 저장됐어요. 아래에서 질문의 핵심 단어로 찾아보세요.':'위키는 저장했지만 검색 갱신을 완료하지 못했어요. 새 초안을 만들지 말고 색인 상태를 확인해주세요.');
  }));
  byId('wikiSearchButton').addEventListener('click',()=>run('검토된 지식을 찾고 있어요.',async version=>{
    const query=byId('wikiSearchInput').value.trim();if(!query||query.length>300)throw Error('찾을 내용을 300자 안에서 입력해주세요.');
    const result=await api('/api/wiki/search',{query});if(version!==generation)return;
    byId('wikiResults').replaceChildren(...result.results.map(hit=>{const item=element('article','');item.append(element('h3',hit.metadata?.title||'검색 결과'),element('p',hit.excerpt),element('small',hit.path+' · '+hit.line+'행 · SHA256 '+hit.source_sha256));return item;}));
    byId('wikiResults').hidden=!result.results.length;
    notice(result.indexed===false?'검색 색인이 아직 없어요. 초안을 검토하고 저장한 뒤 검색해주세요.':result.results.length?'원문과 출처를 확인해주세요. 이 화면은 검색 결과이며 AI의 종합 답변은 아니에요.':'맞는 승인 자료를 찾지 못했어요. 아직 없는 지식을 만들어 답하지 않아요.');
  }));
  window.addEventListener('pagehide',()=>{generation++;token=null;busy=false;candidate=null;});
  sync();
  if(embedded){
    window.JarvisConnection.subscribe(selected=>{
      if(selected===token)return;
      generation++;token=selected;busy=false;requestId=null;clearCandidate();
      byId('wikiResults').replaceChildren();byId('wikiResults').hidden=true;
      notice(token?'메인 화면의 연결로 지식을 정리할 수 있어요.':'메인 화면에서 자비스에 먼저 연결해주세요.');sync();
    });
  }else{
    const hash=new URLSearchParams(window.location.hash.slice(1)),code=hash.get('connect');
    if(code)history.replaceState(null,'',window.location.pathname+window.location.search);
    connect(code);
    window.addEventListener('pageshow',event=>{if(event.persisted)connect();});
  }
})();
