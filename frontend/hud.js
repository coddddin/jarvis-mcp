'use strict';
const $ = id => document.getElementById(id);
const state = {token:null, jobs:[], selected:null, demo:false, generation:0, cursors:new Map(), feeds:new Map(), recovery:new Set(), timings:new JarvisDisplayTimings()};
const stream=new JarvisTaskStream({onEvent:receiveEvent,onState:streamState,onRecovery:streamRecovery});
const labels = {ready:'대기', running:'진행 중', completed:'완료', needs_user_input:'확인 필요', cancelled:'취소', timed_out:'시간 초과'};
const roles = {planner:'계획', coder:'코드 작성', qa:'검증'};
const terminal = status => ['completed','needs_user_input','cancelled','timed_out'].includes(status);
const stamp = seconds => new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(seconds*1000));
const number = value => new Intl.NumberFormat('ko-KR').format(value);
function element(tag, className, text){const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined)node.textContent=text; return node;}
function notice(message=''){ $('notice').textContent=message; $('notice').hidden=!message; }
let processPollBusy=false;
function processNotice(message=''){ $('processNotice').textContent=message; $('processNotice').hidden=!message; }
async function loadProcesses(){
  if(!state.token||state.demo||document.hidden||processPollBusy)return;
  const token=state.token; processPollBusy=true;
  try{
    const response=await fetch('/api/processes',{headers:{Authorization:'Bearer '+token},cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(10000)});
    if(state.token!==token||state.demo)return;
    if(response.status===401){disconnect();return;}
    if(!response.ok)throw new Error('monitor_unavailable');
    const data=await response.json();
    if(state.token!==token||state.demo)return;
    if(!Array.isArray(data.processes)||data.termination_supported!==false)throw new Error('invalid_monitor');
    const descriptions={parent_exited:'실행한 부모 프로그램 종료',parent_unverifiable:'부모 상태 확인 불가',runtime_exceeded:'설정된 실행 시간 초과',clock_regressed:'시각 확인 필요',process_unverifiable:'실행 상태 확인 불가',process_pid_reused:'프로세스 번호 재사용'};
    const warnings=data.processes.filter(item=>Array.isArray(item.warnings)&&item.warnings.length);
    const message=warnings.slice(0,3).map(item=>item.label+' (PID '+item.pid+'): '+item.warnings.map(code=>descriptions[code]||'상태 확인 필요').join(', ')).join(' · ');
    processNotice(warnings.length||data.has_more?'프로세스 확인: '+message+(warnings.length>3?' · 추가 경고 '+(warnings.length-3)+'개':'')+(data.has_more?' · 등록 목록 일부만 확인':'')+' · 자동 종료하지 않습니다.':'');
  }catch(error){if(state.token===token&&!state.demo)processNotice('프로세스 감시 상태를 가져오지 못했어요. 정상 여부는 확인되지 않았습니다.');}
  finally{processPollBusy=false;}
}
window.addEventListener('jarvis-connection',event=>{if(event.detail.token)loadProcesses();else processNotice();});
setInterval(loadProcesses,30000);
// Service roles use only existing read APIs. A missing observation is never an offline verdict.
const serviceOverview = (() => {
  const panel=element('section','collaboration-panel panel');
  panel.id='serviceOverview'; panel.setAttribute('aria-label','서비스별 역할과 확인 상태');
  panel.append(element('h2','','자비스가 일하려면'), element('p','muted small',
    'Ollama만 켜면 자동 업무가 시작되는 것은 아니에요. 모델 서버, 대화·작업 화면, 자동 배정은 각각 준비되어야 해요.'));
  const cards=element('div','collaboration-agents');
  function card(title, role, reason, action) {
    const box=element('article','collaboration-agent'), status=element('p','status','미확인');
    const detail=element('p','muted small',reason), next=element('p','small',action);
    box.append(element('h3','',title),element('p','muted small',role),status,detail,next); cards.append(box);
    return {status,detail,next};
  }
  const modelCard=card('Ollama · 모델 서버','로컬 AI가 답변을 만드는 데 필요해요.',
    '연결 전에는 모델 서버를 확인하지 않아요. 모델 이름이나 화면 연결만으로 실행 여부를 판단하지 않아요.',
    '다음: 연결 파일을 선택하면 모델 서버의 마지막 관찰 결과를 보여 드려요.');
  const hudCard=card('자비스 · 대화와 작업 화면','대화하고 작업 기록을 확인하는 창이에요.',
    '아직 작업 기록에 연결하지 않았어요. 채팅 응답 가능 여부는 별도로 확인해야 해요.',
    '다음: 자비스 화면을 실행한 뒤 연결 파일을 선택하세요.');
  const dispatchCard=card('디스패처 · 자동 배정','받은 일을 직원에게 자동으로 배정해요.',
    '연결 후 서비스 기록을 조회할 수 있어요. 설정의 켜짐과 실제 실행은 달라요.',
    '다음: 연결 후 자동 배정 서비스와 설정을 확인하세요.');
  panel.append(cards,element('p','muted small','표시된 상태는 마지막 확인 근거입니다. 직원 가용성, 작업 성공, 검증 통과를 보장하지 않아요.'));
  $('processNotice').after(panel);
  let token=null, version=0, pending=null;
  function unknown(reason) {
    dispatchCard.status.textContent='미확인'; dispatchCard.status.className='status';
    dispatchCard.detail.textContent=reason;
    dispatchCard.next.textContent='다음: 연결 파일과 자비스 실행 상태를 확인하고 새로고침하세요.';
  }
  async function refresh() {
    if(!token||document.hidden||pending)return;
    const selected=token, revision=version, request=new AbortController(); pending=request;
    unknown('서비스 기록을 확인하고 있어요.');
    const timeout=setTimeout(()=>request.abort(),8000);
    try {
      const response=await fetch('/api/dispatcher',{headers:{Authorization:'Bearer '+selected},
        cache:'no-store',credentials:'omit',signal:request.signal});
      if(!response.ok)throw new Error('unavailable');
      const data=await response.json();
      if(selected!==token||revision!==version)return;
      if(typeof data?.service?.running!=='boolean'||typeof data.config_enabled!=='boolean'
        ||typeof data.paused!=='boolean'||!Number.isFinite(data.read_at))throw new Error('invalid_snapshot');
      const service=data.service;
      const reasons={matching_pid:'등록 기록에 맞는 프로세스가 관찰됐어요.',
        process_missing:'등록 기록의 프로세스가 종료된 것으로 관찰됐어요.',
        missing_record:'서비스 등록 기록이 없어요. 별도로 실행한 프로그램의 상태는 알 수 없어요.',
        invalid_record:'서비스 기록의 형식을 확인할 수 없어요.',
        unreadable_record:'서비스 기록을 읽을 수 없어요.',
        process_unreadable:'등록된 프로세스를 확인할 수 없어요.',
        pid_reused:'프로세스 번호가 다른 실행에 사용되어 자비스 서비스인지 확인할 수 없어요.'};
      const allowed={running:['matching_pid'],stopped:['process_missing'],not_registered:['missing_record'],
        unknown:['invalid_record','unreadable_record','process_unreadable'],identity_mismatch:['pid_reused']};
      // Old servers expose only the boolean. Explicit new statuses must never fall back to it.
      const legacy=!Object.prototype.hasOwnProperty.call(service,'status');
      const validStatus=!legacy&&service.source==='observed'&&allowed[service.status]?.includes(service.reason)
        &&service.running===(service.status==='running');
      const observedRunning=legacy?service.running:validStatus&&service.status==='running';
      const label=legacy?(service.running?'서비스 실행 관찰':'실행 확인 안 됨')
        :!validStatus?'미확인':({running:'서비스 실행 관찰',stopped:'등록 프로세스 종료 관찰',
          not_registered:'등록 기록 없음 · 실행 미확인',unknown:'미확인',identity_mismatch:'실행 신원 미확인'})[service.status];
      const reason=legacy?(service.running?'서비스 프로세스가 관찰됐어요.'
        :'실행 중인 서비스 기록을 확인하지 못했어요. 미등록 또는 중지 상태일 수 있어요.')
        :validStatus?reasons[service.reason]:'서비스 상태 응답을 해석할 수 없어요. 실행 여부는 미확인이에요.';
      const setting=!data.config_enabled?'자동 배정 설정 꺼짐':data.paused?'자동 배정 일시정지':'자동 배정 설정 켜짐';
      dispatchCard.status.textContent=label;
      dispatchCard.status.className='status '+(observedRunning?'completed':'needs_user_input');
      dispatchCard.detail.textContent=setting+' · 마지막 조회 '+stamp(data.read_at)
        +' · '+reason+(observedRunning?' 실제 업무 진행은 작업 기록에서 확인하세요.':'');
      dispatchCard.next.textContent=!observedRunning?'다음: 자동 배정 서비스의 등록·실행 여부를 확인하세요. Ollama 실행만으로 시작되지 않아요.'
        :!data.config_enabled?'다음: 자동 배정을 원하면 담당자에게 설정 확인을 요청하세요.'
        :data.paused?'다음: 작업 현황의 자동 순환 패널에서 재개 여부를 결정하세요.'
        :'다음: 작업 현황에서 배정 결과와 직원별 제한을 확인하세요.';
    } catch(error) {
      if(selected===token&&revision===version)unknown('조회하지 못했어요. 이전 상태는 현재 실행 근거로 사용할 수 없어요. 서비스가 꺼졌다고 단정할 수는 없어요.');
    } finally {clearTimeout(timeout);if(pending===request)pending=null;}
  }
  // Ollama card: last /api/tags observation only. Installed is not loaded, inference-ready or chat success.
  let modelPending=null;
  function modelUnknown(reason) {
    modelCard.status.textContent='미확인'; modelCard.status.className='status';
    modelCard.detail.textContent=reason;
    modelCard.next.textContent='다음: Ollama 실행 상태를 확인하고 잠시 후 다시 확인하세요.';
  }
  async function refreshModel() {
    if(!token||document.hidden||modelPending)return;
    const selected=token, revision=version, request=new AbortController(); modelPending=request;
    const timeout=setTimeout(()=>request.abort(),8000);
    try {
      const response=await fetch('/api/model-status',{headers:{Authorization:'Bearer '+selected},
        cache:'no-store',credentials:'omit',signal:request.signal});
      if(selected!==token||revision!==version)return;
      if(!response.ok)throw new Error(response.status===404?'old_server':'unavailable');
      const data=await response.json();
      if(selected!==token||revision!==version)return;
      // Fixed contract: reason decides model_installed; anything else is unknown, never a guess.
      const allowed={responding:{model_listed:true,model_not_listed:false},connection_refused:{connection_refused:null},
        unknown:{timeout:null,http_error:null,unsupported_encoding:null,response_too_large:null,malformed_response:null,connection_error:null}};
      const own=(object,key)=>typeof key==='string'&&Object.prototype.hasOwnProperty.call(object,key);
      if(data?.source!=='observed'||!Number.isFinite(data.observed_at)||typeof data.model!=='string'||!data.model
        ||!own(allowed,data.status)||!own(allowed[data.status],data.reason)
        ||allowed[data.status][data.reason]!==data.model_installed)throw new Error('invalid_status');
      const reasons={model_listed:'설치 목록에 설정된 모델이 있어요.',model_not_listed:'설치 목록에 설정된 모델이 없어요.',
        connection_refused:'모델 서버가 연결을 거부했어요. 실행 중이 아닐 수 있어요.',timeout:'응답을 기다리다 확인하지 못했어요.',
        http_error:'모델 서버가 정상 응답을 주지 않았어요.',unsupported_encoding:'응답 형식을 확인할 수 없어요.',
        response_too_large:'응답이 너무 커서 확인하지 않았어요.',malformed_response:'응답 형식을 확인할 수 없어요.',
        connection_error:'모델 서버 연결을 확인하지 못했어요.'};
      const installed=data.status==='responding'&&data.model_installed;
      modelCard.status.textContent=data.status==='responding'?(installed?'서버 응답 · 모델 설치 확인':'서버 응답 · 모델 없음')
        :data.status==='connection_refused'?'연결 안 됨':'미확인';
      modelCard.status.className='status'+(installed?' completed':data.status==='unknown'?'':' needs_user_input');
      modelCard.detail.textContent='마지막 확인 '+stamp(data.observed_at)+' · 모델 '+data.model+' · '+reasons[data.reason]
        +' 마지막 관찰일 뿐 모델 로딩이나 답변 가능 여부를 확인한 것은 아니에요.';
      modelCard.next.textContent=installed?'다음: 실제 답변 가능 여부는 대화 탭에서 확인하세요.'
        :data.status==='responding'?'다음: 설정된 모델을 직접 설치했는지 확인하세요. 자동으로 내려받지 않아요.'
        :'다음: Ollama 실행 상태를 확인하고 잠시 후 다시 확인하세요.';
    } catch(error) {
      if(selected===token&&revision===version)modelUnknown(error.message==='old_server'
        ?'이 자비스 화면 서버는 모델 서버 확인을 지원하지 않아요. 화면 서버를 다시 시작하면 확인할 수 있어요.'
        :'모델 서버 상태를 조회하지 못했어요. 이전 상태는 현재 근거로 사용할 수 없고, 꺼졌다고 단정할 수도 없어요.');
    } finally {clearTimeout(timeout);if(modelPending===request)modelPending=null;}
  }
  window.addEventListener('jarvis-connection',event=>{
    const next=event.detail.token||null;
    if(next!==token){version++;pending?.abort();pending=null;modelPending?.abort();modelPending=null;token=next;
      modelUnknown(token?'모델 서버 상태를 확인하고 있어요.':'실제 서비스에 연결하지 않았어요. 모델 서버 상태는 미확인이에요.');}
    if(token){refresh();refreshModel();}else unknown('실제 서비스에 연결하지 않았어요. 샘플 기록으로 실행 여부를 알 수 없어요.');
  });
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){refresh();refreshModel();}});
  setInterval(()=>Promise.all([refresh(),refreshModel()]),30000);
  return {hud(text,mode){
    hudCard.status.textContent=mode==='connected'?'작업 기록 연결 확인':mode==='demo'?'샘플 · 실제 상태 미확인':'미확인';
    hudCard.status.className='status';
    hudCard.detail.textContent=mode==='connected'?'최근 연결 확인 '+stamp(Date.now()/1000)+' · 작업 기록 연결의 근거예요. 모델 서버와 채팅 응답 준비를 확인한 것은 아니에요.'
      :mode==='demo'?'예시 화면이에요. 실제 자비스나 직원의 실행 상태를 나타내지 않아요.'
      :'화면 연결: '+text+' · 서비스가 꺼졌다고 단정할 수는 없어요.';
    hudCard.next.textContent=mode==='connected'?'다음: 대화 탭에서 채팅 연결 안내를 확인하세요. 자동 업무는 디스패처 상태도 확인해야 해요.'
      :'다음: 자비스 화면을 실행한 뒤 연결 파일을 선택하세요.';
  }};
})();
function connection(text, mode=''){ $('connectionLabel').textContent=text; $('connectionDot').className='dot '+mode; serviceOverview.hud(text, mode); }
function stopStream(){state.generation++;stream.stop();}
function resetTimings(){state.timings=new JarvisDisplayTimings();$('latencyMetric').textContent='새 기록이 도착하면 갱신 속도를 측정합니다. 과거 기록은 제외해요.';}
function disconnect(){stopStream(); state.token=null; state.demo=false; state.jobs=[]; state.cursors.clear(); state.feeds.clear(); state.recovery.clear();state.selected=null;resetTimings(); $('connectionFile').value=''; $('workspace').hidden=true; $('welcome').hidden=false; $('refresh').hidden=true; $('disconnect').hidden=true; connection('연결 전'); notice(); window.dispatchEvent(new CustomEvent('jarvis-connection',{detail:{token:null}}));}
function render(){
  $('welcome').hidden=true; $('workspace').hidden=false; $('refresh').hidden=state.demo; $('disconnect').hidden=false; $('demoNote').hidden=!state.demo;
  $('totalCount').textContent=state.jobs.length; $('taskCount').textContent=state.jobs.length+'개';
  $('activeCount').textContent=state.jobs.filter(j=>j.status==='running').length; $('doneCount').textContent=state.jobs.filter(j=>j.status==='completed').length;
  $('attentionCount').textContent=state.jobs.filter(j=>['needs_user_input','timed_out'].includes(j.status)).length;
  const list=$('taskList'); list.replaceChildren();
  for(const job of state.jobs){const button=element('button','task-button'+(job.task_id===state.selected?' selected':'')); button.type='button'; button.setAttribute('aria-pressed',String(job.task_id===state.selected));
    button.append(element('span','task-button-title',job.title)); const meta=element('span','task-button-meta'); meta.append(element('span','status '+job.status,labels[job.status]||'상태 확인'),element('span','',stamp(job.updated_at))); button.append(meta); button.addEventListener('click',()=>select(job.task_id)); list.append(button);}
  const job=state.jobs.find(j=>j.task_id===state.selected);
  if(!job){$('taskTitle').textContent='표시할 작업이 없습니다'; $('taskUpdated').textContent='허용된 작업이 만료되었거나 삭제되었을 수 있어요.'; $('workflow').replaceChildren(); $('activity').replaceChildren(); $('qaResult').textContent='—'; $('qaDescription').textContent=''; $('budgetValue').textContent='—'; $('budgetProgress').value=0; $('taskStatus').textContent=''; $('streamLabel').textContent='표시할 기록 없음'; return;}
  $('taskTitle').textContent=job.title; $('taskStatus').textContent=labels[job.status]||'상태 확인'; $('taskStatus').className='status '+job.status;
  $('taskUpdated').textContent='최근 변경 '+stamp(job.updated_at)+' · 수정 '+job.corrections+'/3 · 실행 재개 '+job.resumes+'/2';
  const stage=['planner','coder','qa'].indexOf(job.role); $('workflow').replaceChildren();
  ['planner','coder','qa'].forEach((role,index)=>{const done=job.status==='completed'||index<stage; const node=element('div','workflow-step'+(done?' done':index===stage?' current':'')); node.append(element('span','step-mark',done?'✓':String(index+1)),element('span','',roles[role])); $('workflow').append(node);});
  if(job.qa){$('qaResult').textContent=job.qa.passed?'검증 통과':'검증 확인 필요'; $('qaDescription').textContent=job.qa.cases_total?job.qa.cases_passed+' / '+job.qa.cases_total+'개 입력·출력 테스트 통과'+(job.qa.checker==='python_container_cases_v1'?' · 격리 실행':' · 제한 수식 검증'):'구문·컴파일 검사 범위입니다.';}
  else{$('qaResult').textContent='검증 대기'; $('qaDescription').textContent='결과가 도착하면 여기에 표시됩니다.';}
  $('budgetValue').textContent=number(job.model_reserved)+' / '+number(job.model_limit); $('budgetProgress').max=job.model_limit; $('budgetProgress').value=job.model_reserved;
  const feed=state.feeds.get(job.task_id)||[]; $('activity').replaceChildren();
  if(!feed.length){$('activity').append(element('li','empty-feed',state.demo?'이 작업에는 예시 기록이 없습니다.':job.latest_sequence===0?'이벤트 도입 전 작업입니다. 현재 상태만 표시해요.':'작업 기록을 불러오고 있어요.'));}
  for(const item of [...feed].reverse().slice(0,14)){const row=element('li'); row.append(element('span','',roles[item.agent_id]+' · '+(labels[item.payload.status]||'변경'))); const time=element('time','',stamp(item.timestamp)); time.dateTime=new Date(item.timestamp*1000).toISOString(); row.append(time); $('activity').append(row);}
}
async function loadJobs(){
  const token=state.token;
  const response=await fetch('/api/tasks',{headers:{Authorization:'Bearer '+token},cache:'no-store',credentials:'omit'});
  if(state.token!==token||state.demo)return;
  if(response.status===401){disconnect(); throw new Error('연결 파일을 다시 선택해주세요.');}
  if(!response.ok)throw new Error('자비스 상태를 가져오지 못했어요. 실행 상태를 확인해주세요.');
  const data=await response.json(); if(!Array.isArray(data.tasks)||data.read_only!==true)throw new Error('작업 응답을 확인할 수 없어요.');
  if(state.token!==token||state.demo)return;
  state.jobs=data.tasks; if(!state.jobs.some(j=>j.task_id===state.selected))state.selected=state.jobs[0]?.task_id||null;
  render(); connection('연결됨','connected');window.dispatchEvent(new CustomEvent('jarvis-connection',{detail:{token}}));
}
function select(id){state.selected=id;resetTimings();notice();render();watch();}
function watch(){
  stopStream(); if(state.demo||!state.token||!state.selected)return;
  const job=state.jobs.find(j=>j.task_id===state.selected); if(!job||!job.events_available){$('streamLabel').textContent='현재 상태만 표시';return;}
  const id=state.selected;
  stream.start({url:(location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/tasks/'+encodeURIComponent(id)+'/events',
    taskId:id,token:state.token,after:state.cursors.get(id)||0,latest:job.latest_sequence,
    isTerminal:()=>terminal(state.jobs.find(j=>j.task_id===id)?.status)});
}
function streamState(status){
  const text={connecting:'기록 연결 중',connected:'기록 연결됨',receiving:'기록 동기화 중',checking:'연결 확인 중',retrying:'다시 연결 중',offline:'연결 중단',idle:'새 기록 대기',synced:'최신 기록'};
  $('streamLabel').textContent=text[status]||'연결 확인 중';
  if(['connected','receiving','idle','synced'].includes(status))connection('연결됨','connected');
  if(['checking','retrying','offline'].includes(status))connection(status==='offline'?'갱신 중단':'재연결 중');
  if(status==='offline')notice('실시간 연결이 잠시 멈췄어요. 새로고침으로 다시 연결해주세요.');
}
function streamRecovery(reason){
  state.recovery.add(state.selected);connection('기록 확인 필요');$('streamLabel').textContent='현재 상태 확인 필요';
  notice(reason==='denied'?'연결이 거부됐어요. 연결 파일과 허용된 화면을 확인해주세요.':'기록 형식이나 순서를 확인해야 해요. 새로고침하면 현재 상태부터 다시 연결합니다.');
}
function receiveEvent(event,{live}){
  const id=event.task_id,job=state.jobs.find(j=>j.task_id===id);if(!job)return;
  state.cursors.set(id,event.sequence);const feed=state.feeds.get(id)||[];feed.push(event);state.feeds.set(id,feed.slice(-32));
  if(event.sequence>=job.latest_sequence){job.status=event.payload.status;job.role=event.payload.role;job.corrections=event.payload.correction_retries;job.steps_started=event.payload.steps_started;job.output_reserved=event.payload.generation_tokens_reserved;job.model_reserved=job.output_reserved*9;job.updated_at=event.timestamp;job.latest_sequence=event.sequence;}
  render();
  if(live&&!document.hidden){const generation=state.generation;
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(generation!==state.generation||state.selected!==id||document.hidden)return;
      state.timings.record(Date.now()-event.timestamp*1000);const metrics=state.timings.summary();
      if(metrics.count)$('latencyMetric').textContent='최근 화면 갱신 p95 '+Math.round(metrics.p95)+'ms · '+metrics.count+'개 표본 (이 PC의 현재 연결)';
    }));
  }
  if(terminal(job.status)&&event.sequence>=job.latest_sequence){const token=state.token;loadJobs().catch(error=>{if(state.token===token)notice(error.message);});}
}
const timingLabel=element('p','muted small','새 기록이 도착하면 갱신 속도를 측정합니다. 과거 기록은 제외해요.');
timingLabel.id='latencyMetric';$('activity').after(timingLabel);
$('chooseFile').addEventListener('click',()=>$('connectionFile').click());
$('connectionFile').addEventListener('change',async event=>{
  const file=event.target.files[0]; if(!file)return;
  if(file.size>128){notice('자비스 연결 파일을 선택해주세요. 다른 문서는 읽지 않습니다.'); event.target.value=''; return;}
  const token=await file.text(); event.target.value='';
  if(!/^[A-Za-z0-9_-]{43}$/.test(token)){notice('올바른 자비스 연결 파일이 아닙니다.');return;}
  stopStream(); state.demo=false; state.token=token; state.cursors.clear(); state.feeds.clear();state.recovery.clear();resetTimings();notice(); connection('연결 중');
  try{await loadJobs(); watch();}catch(error){state.token=null; connection('연결 확인 필요');notice(error.message);}
});
$('refresh').addEventListener('click',async()=>{notice();try{await loadJobs();
  for(const id of state.recovery){const job=state.jobs.find(j=>j.task_id===id);if(job){state.cursors.set(id,job.latest_sequence);state.feeds.delete(id);}}
  if(state.recovery.size)notice('현재 상태를 다시 불러왔습니다. 이전 기록은 건너뛰고 이후 변경부터 표시합니다.');
  state.recovery.clear();render();watch();}catch(error){notice(error.message);}});
