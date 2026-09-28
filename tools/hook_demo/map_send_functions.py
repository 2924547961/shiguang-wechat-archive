"""Map observed return addresses to PE unwind ranges and actual preceding calls."""
import bisect
import hashlib
import json
import sys
import struct
from pathlib import Path
import capstone
import pefile

def main():
    module=Path(sys.argv[1]); folder=Path(sys.argv[2])
    pe=pefile.PE(str(module),fast_load=True)
    directory=pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXCEPTION']]
    raw=pe.get_data(directory.VirtualAddress,directory.Size)
    entries=sorted((a,b) for a,b,_ in struct.iter_unpack('<III',raw[:len(raw)//12*12]) if a<b)
    starts=[a for a,b in entries]
    md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64)
    observed={}
    for line in (folder/'events.jsonl').read_text('utf-8').splitlines():
        e=json.loads(line)
        for frame in e.get('stack',[]):
            if frame.startswith('Weixin.dll+'):
                observed.setdefault(int(frame.split('+')[1],16),set()).add(e['event'])
    mapped=[]
    for rva,kinds in sorted(observed.items()):
        i=bisect.bisect_right(starts,rva-1)-1
        if i<0 or not entries[i][0]<=rva-1<entries[i][1]:continue
        begin,end=entries[i]
        instructions=list(md.disasm(pe.get_data(begin,rva-begin),begin))
        prior=instructions[-1] if instructions else None
        valid=prior is not None and prior.address+prior.size==rva and prior.mnemonic=='call'
        mapped.append(dict(return_rva=hex(rva),begin_rva=hex(begin),end_rva=hex(end),
                           events=sorted(kinds),preceding_call=(prior.mnemonic+' '+prior.op_str) if valid else None,
                           entry_bytes=pe.get_data(begin,16).hex()))
    data={'module':str(module),'sha256':hashlib.sha256(module.read_bytes()).hexdigest(),
          'image_size':pe.OPTIONAL_HEADER.SizeOfImage,'ranges':mapped}
    (folder/'function_ranges.json').write_text(json.dumps(data,indent=2),'utf-8')
    lines=['# 已观察调用栈的函数范围映射','',
           'PE 异常展开范围可能是函数片段，不保证是完整业务函数入口。前置 call 由范围起点顺序反汇编校验。','',
           '| 返回 RVA | 范围起点 | 前置调用 | 观察阶段 |','|---|---|---|---|']
    for x in mapped:lines.append(f"| {x['return_rva']} | {x['begin_rva']} | `{x['preceding_call']}` | {', '.join(x['events'])} |")
    (folder/'函数范围映射.md').write_text('\n'.join(lines),'utf-8')
    print(json.dumps(data))

if __name__=='__main__':main()
