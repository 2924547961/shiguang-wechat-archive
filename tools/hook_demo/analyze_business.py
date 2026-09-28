"""Summarize observed call edges without inferring cross-thread causality."""
import bisect
import collections
import json
import struct
import sys
from pathlib import Path
import pefile

folder=Path(sys.argv[1])
pe=pefile.PE(sys.argv[2],fast_load=True)
d=pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
raw=pe.get_data(d.VirtualAddress,d.Size)
ranges=sorted((a,b) for a,b,_ in struct.iter_unpack('<III',raw[:len(raw)//12*12]) if a<b)
starts=[a for a,b in ranges]
def owner(rva):
    i=bisect.bisect_right(starts,rva)-1
    return hex(ranges[i][0]) if i>=0 and rva<ranges[i][1] else 'unmapped:'+hex(rva)
events=[json.loads(s) for s in (folder/'events.jsonl').read_text('utf-8').splitlines()]
counts=collections.Counter(); graphs=collections.defaultdict(collections.Counter)
for e in events:
    if e['event']!='business_call_edges':continue
    key=(e['window'],e['thread']);counts[key]+=len(e['edges'])
    for source,target,depth in e['edges']:
        graphs[key][(owner(int(source,16)),source,target)]+=1
rows=[dict(window=w,thread=t,caller_range=a,call_site=b,target=c,count=n)
      for (w,t),g in graphs.items() for (a,b,c),n in g.items()]
(folder/'调用关系.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),'utf-8')
lines=['# 业务调用追踪：已验证关系与缺口','',
       f'共记录 {sum(counts.values()):,} 次调用事件；这些包含后台工作，不等于发送业务专属调用。',
       '本次监听已结束。函数目标为原始执行地址；调用者范围由 PE 展开表映射，可能是函数片段。','',
       '| 追踪时间段 | 线程 | 调用事件数 | 不同调用关系数 |','|---|---|---:|---:|']
for key,n in sorted(counts.items()):lines.append(f'| {key[0]} | {key[1]} | {n} | {len(graphs[key])} |')
lines+=['','## 第二段的网络线程：可验证的调用关系','',
        '以下只列从已知网络处理代码范围发出的模块内调用，不给未知函数命名，也不把这些边排列成一次请求的完整调用顺序。','',
        '| 调用者代码范围 | call 指令位置 | 实际目标 | 次数 |','|---|---|---|---:|']
for (a,b,c),n in sorted(graphs.get((2,37712),{}).items()):
    if a in {'0x4d99340','0x4d950c0'} and c.startswith('Weixin.dll+'):
        lines.append(f'| `{a}` | `{b}` | `{c}` | {n} |')
lines+=['','## 本轮消息证据','',
        '- 18:53:21.032：线程 37712、socket 0x2ecc 发送 243 字节。',
        '- 18:53:21.239：首次查询到测试消息 local_id=32989，server_id=0。',
        '- 18:53:21.243：同一 socket 接收 179 字节。',
        '- 18:53:21.467：同一条消息的 server_id 更新为非零。','',
        '## 尚未还原的部分','',
        '1. UI 点击与特定业务任务的对应关系；未捕捉消息对象或任务 ID。',
        '2. 跨线程队列提交/取出关系；地址调用边不能证明不同线程处理的是同一个任务。',
        '3. 序列化、加密及应答解析函数的语义；未读取其参数内容。',
        '4. 上轮线程 46852 已不在本轮存活线程列表，新建工作线程未被自动纳入。',
        '5. 仅采集短时窗口并排除系统模块，不能覆盖全部执行过程。',
        '6. 原始 depth 出现负数及很大的累积值，不据此构建嵌套树。批次时间不是每个调用的时间。','',
        '因此本轮产物是实际调用关系集合，尚不是完整业务调用链。下一步应分析候选函数并用任务/对象标识验证线程之间的连接，而不是继续扩大盲目追踪。','',
        '机器可读调用关系：同目录 调用关系.json；原始日志：events.jsonl。']
(folder/'业务调用追踪报告.md').write_text('\n'.join(lines),'utf-8')
print(json.dumps({'events':sum(counts.values()),'unique_edges':len(rows),'counts':{str(k):v for k,v in counts.items()},'report':str(folder/'业务调用追踪报告.md')},ensure_ascii=False))
