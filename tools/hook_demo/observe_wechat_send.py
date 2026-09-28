"""Observe marked outgoing test messages and socket calls.

No message sending, argument replacement, payload capture, or database writes.
This records evidence; it does not assume socket writes are business-message sends.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from wxdesk.common import BASE, STATE, read_json, self_username
from wxdesk.discovery import discover, find_verified
from wxdesk.importer import encrypted
from wxdesk.messages import text_content

NETWORK_JS = r'''
const hooked=[];let calls=0;
const sockets=Process.findModuleByName('ws2_32.dll');
function stack(context){
  return Thread.backtrace(context,Backtracer.ACCURATE).slice(0,16).map(address=>{
    const module=Process.findModuleByAddress(address);
    return module?module.name+'+'+address.sub(module.base):address.toString();
  });
}
if(sockets)for(const name of ['send','WSASend']){
  const address=sockets.findExportByName(name);if(!address)continue;
  Interceptor.attach(address,{
    onEnter(args){
      this.active=++calls<=5000;if(!this.active)return;
      this.started=Date.now();this.call=calls;
      let size=0,count=1;
      try{
        if(name==='send')size=args[2].toInt32();
        else{
          count=args[2].toUInt32();
          if(count>64)return;
          for(let i=0;i<count;i++)size+=args[1].add(i*(Process.pointerSize===8?16:8)).readU32();
        }
        send({event:'socket_enter',api:name,call:this.call,wall_ms:this.started,
              thread:Process.getCurrentThreadId(),socket:args[0].toString(),bytes:size,buffers:count,stack:stack(this.context)});
      }catch(error){send({event:'probe_error',api:name,error:String(error)});}
    },
    onLeave(result){if(this.active)send({event:'socket_return',api:name,call:this.call,
      wall_ms:Date.now(),return_value:result.toInt32(),elapsed_ms:Date.now()-this.started});}
  });hooked.push(name);
}
send({event:'probe_ready',pid:Process.id,apis:hooked,modules:Process.enumerateModules()
  .filter(m=>/weixin|wechat|mmcomm/i.test(m.name)).map(m=>({name:m.name,size:m.size}))});
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=180)
    parser.add_argument('--marker',default='SG-SEND-TEST-01')
    parser.add_argument('--all-chats',action='store_true',help='Observe marked outgoing text in all conversations.')
    parser.add_argument('--lifecycle',action='store_true',help='Also observe UI input dispatch, receives and message database WriteFile calls.')
    parser.add_argument('--business-threads',default='',help='Comma-separated previously observed worker thread IDs; enable bounded call-edge trace on Enter.')
    parser.add_argument('--count',type=int,default=1,help='Stop eight seconds after observing this many distinct test messages.')
    args=parser.parse_args()
    if args.count<1:parser.error('--count must be positive.')
    if len(args.marker)<8:parser.error('Use a distinctive marker of at least 8 characters.')
    info=discover('',BASE,STATE);accounts=[a for a in info['accounts'] if a.get('active')]
    if len(accounts)!=1:raise SystemExit('Exactly one active account is required for this learning capture.')
    account=accounts[0];verified=find_verified(account['account'],account['dbdir'])
    if not verified:raise SystemExit('First synchronize this account to validate database access.')
    manifest=read_json(verified)
    folder=STATE/'observations'/('send_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S'))
    folder.mkdir(parents=True,exist_ok=True);lock=threading.Lock();sessions=[];sources=[]
    deadline=time.monotonic()+max(10,min(600,args.seconds));seen={};found_at=None;net_count=0
    table='Msg_'+hashlib.md5(b'filehelper').hexdigest()
    def tables(c):
        return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if re.fullmatch(r'Msg_[0-9a-fA-F]{32}',r[0]) and (args.all_chats or r[0]==table)]
    def new_rows(c,highs):
        for name in tables(c):
            high=highs.setdefault(name,0)
            for row in c.execute('SELECT local_id,server_id,local_type,create_time,real_sender_id,message_content FROM '+name+' WHERE local_id>? ORDER BY local_id DESC LIMIT 100',(high,)):
                yield (name,*row)
    out=(folder/'events.jsonl').open('w',encoding='utf-8',buffering=1)
    def record(event):
        event={'observed_at':dt.datetime.now().isoformat(timespec='milliseconds'),**event}
        with lock:out.write(json.dumps(event,ensure_ascii=False)+'\n')
    def on_message(message,data):
        nonlocal net_count
        if message['type']=='send':
            payload=message['payload'];record(payload)
            if payload.get('event')=='socket_enter':net_count+=1
            if payload.get('event') in {'probe_ready','lifecycle_probe_ready','business_probe_ready','business_trace_started','business_trace_stopped'}:print(json.dumps(payload),flush=True)
        elif message['type']=='error':record({'event':'probe_error','description':message.get('description','')})
    try:
        for rel,key in manifest['keys'].items():
            if not Path(rel).name.startswith('message_') or Path(rel).name=='message_fts.db':continue
            context=encrypted(Path(account['dbdir'])/rel,key['key']);c=context.__enter__()
            try:
                if not c.execute("SELECT 1 FROM sqlite_master WHERE name='Name2Id'").fetchone():
                    context.__exit__(None,None,None);continue
                names=tables(c)
                if not names and not args.all_chats:context.__exit__(None,None,None);continue
                high={name:c.execute('SELECT coalesce(max(local_id),0) FROM '+name).fetchone()[0] for name in names}
                own={r[0] for r in c.execute('SELECT rowid FROM Name2Id WHERE user_name IN (?,?)',(account['account'],self_username(account['account'])))}
                sources.append((context,c,rel,high,own))
            except BaseException:context.__exit__(None,None,None);raise
        if not sources:raise ValueError('No File Transfer Assistant history was found; open it in WeChat first.')
        import frida
        for pid in account['pids']:
            session=frida.attach(pid);sessions.append(session)
            js=NETWORK_JS
            if args.lifecycle or args.business_threads:js+='\n'+Path(__file__).with_name('observe_lifecycle.js').read_text('utf-8')
            if args.business_threads:
                tids=[int(t) for t in args.business_threads.split(',')]
                js+='\nconst BUSINESS_THREADS='+json.dumps(tids)+';\n'+Path(__file__).with_name('trace_business.js').read_text('utf-8')
            script=session.create_script(js);script.on('message',on_message);script.load()
        record({'event':'capture_started','marker':args.marker,'scope':('all-chats' if args.all_chats else 'filehelper')+'/self/test-marker','seconds':args.seconds})
        print(json.dumps({'event':'ready','marker':args.marker,'output':str(folder)}),flush=True)
        while time.monotonic()<deadline:
            for _,c,rel,high,own in sources:
                for chat_table,lid,sid,kind,created,sender,content in new_rows(c,high):
                    if sender not in own or kind!=1:continue
                    body=text_content(content)
                    if not body.startswith(args.marker):continue
                    key=(rel,chat_table,lid);state=(str(sid),body)
                    if seen.get(key)==state:continue
                    record({'event':'message_insert_seen' if key not in seen else 'message_update_seen',
                            'source':rel,'conversation_table':chat_table,'local_id':lid,'server_id':str(sid),'create_time':created,'text':body[:300]})
                    seen[key]=state
                    if len(seen)>=args.count:found_at=found_at or time.monotonic()
                    print(json.dumps({'event':'test_message_seen','local_id':lid,'server_id':str(sid)}),flush=True)
            if found_at and time.monotonic()-found_at>=8:break
            time.sleep(.2)
    finally:
        for session in sessions:
            try:session.detach()
            except Exception:pass
        for context,*_ in sources:context.__exit__(None,None,None)
        record({'event':'capture_finished','test_messages':len(seen),'socket_calls':net_count});out.close()
        print(json.dumps({'event':'finished','test_messages':len(seen),'socket_calls':net_count,'output':str(folder)}),flush=True)


if __name__=='__main__':main()
