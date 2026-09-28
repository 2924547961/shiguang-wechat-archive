"""Resolve observed call edges plus explicitly labelled static tail jumps."""
import bisect,collections,json,struct,sys
from pathlib import Path
import capstone,pefile
folder=Path(sys.argv[1]);pe=pefile.PE(sys.argv[2],fast_load=True)
d=pe.OPTIONAL_HEADER.DATA_DIRECTORY[3];raw=pe.get_data(d.VirtualAddress,d.Size)
ranges=sorted((a,b) for a,b,_ in struct.iter_unpack('<III',raw[:len(raw)//12*12]) if a<b);starts=[a for a,b in ranges]
def owner(a):
    i=bisect.bisect_right(starts,a)-1
    return ranges[i][0] if i>=0 and a<ranges[i][1] else a
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64);md.detail=True
graphs=collections.defaultdict(lambda:collections.defaultdict(list))
for r in json.loads((folder/'调用关系.json').read_text('utf-8')):
    if r['window']!=2 or not r['caller_range'].startswith('0x') or not r['target'].startswith('Weixin.dll+'):continue
    a=int(r['caller_range'],16); b=int(r['target'].split('+')[1],16)
    graphs[r['thread']][a].append((b,'observed_call',r['call_site']))
for tid,graph in graphs.items():
    targets={b for edges in graph.values() for b,_,_ in edges}
    for a in targets:
        if a in graph:continue
        # Only accept straight-line small adapters ending in a direct jump.
        for ins in md.disasm(pe.get_data(a,64),a):
            if ins.mnemonic=='jmp' and ins.operands[0].type==capstone.CS_OP_IMM:
                graph[a].append((owner(ins.operands[0].imm),'static_tail_jump',hex(ins.address)));break
            if ins.mnemonic.startswith('j') or ins.mnemonic in ('call','ret','int3'):break
def path(tid,start,end):
    q=collections.deque([(start,[])]);seen={start}
    while q:
        a,steps=q.popleft()
        if a==end:return steps
        if len(steps)>=24:continue
        for b,kind,site in graphs[tid].get(a,[]):
            if b not in seen:
                seen.add(b);q.append((b,steps+[dict(source=hex(a),target=hex(b),kind=kind,site=site)]))
    return None
requests=[('record_encrypt',37712,0x66027f0,0x7333ad0),
          ('record_decrypt',37712,0x701c310,0x7333ad0),
          ('response_enqueue',37712,0x4c767d0,0x28dfb30),
          ('database_add_dispatch',37220,0x39368c0,0x1477de0),
          ('database_add_to_writefile',37220,0x39368c0,0x5826700)]
out=[dict(name=n,thread=t,start=hex(a),end=hex(b),path=path(t,a,b)) for n,t,a,b in requests]
(folder/'已验证局部路径.json').write_text(json.dumps(out,indent=2),'utf-8')
print(json.dumps(out))
