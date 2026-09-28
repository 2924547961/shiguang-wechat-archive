// Short, bounded call-edge capture after retrieving an Enter key event.
// Only code addresses and call depth are recorded, never message buffers.
let businessStarted=false, businessStopped=false, edgeCount=0;
let traceWindow=0;
const followed=new Set();
const businessModule=Process.getModuleByName('Weixin.dll');
const businessEnd=businessModule.base.add(businessModule.size);
function localAddress(p) {
  return p.compare(businessModule.base)>=0 && p.compare(businessEnd)<0;
}
function stopBusiness() {
  if(businessStopped)return;businessStopped=true;
  for(const tid of followed) {try{Stalker.unfollow(tid);}catch(_){} }
  Stalker.flush();
  send({event:'business_trace_stopped',wall_ms:Date.now(),edges:edgeCount});
  followed.clear();
  setTimeout(()=>{businessStarted=false;},150);
}
for(const m of Process.enumerateModules()) {
  if(m.name!=='Weixin.dll' && m.name!=='Weixin.exe')Stalker.exclude(m);
}
function startBusiness(uiThread) {
  if(businessStarted || edgeCount>=200000 || traceWindow>=30)return;
  businessStarted=true;businessStopped=false;traceWindow++;
  const windowId=traceWindow;
  const live=new Set(Process.enumerateThreads().map(t=>t.id));
  const targets=[...new Set([uiThread,...BUSINESS_THREADS])].filter(t=>live.has(t));
  send({event:'business_trace_started',wall_ms:Date.now(),threads:targets,window:windowId,
    note:'Input is a candidate trigger. Edges prove same-thread calls only; cross-thread causality remains unverified.'});
  for(const tid of targets) {
    try {
      Stalker.follow(tid,{
        events:{call:true,ret:false,exec:false,block:false,compile:false},
        onReceive(data) {
          const edges=[];
          for(const row of Stalker.parse(data,{annotate:false,stringify:false})) {
            if(edgeCount>=200000)break;
            const [from,to,depth]=row;
            if(!localAddress(from))continue;
            edges.push([from.sub(businessModule.base).toString(),
              localAddress(to)?'Weixin.dll+'+to.sub(businessModule.base):to.toString(),depth]);
            edgeCount++;
          }
          if(edges.length)send({event:'business_call_edges',thread:tid,window:windowId,wall_ms:Date.now(),
            note:'Batch arrival time; individual call timestamps are not available.',edges});
          if(edgeCount>=200000 && !businessStopped)stopBusiness();
        }
      });
      followed.add(tid);
    }catch(e){send({event:'probe_error',api:'Stalker.follow',thread:tid,error:String(e)});}
  }
  setTimeout(stopBusiness,1200);
}
for(const api of ['PeekMessageW','GetMessageW']) {
  attachExport('user32.dll',api,{
    onEnter(args){this.message=args[0];this.remove=api==='GetMessageW'||(args[4].toUInt32()&1)!==0;},
    onLeave(result){
      if(businessStarted || result.toInt32()<=0 || !this.remove)return;
      try {
        const msg=this.message;
        if(msg.add(Process.pointerSize).readU32()===0x100 &&
           msg.add(Process.pointerSize===8?16:8).readPointer().toUInt32()===13)
          startBusiness(Process.getCurrentThreadId());
      }catch(e){send({event:'probe_error',api,error:String(e)});}
    }
  });
}
attachExport('user32.dll','DispatchMessageW',{
  onEnter(args){
    try {
      const msg=args[0], id=msg.add(Process.pointerSize).readU32();
      const enter=id===0x100 && msg.add(Process.pointerSize===8?16:8).readPointer().toUInt32()===13;
      if(id===0x202 || enter)startBusiness(Process.getCurrentThreadId());
    }catch(e){send({event:'probe_error',api:'business DispatchMessageW',error:String(e)});}
  }
});
send({event:'business_probe_ready',trigger:'mouse release or Enter; rearm after each window',duration_ms:1200,max_edges:200000,max_windows:30});
