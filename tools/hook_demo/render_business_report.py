"""Render the consolidated local evidence report as a standalone HTML file."""
import html
import re
import sys
from pathlib import Path
import markdown

folder=Path(sys.argv[1]);source=(folder/'发送机制还原报告.md').read_text('utf-8')
sections=re.split(r'^## ',source,flags=re.M)[1:]
body=[]
for i,section in enumerate(sections):
    title,_,content=section.partition('\n')
    body.append(f'<details id="s{i}" {"open" if i==0 else ""}><summary>{html.escape(title)}</summary><article>'+markdown.markdown(content,extensions=['tables','fenced_code'])+'</article></details>')
nodes=[('输入与界面','已观察输入；控件到业务对象未连接','gap',2),
       ('本地消息处理','已定位 AddMessage 局部调用关系','ok',3),
       ('发送任务跨线程','缺少任务提交与取出时的同一标识','gap',9),
       ('MMTLS 发出路径','读写循环 → 记录写入 → AES-GCM 相关处理','ok',5),
       ('网络与接收路径','同 socket 收发；记录解密路径已定位','ok',6),
       ('应答进入队列','已定位 __OnResponse 的队列转发分支','ok',7),
       ('业务回调与状态更新','观察到 server_id 更新；因果调用连接未捕捉','gap',8)]
cards=''.join(f'<a class="node {kind}" href="#s{idx}"><small>{"已确认局部证据" if kind=="ok" else "连接尚有缺口"}</small><strong>{title}</strong><span>{desc}</span></a>' for title,desc,kind,idx in nodes)
document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>微信发送机制 · 实测还原</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f4f2ee;color:#242c2a;font:16px/1.75 "Microsoft YaHei",sans-serif}main{max-width:1080px;margin:auto;padding:42px 24px}header{border-top:5px solid #294f43;padding:24px 0}h1{font-size:32px;line-height:1.4;margin:12px 0}header p{max-width:850px;color:#59615c}.status{display:inline-block;background:#f4e3c6;color:#70501d;padding:4px 12px;border-radius:4px}.flow{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:24px 0}.node{display:flex;flex-direction:column;gap:8px;padding:18px;background:white;border:1px solid #d2dad4;border-left:4px solid #426856;text-decoration:none;color:inherit}.node strong{font-size:18px}.node span{font-size:14px}.node small{color:#426856}.node.gap{border-left-color:#ae7833}.node.gap small{color:#8c6026}button{font:inherit;padding:8px 16px;background:white;border:1px solid #bfc8c2;border-radius:4px;cursor:pointer;margin:0 8px 16px 0}details{background:white;border:1px solid #d9dcd6;margin:12px 0;scroll-margin-top:16px}summary{padding:17px 20px;font-weight:bold;cursor:pointer}article{padding:0 22px 20px;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #d7ddd8;padding:8px;text-align:left}pre{background:#eef2ed;padding:16px;overflow:auto;font-size:14px}code{font-family:Consolas,monospace}a{color:#285e4b}.note{border-left:3px solid #ae7833;padding:10px 18px;background:#fffaf1}footer{font-size:13px;color:#66706a;margin-top:24px}@media print{button{display:none}body{background:white}main{padding:0}details{break-inside:avoid}.flow{grid-template-columns:1fr 1fr}}
</style><main><header><small>LOCAL EVIDENCE / 微信 4.1.15.13</small><h1>从输入到消息更新：发送机制还原</h1><span class="status">局部调用已确认 · 端到端链路仍有缺口</span><p>基于 193,458 次调用事件与 2,440 个代码范围的静态分析。点击阶段查看依据。流程按职责排列，不表示卡片之间已建立直接调用关系。</p></header><div class="flow">'''+cards+'''</div><p class="note">完整性边界：没有采集任务 ID 与跨线程队列消费者，不能把时间相近的操作拼成一条已验证业务链。无须再次发送测试消息来阅读本报告。</p><button id="expand">展开全部证据</button><button id="collapse">收起全部</button><button id="print">打印 / 保存 PDF</button>'''+''.join(body)+'''<footer>原始材料均保存在本目录。此页面不联网、不读取微信、不执行发送操作。</footer></main><script>
const all=()=>document.querySelectorAll('details');document.getElementById('expand').onclick=()=>all().forEach(d=>d.open=true);document.getElementById('collapse').onclick=()=>all().forEach(d=>d.open=false);document.getElementById('print').onclick=()=>{all().forEach(d=>d.open=true);window.print()};document.querySelectorAll('.node').forEach(a=>a.addEventListener('click',()=>{const d=document.querySelector(a.getAttribute('href'));if(d)d.open=true}));
</script></html>'''
target=folder/'发送流程图.html';target.write_text(document,'utf-8')
assert all(f'id="s{idx}"' in document for *_,idx in nodes)
assert 'http-equiv="refresh"' not in document
print(str(target))
