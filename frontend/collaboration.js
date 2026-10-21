'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const statuses = {open:'미배정', claimed:'담당 배정됨', blocked:'막힘 보고', review:'검토 요청', done:'완료 보고', cancelled:'취소 보고'};
  const rosterStatuses={active:'정식',probation:'수습',candidate:'후보',retired:'퇴직',disabled:'사용 중지'};
  let token=null, generation=0, busy=false, controller=null, fingerprint=null;
  const node=(tag, cls, text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const time=value=>Number.isFinite(value)?new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value*1000)):'기록 없음';
  function clear(){fingerprint=null;runFingerprint=null;sessionFingerprint=null;$('sessionNotice').textContent='';$('activityNotice').textContent='';for(const id of ['collaborationAgents','collaborationMessages','collaborationNotes','activityRuns','sessionCards'])$(id).replaceChildren();$('collaborationNotice').textContent='';$('collaborationUpdated').textContent='조회 대기';}
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
      $('activityNotice').textContent='프로세스와 보고 수신은 직접 관찰, 작업 단계는 자기 보고입니다. 작업 성공·검증 통과와 별개입니다. 최근 조회 '+time(data.observed_at)+(data.has_more?' · 일부 실행만 표시':'');
      const next=JSON.stringify(data.runs);if(next===runFingerprint)return;runFingerprint=next;
      const list=$('activityRuns');list.replaceChildren();
      const heartbeatLabels={recent:'최근 보고 수신',delayed:'보고 지연 · 연결 미확인',unknown:'보고 수신 상태 미확인'};
      const processLabels={running:'프로세스 생존 관찰',exited:'프로세스 종료 관찰',identity_mismatch:'프로세스 신원 불일치',unknown:'프로세스 확인 불가'};
      const phaseLabels={queued:'대기 보고',working:'작업 중 보고',review:'검토 중 보고',summarizing:'정리 중 보고',finished:'종료 단계 보고'};
      const kindLabels={start:'시작 보고',heartbeat:'생존 신호',progress:'진행 보고',result_received:'결과 수신 보고',completed:'실행 완료 보고',failed:'실패 보고'};
      for(const run of data.runs.slice(0,20)){
        const card=node('article','collaboration-agent');
        card.append(node('h3','',run.owner+' · 실행 '+String(run.run_id).slice(0,8)));
        card.append(node('p','muted small','직접 관찰: '+(heartbeatLabels[run.heartbeat?.status]||'확인 불가')+' · '+(processLabels[run.process?.status]||'확인 불가')));
        const event=run.report?.event;
        card.append(node('p','',event?'자기 보고: '+(phaseLabels[event.phase]||'단계 미확인')+' / '+(kindLabels[event.kind]||'종류 미확인'):'자기 보고 없음'));
        card.append(node('p','muted small','검증: '+(Array.isArray(run.verified)&&run.verified.length?'별도 검증 기록 있음':'검증 근거 없음')));
        if(run.fenced)card.append(node('p','collaboration-warning','이전 실행 · 새로운 담당 세대에 보고를 반영하지 않습니다.'));
        if(run.sequence_gap)card.append(node('p','collaboration-warning','보고 순번 누락 · 일부 기록 미확인'));
        list.append(card);
      }
      if(!data.runs.length)list.append(node('p','muted small','등록된 실행이 없습니다. 직원이 쉬고 있다는 뜻은 아닙니다.'));
      if(data.runs.length>20)list.append(node('p','muted small','추가 실행 '+(data.runs.length-20)+'건은 표시 범위 밖입니다.'));
    }catch(error){if(selected===token&&version===generation)$('activityNotice').textContent='실행 관찰을 갱신하지 못했습니다. 이전 표시가 있다면 과거 기록이며, 현재 상태는 미확인입니다.';}
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
      $('sessionNotice').textContent='플랫폼 접점의 자기 보고 · 신원과 모델 내부 상태를 확인한 정보가 아닙니다. 수신 시각만 관찰합니다. 최근 조회 '+time(data.observed_at);
      const next=JSON.stringify(data.sessions);if(next===sessionFingerprint)return;sessionFingerprint=next;
      const list=$('sessionCards');list.replaceChildren();
      const labels={unknown:'상태 미확인',started:'세션 시작 보고',turn_started:'응답 시작 보고',tool_started:'도구 시작 보고',tool_finished:'도구 종료 보고',permission_requested:'승인 대기 보고',turn_stopped:'응답 종료 보고',interrupted:'중단 보고',ended:'세션 종료 보고'};
      const receipts={recent:'최근 보고 수신',old:'최근 이벤트가 오래됨 · 실행 여부는 미확인',clock_unknown:'시각 확인 불가'};
      for(const session of data.sessions.slice(0,20)){
        const card=node('article','collaboration-agent');
        card.append(node('h3','',session.employee+' · '+String(session.session_id).slice(0,12)));
        card.append(node('p','',labels[session.state]||'상태 미확인'));
        card.append(node('p','muted small','수신 관찰: '+(receipts[session.receipt?.status]||'확인 불가')+' · '+time(session.receipt?.received_at)));
        card.append(node('p','muted small','연결 상태 미확인 · 작업 성공·검증 통과 판정 아님'));
        list.append(card);
      }
      if(!data.sessions.length)list.append(node('p','muted small','아직 수신한 세션 이벤트가 없습니다. Hook 설치·신뢰 승인·세션 재시작이 필요할 수 있습니다. 직원이 쉬고 있다는 뜻은 아닙니다.'));
      if(data.sessions.length>20)list.append(node('p','muted small','추가 세션 '+(data.sessions.length-20)+'개는 표시 범위 밖입니다.'));
    }catch(error){if(selected===token&&version===generation)$('sessionNotice').textContent='외부 세션 보고를 갱신하지 못했습니다. 이전 표시는 과거 기록이며 현재 상태는 미확인입니다.';}
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
