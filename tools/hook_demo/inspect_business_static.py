"""Offline annotations of executed function ranges; no live process modification."""
import bisect,collections,json,re,struct,sys
from pathlib import Path
import capstone
import pefile

folder=Path(sys.argv[1]); binary=Path(sys.argv[2])
pe=pefile.PE(str(binary),fast_load=True)
d=pe.OPTIONAL_HEADER.DATA_DIRECTORY[3];raw=pe.get_data(d.VirtualAddress,d.Size)
ranges=sorted((a,b) for a,b,_ in struct.iter_unpack('<III',raw[:len(raw)//12*12]) if a<b)
starts=[a for a,b in ranges]
def bounds(rva):
    i=bisect.bisect_right(starts,rva)-1
    return ranges[i] if i>=0 and rva<ranges[i][1] else None
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64);md.detail=True
rows=json.loads((folder/'调用关系.json').read_text('utf-8'))
seen=collections.defaultdict(set)
for r in rows:
    for field in ['caller_range','target']:
        s=r[field].replace('Weixin.dll+','')
        if not s.startswith('0x'):continue
        b=bounds(int(s,16))
        if b:seen[b].add((r['window'],r['thread']))
def literal(rva):
    try:
        raw=pe.get_data(rva,512)
        value=raw.split(b'\0')[0]
        if len(value)>=5 and all(32<=c<127 or c in (9,10,13) for c in value):return value.decode('ascii')
    except Exception:pass
    return None
annotations=[]
for (start,end),contexts in sorted(seen.items()):
    if end-start>150000:continue
    strings={};calls=[]
    for ins in md.disasm(pe.get_data(start,end-start),start):
        if ins.mnemonic=='call':calls.append({'site':hex(ins.address),'operand':ins.op_str})
        for op in ins.operands:
            if op.type==capstone.CS_OP_MEM and op.mem.base==capstone.x86.X86_REG_RIP:
                ref=ins.address+ins.size+op.mem.disp
                s=literal(ref)
                if s:strings[hex(ref)]=s
    annotations.append({'start':hex(start),'end':hex(end),'contexts':sorted(contexts),'strings':strings,'calls':calls})
(folder/'静态函数注释.json').write_text(json.dumps(annotations,ensure_ascii=False,indent=2),'utf-8')
interesting=[]
pattern=re.compile(r'send|recv|message|longlink|shortlink|task|encrypt|proto|sqlite|chat|cgi|queue',re.I)
for a in annotations:
    hits=[s for s in a['strings'].values() if pattern.search(s)]
    if hits:interesting.append({'start':a['start'],'contexts':a['contexts'],'strings':hits[:12]})
(folder/'函数职责线索.json').write_text(json.dumps(interesting,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({'functions':len(annotations),'interesting':len(interesting),'candidates':interesting[:65]},ensure_ascii=True))
