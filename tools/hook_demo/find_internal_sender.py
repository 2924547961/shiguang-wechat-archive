"""Find internal-send research candidates in a PE file; never calls unknown functions."""
import argparse,bisect,hashlib,json,re,struct
from pathlib import Path
import capstone,pefile

def inspect(binary):
    data=binary.read_bytes();pe=pefile.PE(data=data,fast_load=True)
    directory=pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
    raw=pe.get_data(directory.VirtualAddress,directory.Size)
    ranges=sorted((a,b) for a,b,_ in struct.iter_unpack('<III',raw[:len(raw)//12*12]) if a<b)
    starts=[a for a,b in ranges]
    def owner(rva):
        i=bisect.bisect_right(starts,rva)-1
        return hex(ranges[i][0]) if i>=0 and rva<ranges[i][1] else None
    found={}
    for needle in [b'SendText',b'SendMsgRequest',b'SendMsgResponse',b'newsendmsg',b'SendMessage',b'SendMsgFailed']:
        pos=0
        while True:
            pos=data.find(needle,pos)
            if pos<0:break
            start=pos;end=pos+len(needle)
            while start>max(0,pos-100) and 32<=data[start-1]<127:start-=1
            while end<min(len(data),pos+200) and 32<=data[end]<127:end+=1
            rva=pe.get_rva_from_offset(start)
            found[rva]={'rva':hex(rva),'text':data[start:end].decode('ascii','replace'),'references':[]}
            pos+=len(needle)
    md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64)
    # Candidate RIP-relative LEA references, checked by disassembling the match.
    pattern=re.compile(rb'[\x48-\x4f]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....',re.S)
    for section in pe.sections:
        if not section.Characteristics&0x20000000:continue
        blob=section.get_data()
        for m in pattern.finditer(blob):
            site=section.VirtualAddress+m.start()
            target=site+7+struct.unpack('<i',m.group()[3:7])[0]
            if target not in found:continue
            ins=next(md.disasm(m.group(),site),None)
            if ins and ins.mnemonic=='lea':
                found[target]['references'].append({'instruction_rva':hex(site),'unwind_range':owner(site),
                                                   'instruction':ins.mnemonic+' '+ins.op_str,
                                                   'status':'static candidate; instruction boundary and business role require validation'})
    return {'binary':str(binary),'sha256':hashlib.sha256(data).hexdigest(),
            'send_ready':False,'reason':'No verified business entry, ABI, object lifetime or thread contract.',
            'candidates':list(found.values())}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary',type=Path,default=Path(r'D:\software\Weixin\4.1.15.13\Weixin.dll'))
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=inspect(args.binary)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(result,ensure_ascii=True))
