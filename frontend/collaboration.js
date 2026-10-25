'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const statuses = {open:'담당 없음', claimed:'맡음', blocked:'막힘', review:'검토 요청', done:'완료', cancelled:'취소'};
  const rosterStatuses={active:'정식',probation:'수습',candidate:'후보',retired:'퇴직',disabled:'사용 중지'};
  let token=null, generation=0, busy=false, controller=null, fingerprint=null;
  const node=(tag, cls, text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  // One-line summary for a collapsed entry: drop a short leading [tag], squeeze whitespace, keep the first sentence or line.
  const preview=text=>{const raw=String(text||'');const flat=raw.replace(/^\s*\[[^\]\n]{1,24}\]\s*/,'').trim()?raw.replace(/^\s*\[[^\]\n]{1,24}\]\s*/,''):raw;const first=(flat.split(/\n\s*\n|\n/).find(line=>line.trim())||'').replace(/\s+/g,' ').trim();const sentence=(first.match(/^.*?[.!?。](?=\s|$)/)||[first])[0];const chars=Array.from(sentence.length>=20?sentence:first);return chars.length>90?chars.slice(0,89).join('').trimEnd()+'…':chars.join('');};
  const time=value=>Number.isFinite(value)?new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value*1000)):'기록 없음';
  function clear(){fingerprint=null;runFingerprint=null;sessionFingerprint=null;$('sessionNotice').textContent='';$('activityNotice').textContent='';for(const id of ['collaborationAgents','collaborationMessages','collaborationNotes','activityRuns','sessionCards'])$(id).replaceChildren();$('collaborationNotice').textContent='';$('collaborationUpdated').textContent='조회 대기';}
  function render(data){
    if(data.schema_version!==1||data.source!=='self_reported'||data.read_only!==true||!Array.isArray(data.agents)||!Array.isArray(data.tasks)||!Array.isArray(data.timeline))throw new Error('invalid_snapshot');
    $('collaborationUpdated').textContent=time(data.observed_at)+' 갱신';
    $('collaborationNotice').textContent=!data.board_available?'게시판 기록이 아직 없어요.':data.has_more?.tasks||data.has_more?.timeline?'최근 기록 일부만 보여요.':'';
    const nextFingerprint=JSON.stringify([data.board_available,data.agents,data.tasks,data.timeline,data.has_more]);
    if(nextFingerprint===fingerprint)return;
    fingerprint=nextFingerprint;
    $('collaborationAgents').replaceChildren();
    for(const agent of data.agents){
      const card=node('article','collaboration-agent');
      const heading=node('div','collaboration-agent-heading');
      heading.append(node('h3','',agent.label||agent.id));card.append(heading);
      card.append(node('p','muted small',(rosterStatuses[agent.roster_status]||'등록 상태 알 수 없음')+' · 마지막 기록 '+time(agent.last_activity)));
      const tasks=data.tasks.filter(task=>['claimed','blocked','review'].includes(task.status)&&typeof task.owner==='string'&&(task.owner===agent.id||task.owner.startsWith(agent.id+':')));
      for(const task of tasks.slice(0,3)){
        const row=node('div','collaboration-task');
        row.append(node('span',task.stale?'collaboration-warning':'muted small',task.stale?'기한 지남 · 확인 필요':statuses[task.status]||'확인 필요'),node('p','',task.title));
        card.append(row);
      }
      if(!tasks.length)card.append(node('p','muted small',agent.claimed_count?'맡은 일이 더 있어요.':'맡은 일 없음'));
      const visibleClaims=tasks.slice(0,3).filter(task=>task.status==='claimed').length;
      if(agent.claimed_count>visibleClaims)card.append(node('p','muted small','맡은 일 '+(agent.claimed_count-visibleClaims)+'건 더'));
      $('collaborationAgents').append(card);
    }
    for(const [kind,id] of [['message','collaborationMessages'],['note','collaborationNotes']]){
      const feed=$(id),scroll=feed.scrollTop,opened=new Set([...feed.querySelectorAll('details[open]')].map(item=>item.dataset.recordId));feed.replaceChildren();
      const entries=data.timeline.filter(item=>item.kind===kind);
      for(const item of entries.slice(0,12)){
        const row=node('details','collaboration-entry');
        row.dataset.recordId=item.id;row.open=opened.has(item.id);
        const summary=node('summary','',time(item.created)+' · '+item.from+(item.to?' → '+(item.to==='*'?'모두':item.to):' · 작업 기록'));
        summary.append(node('span','collaboration-preview',preview(item.text)));
        row.append(summary,node('p','collaboration-body',item.text||''));
        if(Array.isArray(item.readers)&&item.readers.length)row.append(node('p','muted small','읽음: '+item.readers.join(', ')));
        feed.append(row);
      }
      if(!entries.length)feed.append(node('p','muted small','최근 기록이 없어요.'));
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
    }catch(error){if(selected===token&&version===generation){$('collaborationNotice').textContent='새로 가져오지 못했어요. 아래는 이전 기록이에요.';$('collaborationUpdated').textContent='갱신 멈춤';}}
    finally{clearTimeout(timeout);controller=null;busy=false;}
  }
  let runBusy=false,runController=null,runFingerprint=null;
  async function loadRuns(){
    if(!token||document.hidden||!$('collaborationPanel').open||runBusy)return;
    runBusy=true;const selected=token,version=generation;runController=new AbortController();
    const timeout=setTimeout(()=>runController?.abort(),8000);
    try{
      const response=await fetch('/api/activity',{headers:{Authorization:'Bearer '+selected},cache:'no-store',credentials:'omit',signal:runController.signal});
      if(selected!==token||version!==generation)return;
      if(!response.ok)throw new Error('unavailable');
      const data=await response.json();if(selected!==token||version!==generation)return;
      if(!Array.isArray(data.runs)||data.control_supported!==false)throw new Error('invalid_activity');
      $('activityNotice').textContent=time(data.observed_at)+' 갱신'+(data.has_more?' · 일부만 보여요':'');
      const next=JSON.stringify(data.runs);if(next===runFingerprint)return;runFingerprint=next;
      const list=$('activityRuns');list.replaceChildren();
      const heartbeatLabels={recent:'소식 받음',delayed:'소식 늦음',unknown:'소식 알 수 없음'};
      const processLabels={running:'실행 중',exited:'끝남',identity_mismatch:'다른 프로그램일 수 있음',unknown:'실행 여부 알 수 없음'};
      const phaseLabels={queued:'대기',working:'작업 중',review:'검토 중',summarizing:'정리 중',finished:'마무리'};
      const kindLabels={start:'시작',heartbeat:'살아 있음',progress:'진행',result_received:'결과 받음',completed:'완료',failed:'실패'};
      for(const run of data.runs.slice(0,20)){
        const card=node('article','collaboration-agent');
        card.append(node('h3','',run.owner+' · 실행 '+String(run.run_id).slice(0,8)));
        card.append(node('p','muted small',(processLabels[run.process?.status]||'실행 여부 알 수 없음')+' · '+(heartbeatLabels[run.heartbeat?.status]||'소식 알 수 없음')));
        const event=run.report?.event;
        card.append(node('p','',event?'직원이 알림: '+(phaseLabels[event.phase]||'단계 알 수 없음')+' · '+(kindLabels[event.kind]||'종류 알 수 없음'):'직원이 알린 내용 없음'));
        card.append(node('p','muted small',Array.isArray(run.verified)&&run.verified.length?'검증 기록 있음':'검증 기록 없음'));
        if(run.fenced)card.append(node('p','collaboration-warning','이전 실행이에요. 지금 담당에는 반영하지 않아요.'));
        if(run.sequence_gap)card.append(node('p','collaboration-warning','중간 기록이 빠졌어요.'));
        list.append(card);
      }
      if(!data.runs.length)list.append(node('p','muted small','여기서 시작한 실행이 없어요.'));
      if(data.runs.length>20)list.append(node('p','muted small','실행 '+(data.runs.length-20)+'건 더 있어요.'));
    }catch(error){if(selected===token&&version===generation)$('activityNotice').textContent='새로 가져오지 못했어요. 아래는 이전 기록이에요.';}
    finally{clearTimeout(timeout);runController=null;runBusy=false;}
  }
  let sessionBusy=false,sessionController=null,sessionFingerprint=null;
  async function loadSessions(){
    if(!token||document.hidden||!$('collaborationPanel').open||sessionBusy)return;
    sessionBusy=true;const selected=token,version=generation;sessionController=new AbortController();
    const timeout=setTimeout(()=>sessionController?.abort(),8000);
    try{
      const response=await fetch('/api/sessions',{headers:{Authorization:'Bearer '+selected},cache:'no-store',credentials:'omit',signal:sessionController.signal});
      if(selected!==token||version!==generation)return;
      if(!response.ok)throw new Error('unavailable');
      const data=await response.json();if(selected!==token||version!==generation)return;
      if(data.schema_version!==1||data.source!=='self_reported'||data.read_only!==true||data.identity_verified!==false||!Array.isArray(data.sessions))throw new Error('invalid_sessions');
      const capacityWarning=(data.capacity?.events?.reached?' 저장 공간이 가득 차서 새 소식을 저장하지 않아요.':'')+(data.capacity?.sessions?.reached?' 세션 수가 한도에 닿아 새 세션을 받지 않아요.':'');
      $('sessionNotice').textContent=time(data.observed_at)+' 갱신'+(capacityWarning?' ·'+capacityWarning:'');
      const next=JSON.stringify(data.sessions);if(next===sessionFingerprint)return;sessionFingerprint=next;
      const list=$('sessionCards');list.replaceChildren();
      const labels={unknown:'알 수 없음',started:'세션 시작',turn_started:'답변 중',tool_started:'도구 사용 중',tool_finished:'도구 사용 끝',permission_requested:'승인 기다림',turn_stopped:'답변 끝',interrupted:'중단됨',ended:'세션 끝'};
      const receipts={recent:'최근 소식',old:'소식이 오래됨',clock_unknown:'시각 알 수 없음'};
      for(const session of data.sessions.slice(0,20)){
        const card=node('article','collaboration-agent');
        card.append(node('h3','',session.employee+' · '+String(session.session_id).slice(0,12)));
        card.append(node('p','',labels[session.state]||'알 수 없음'));
        card.append(node('p','muted small',(receipts[session.receipt?.status]||'소식 알 수 없음')+' · '+time(session.receipt?.received_at)));
        list.append(card);
      }
      if(!data.sessions.length)list.append(node('p','muted small','아직 받은 소식이 없어요. 훅 설치나 세션 재시작이 필요할 수 있어요.'));
      if(data.sessions.length>20)list.append(node('p','muted small','세션 '+(data.sessions.length-20)+'개 더 있어요.'));
    }catch(error){if(selected===token&&version===generation)$('sessionNotice').textContent='새로 가져오지 못했어요. 아래는 이전 기록이에요.';}
    finally{clearTimeout(timeout);sessionController=null;sessionBusy=false;}
  }
  function refresh(){load();loadRuns();loadSessions();}
  window.addEventListener('jarvis-connection',event=>{
    const next=event.detail.token||null;
    if(next!==token){generation++;controller?.abort();runController?.abort();sessionController?.abort();clear();token=next;}
    $('collaborationPanel').hidden=!token;if(token)refresh();
  });
  $('collaborationPanel').addEventListener('toggle',refresh);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(refresh,5000);
})();
