'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const statuses = {open:'미배정', claimed:'담당 배정됨', blocked:'막힘 보고', review:'검토 요청', done:'완료 보고', cancelled:'취소 보고'};
  const rosterStatuses={active:'정식',probation:'수습',candidate:'후보',retired:'퇴직',disabled:'사용 중지'};
  let token=null, generation=0, busy=false, controller=null, fingerprint=null;
  const node=(tag, cls, text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const time=value=>Number.isFinite(value)?new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value*1000)):'기록 없음';
  function clear(){fingerprint=null;for(const id of ['collaborationAgents','collaborationMessages','collaborationNotes'])$(id).replaceChildren();$('collaborationNotice').textContent='';$('collaborationUpdated').textContent='조회 대기';}
  function render(data){
    if(data.schema_version!==1||data.source!=='self_reported'||data.read_only!==true||!Array.isArray(data.agents)||!Array.isArray(data.tasks)||!Array.isArray(data.timeline))throw new Error('invalid_snapshot');
    $('collaborationUpdated').textContent='최근 조회 '+time(data.observed_at);
    $('collaborationNotice').textContent=!data.board_available?'게시판 기록이 아직 없습니다.':data.has_more?.tasks||data.has_more?.timeline?'최근 기록 일부를 표시합니다.':'자기 보고를 기준으로 표시합니다. 프로세스 관찰과 검증 결과는 별도입니다.';
    const nextFingerprint=JSON.stringify([data.board_available,data.agents,data.tasks,data.timeline,data.has_more]);
    if(nextFingerprint===fingerprint)return;
    fingerprint=nextFingerprint;
    $('collaborationAgents').replaceChildren();
    for(const agent of data.agents){
      const card=node('article','collaboration-agent');
      const heading=node('div','collaboration-agent-heading');
      heading.append(node('h3','',agent.label||agent.id),node('span','count','자기 보고'));card.append(heading);
      card.append(node('p','muted small','직원 등록: '+(rosterStatuses[agent.roster_status]||'상태 미확인')));
      card.append(node('p','muted small','연결 상태 미확인 · 최근 기록 '+time(agent.last_activity)));
      const tasks=data.tasks.filter(task=>['claimed','blocked','review'].includes(task.status)&&typeof task.owner==='string'&&(task.owner===agent.id||task.owner.startsWith(agent.id+':')));
      for(const task of tasks.slice(0,3)){
        const row=node('div','collaboration-task');
        row.append(node('span',task.stale?'collaboration-warning':'muted small',task.stale?'담당 기한 경과 · 상태 확인 필요':statuses[task.status]||'상태 확인 필요'),node('p','',task.title));
        card.append(row);
      }
      if(!tasks.length)card.append(node('p','muted small',agent.claimed_count?'표시 범위 밖에 담당 기록이 있습니다.':'표시할 담당 작업 없음 · 유휴 여부는 미확인'));
      const visibleClaims=tasks.slice(0,3).filter(task=>task.status==='claimed').length;
      if(agent.claimed_count>visibleClaims)card.append(node('p','muted small','추가 배정 기록 '+(agent.claimed_count-visibleClaims)+'건'));
      $('collaborationAgents').append(card);
    }
    for(const [kind,id] of [['message','collaborationMessages'],['note','collaborationNotes']]){
      const feed=$(id),scroll=feed.scrollTop,opened=new Set([...feed.querySelectorAll('details[open]')].map(item=>item.dataset.recordId));feed.replaceChildren();
      const entries=data.timeline.filter(item=>item.kind===kind);
      for(const item of entries.slice(0,12)){
        const row=node('details','collaboration-entry');
        row.dataset.recordId=item.id;row.open=opened.has(item.id);
        const summary=node('summary','',time(item.created)+' · '+item.from+(item.to?' → '+(item.to==='*'?'모두':item.to):' · 작업 기록'));
        summary.append(node('span','collaboration-preview',String(item.text||'').slice(0,140)));
        row.append(summary,node('p','collaboration-body',item.text||''));
        if(Array.isArray(item.readers)&&item.readers.length)row.append(node('p','muted small','우편함 조회: '+item.readers.join(', ')+' · 동의·착수 확인은 아닙니다.'));
        feed.append(row);
      }
      if(!entries.length)feed.append(node('p','muted small','표시할 최근 기록이 없습니다.'));
      feed.scrollTop=scroll;
    }
  }
  async function load(){
    if(!token||document.hidden||!$('collaborationPanel').open||busy)return;
    busy=true;const selected=token,version=generation;controller=new AbortController();
    const timeout=setTimeout(()=>controller?.abort(),8000);
    try{
      const response=await fetch('/api/collaboration',{headers:{Authorization:'Bearer '+selected},cache:'no-store',credentials:'omit',signal:controller.signal});
      if(selected!==token||version!==generation)return;
      if(!response.ok)throw new Error('unavailable');
      const data=await response.json();if(selected!==token||version!==generation)return;
      render(data);
    }catch(error){if(selected===token&&version===generation){$('collaborationNotice').textContent='갱신하지 못했습니다. 이전 표시가 있다면 과거 기록입니다. 현재 상태는 미확인입니다.';$('collaborationUpdated').textContent='갱신 중단';}}
    finally{clearTimeout(timeout);controller=null;busy=false;}
  }
  window.addEventListener('jarvis-connection',event=>{
    const next=event.detail.token||null;
    if(next!==token){generation++;controller?.abort();clear();token=next;}
    $('collaborationPanel').hidden=!token;if(token)load();
  });
  $('collaborationPanel').addEventListener('toggle',load);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
  setInterval(load,5000);
})();
