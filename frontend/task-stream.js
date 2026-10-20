'use strict';

// Transport only: no DOM, storage, model calls or mutation endpoints.
class JarvisTaskStream {
  constructor({socket=url=>new WebSocket(url), later=(fn,ms)=>setTimeout(fn,ms), cancel=timer=>clearTimeout(timer),
    now=Date.now, onEvent=()=>{}, onState=()=>{}, onRecovery=()=>{}}={}) {
    Object.assign(this,{makeSocket:socket,later,cancel,now,onEvent,onState,onRecovery});
    this.epoch=0; this.channel=null; this.timer=null; this.config=null;
  }
  stop(){
    this.epoch++; this.cancel(this.timer); this.timer=null;
    if(this.channel){const old=this.channel;this.channel=null;old.close();}
    this.config=null;
  }
  start(config){
    this.stop(); this.config={...config}; this.cursor=config.after; this.baseline=config.latest;
    this.retries=0; this.connect(this.epoch);
  }
  connect(epoch){
    if(epoch!==this.epoch||!this.config)return;
    const config=this.config;
    const channel=this.makeSocket(config.url+'?after='+this.cursor); this.channel=channel;
    const current=()=>epoch===this.epoch&&this.channel===channel;
    let ended=false, openedAt=0;
    this.onState('connecting');
    const recover=reason=>{if(!current())return;this.stop();this.onRecovery(reason);};
    channel.onopen=()=>{if(current()){openedAt=this.now();channel.send(JSON.stringify({token:config.token}));this.onState('connected');}};
    channel.onmessage=message=>{
      if(!current())return;
      let event;
      try{
        if(typeof message.data!=='string'||message.data.length>4096)throw new Error();
        event=JSON.parse(message.data);
        if(!event||typeof event!=='object')throw new Error();
      }catch{recover('format');return;}
      if(event.kind==='stream.end'){ended=true;this.onState(config.isTerminal()&&this.cursor>=this.baseline?'synced':'idle');return;}
      if(event.kind==='stream.error'){recover('cursor');return;}
      const p=event.payload;
      if(event.version!==1||event.task_id!==config.taskId||!Number.isInteger(event.sequence)||event.sequence<1
        ||!Number.isFinite(event.timestamp)||event.timestamp<0||!p
        ||!['ready','running','completed','needs_user_input','cancelled','timed_out'].includes(p.status)
        ||!['planner','coder','qa'].includes(p.role)||!['planner','coder','qa'].includes(event.agent_id)
        ||!Number.isInteger(p.correction_retries)||p.correction_retries<0||p.correction_retries>3
        ||!Number.isInteger(p.steps_started)||p.steps_started<0||p.steps_started>20
        ||!Number.isInteger(p.generation_tokens_reserved)||p.generation_tokens_reserved<0||p.generation_tokens_reserved>32768){recover('format');return;}
      if(event.sequence<=this.cursor)return;
      if(event.sequence!==this.cursor+1){recover('gap');return;}
      this.cursor=event.sequence; this.retries=0;
      // Replayed history and downtime catch-up are excluded from live timing.
      const live=event.sequence>this.baseline&&openedAt>0&&event.timestamp*1000>=openedAt;
      this.onEvent(event,{live,epoch}); this.onState('receiving');
    };
    channel.onerror=()=>{if(current())this.onState('checking');};
    channel.onclose=event=>{
      if(!current())return;
      if(event.code===1008){recover('denied');return;}
      if(config.isTerminal()&&this.cursor>=this.baseline){this.onState('synced');return;}
      if(event.code===1000&&ended){
        // The bounded server intentionally rotates idle streams. This is healthy.
        this.onState('idle'); this.timer=this.later(()=>this.connect(epoch),250); return;
      }
      if(this.retries>=3){this.onState('offline');return;}
      this.retries++; this.onState('retrying');
      this.timer=this.later(()=>this.connect(epoch),this.retries*1000);
    };
  }
}

class JarvisDisplayTimings {
  constructor(){this.samples=[];}
  record(milliseconds){
    if(!Number.isFinite(milliseconds)||milliseconds<0)return;
    this.samples.push(milliseconds);this.samples=this.samples.slice(-64);
  }
  summary(){
    const sorted=[...this.samples].sort((a,b)=>a-b);
    return {count:sorted.length,p95:sorted.length?sorted[Math.ceil(sorted.length*0.95)-1]:null};
  }
}
if(typeof module!=='undefined')module.exports={JarvisTaskStream,JarvisDisplayTimings};
