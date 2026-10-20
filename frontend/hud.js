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
function connection(text, mode=''){ $('connectionLabel').textContent=text; $('connectionDot').className='dot '+mode; }
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
