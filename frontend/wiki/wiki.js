'use strict';
(() => {
  const byId = id => document.getElementById(id), KEY = 'jarvis-hud-connection';
  const embedded = Boolean(byId('knowledgeSpace'));
  let token = null, generation = 0, busy = false, candidate = null, requestId = null, resultButtons = [];
  const HEX32=/^[0-9a-f]{32}$/, HEX64=/^[0-9a-f]{64}$/, OWNED=/^JARVIS-Vault\/JarvisWiki\/([0-9a-f]{32})\.md$/;
  const WITHDRAW_UNKNOWN='검색 제외가 완료됐는지 확인하지 못했어요. 자동으로 다시 시도하지 않아요. 게시 상태를 다시 확인해주세요.';
  const CONFIRM_WITHDRAW='이 위키 노트를 앞으로의 자비스 검색과 답변 문맥에서 제외할까요?\n\n원본 자료, 초안 기록, Vault 파일은 지우거나 고치지 않아요. 이미 받은 답변도 바뀌지 않아요.\n이 화면에서는 다시 승인할 수 없어요.';
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
  function clearResults(){resultButtons=[];byId('wikiResults').replaceChildren();byId('wikiResults').hidden=true;}
  // Created once; shown only for the approved candidate. The server lookup stays authoritative.
  const withdrawButton=element('button','이 위키를 검색에서 제외');withdrawButton.type='button';withdrawButton.className='button button-quiet';withdrawButton.hidden=true;
  byId('wikiCandidate').append(withdrawButton);
  function sync(){
    byId('wikiConnection').textContent=token?'자비스에 연결됨':'연결 전';
    for(const id of ['wikiPrepare','wikiSearchButton'])byId(id).disabled=!token||busy;
    for(const id of ['wikiSourceTitle','wikiSourceText','wikiSearchInput'])byId(id).disabled=busy;
    byId('wikiApprove').disabled=!token||busy||!candidate||candidate.status!=='proposed';
    withdrawButton.hidden=!candidate||candidate.status!=='approved'||!HEX32.test(candidate.source?.id||'');
    withdrawButton.disabled=!token||busy||withdrawButton.hidden;
    for(const button of resultButtons)button.disabled=!token||busy;
    byId('wikiDisconnect').disabled=!token;
    byId('wikiConnectButton').disabled=busy;
  }
  function disconnect(){generation++;token=null;candidate=null;requestId=null;busy=false;try{sessionStorage.removeItem(KEY);}catch(_){}clearCandidate();if(embedded)window.JarvisConnection.forget();clearResults();sync();}
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
  function markWithdrawn(identity){
    // Displayed excerpts may include the withdrawn note; never keep them on screen as current results.
    clearResults();
    if(candidate&&candidate.id===identity.candidate_id&&candidate.source?.id===identity.source_id){
      candidate={...candidate,status:'withdrawn'};if(byId('wikiReviewState'))byId('wikiReviewState').textContent='검색에서 제외됨';
    }
  }
  const withdraw=sourceId=>run('현재 게시 상태를 확인하고 있어요.',async version=>{
    if(!HEX32.test(sourceId))return;
    const state=await api('/api/wiki/sources/'+sourceId+'/publication');if(version!==generation)return;
    if(state?.source_id!==sourceId||!HEX32.test(state.candidate_id||'')||!HEX64.test(state.digest||'')||!HEX64.test(state.file_sha256||'')||typeof state.withdrawn!=='boolean')throw Error('게시 상태를 확인하지 못했어요. 바꾼 것은 없어요.');
    if(state.withdrawn){markWithdrawn(state);notice('이미 검색에서 제외된 위키예요. 원본과 파일은 그대로 남아 있어요.');return;}
    if(!window.confirm(CONFIRM_WITHDRAW)){if(version===generation)notice('취소했어요. 아무것도 바꾸지 않았어요.');return;}
    if(version!==generation)return;
    notice('검색에서 제외하고 있어요.');
    let result;
    try{result=await api('/api/wiki/withdraw',{candidate_id:state.candidate_id,digest:state.digest,expected_file_sha256:state.file_sha256});}
    catch(_){throw Error(WITHDRAW_UNKNOWN);}
    if(version!==generation)return;
    if(result?.withdrawn!==true||result.status!=='withdrawn'||result.source_id!==sourceId||result.candidate_id!==state.candidate_id)throw Error(WITHDRAW_UNKNOWN);
    markWithdrawn(result);
    notice('앞으로의 검색과 답변 문맥에서 제외했어요. 원본 자료와 Vault 파일은 지우지 않았어요. 표시하던 검색 결과는 지웠으니 다시 찾아보세요.');
  });
  withdrawButton.addEventListener('click',()=>candidate&&candidate.status==='approved'?withdraw(candidate.source.id):undefined);
  function ownedSource(hit){
    // Only a canonical wiki export gets the button; ordinary Vault notes never do.
    const match=OWNED.exec(typeof hit?.path==='string'?hit.path:'');
    return match&&hit.metadata?.id==='wiki-'+match[1]&&hit.metadata?.source==='jarvis-wiki:'+match[1]?match[1]:null;
  }
  byId('wikiConnectButton').addEventListener('click',()=>{if(!embedded)connect(byId('wikiConnectCode').value.trim());});
  byId('wikiDisconnect').addEventListener('click',()=>{disconnect();notice('연결을 해제했어요. 이미 시작한 서버 작업이 취소됐다는 뜻은 아니에요.');});
  for(const id of ['wikiSourceTitle','wikiSourceText'])byId(id).addEventListener('input',()=>{requestId=null;clearCandidate();sync();});
  byId('wikiPrepare').addEventListener('click',()=>run('로컬 AI가 초안을 준비하고 있어요. 아직 검색에 저장하지 않았어요.',async version=>{
    const title=byId('wikiSourceTitle').value.trim(),text=byId('wikiSourceText').value;
    if(!title||title.length>160||!text.trim()||text.length>16000)throw Error('제목은 160자, 자료는 16,000자 안에서 입력해주세요.');
    requestId=requestId||crypto.randomUUID().replaceAll('-','');
    const result=await api('/api/wiki/prepare',{request_id:requestId,title,text});if(version!==generation)return;
    candidate=result;if(byId('wikiReviewState'))byId('wikiReviewState').textContent=result.status==='approved'?'검토 후 저장됨':result.status==='withdrawn'?'검색에서 제외됨':'아직 검색되지 않음';byId('wikiSummary').textContent=result.summary;
    byId('wikiSourceRef').textContent='원본 '+result.source.title+' · '+result.source.id+' · SHA256 '+result.source.sha256;
    byId('wikiFacts').replaceChildren(...result.facts.map(f=>{const item=element('article','');item.append(element('p',f.text),element('blockquote',f.quote),element('small','원본 '+f.line+'행'));return item;}));
    byId('wikiCandidate').hidden=false;notice(result.status==='approved'?'이미 저장한 변경안이에요. 아래에서 검색할 수 있어요.':result.status==='withdrawn'?'검색에서 제외한 변경안이에요. 이 화면에서는 다시 승인하지 않아요.':'초안이에요. 요약·사실과 원문 인용이 맞는지 확인한 뒤 저장해주세요.');
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
    clearResults();
    byId('wikiResults').replaceChildren(...result.results.map(hit=>{const item=element('article','');item.append(element('h3',hit.metadata?.title||'검색 결과'),element('p',hit.excerpt),element('small',hit.path+' · '+hit.line+'행 · SHA256 '+hit.source_sha256));
      const owned=ownedSource(hit);
      if(owned){const button=element('button','이 위키를 검색에서 제외');button.type='button';button.className='button button-quiet';button.addEventListener('click',()=>withdraw(owned));resultButtons.push(button);item.append(button);}
      return item;}));
    byId('wikiResults').hidden=!result.results.length;
    notice(result.indexed===false?'검색 색인이 아직 없어요. 초안을 검토하고 저장한 뒤 검색해주세요.':result.results.length?'원문과 출처를 확인해주세요. 이 화면은 검색 결과이며 AI의 종합 답변은 아니에요.':'맞는 승인 자료를 찾지 못했어요. 아직 없는 지식을 만들어 답하지 않아요.');
  }));
  window.addEventListener('pagehide',()=>{generation++;token=null;busy=false;candidate=null;});
  sync();
  if(embedded){
    window.JarvisConnection.subscribe(selected=>{
      if(selected===token)return;
      generation++;token=selected;busy=false;requestId=null;clearCandidate();clearResults();
      notice(token?'메인 화면의 연결로 지식을 정리할 수 있어요.':'메인 화면에서 자비스에 먼저 연결해주세요.');sync();
    });
  }else{
    const hash=new URLSearchParams(window.location.hash.slice(1)),code=hash.get('connect');
    if(code)history.replaceState(null,'',window.location.pathname+window.location.search);
    connect(code);
    window.addEventListener('pageshow',event=>{if(event.persisted)connect();});
  }
})();