$('disconnect').addEventListener('click',disconnect);
$('demo').addEventListener('click',()=>{
  window.dispatchEvent(new CustomEvent('jarvis-connection',{detail:{token:null}}));
  stopStream(); state.token=null; state.demo=true; state.cursors.clear(); state.feeds.clear();state.recovery.clear();resetTimings();$('latencyMetric').textContent='샘플 모드에서는 갱신 속도를 측정하지 않습니다.';notice();
  const now=Date.now()/1000; state.jobs=[
    {task_id:'sample-complete',title:'정수 합계 함수를 작성하고 검증하기',status:'completed',role:'qa',steps_started:3,corrections:0,resumes:0,updated_at:now-90,model_reserved:18432,model_limit:65536,qa:{checker:'python_container_cases_v1',passed:true,cases_passed:4,cases_total:4},latest_sequence:7,events_available:true},
    {task_id:'sample-active',title:'파일 정리 도우미의 개선안 살펴보기',status:'running',role:'coder',steps_started:2,corrections:0,resumes:0,updated_at:now-30,model_reserved:18432,model_limit:65536,qa:null,latest_sequence:4,events_available:true},
    {task_id:'sample-attention',title:'수정 결과를 다시 확인하기',status:'needs_user_input',role:'qa',steps_started:9,corrections:3,resumes:0,updated_at:now-180,model_reserved:46080,model_limit:65536,qa:{checker:'python_integer_cases_v1',passed:false,cases_passed:2,cases_total:3},latest_sequence:19,events_available:true}];
  state.selected='sample-complete'; state.feeds.set(state.selected,[{agent_id:'planner',timestamp:now-120,payload:{status:'running'}},{agent_id:'coder',timestamp:now-110,payload:{status:'running'}},{agent_id:'qa',timestamp:now-92,payload:{status:'running'}},{agent_id:'qa',timestamp:now-90,payload:{status:'completed'}}]); render(); connection('샘플 모드','demo'); $('streamLabel').textContent='예시 기록';
});
window.addEventListener('pagehide',()=>{stopStream();state.token=null;});
document.addEventListener('visibilitychange',()=>{state.generation++;if(!state.demo)resetTimings();});
