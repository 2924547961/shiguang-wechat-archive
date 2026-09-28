"""Produce a small evidence report from observe_wechat_send.py's JSONL log."""
import collections
import datetime as dt
import json
import sys
from pathlib import Path


def analyze(folder):
    folder=Path(folder)
    events=[json.loads(line) for line in (folder/'events.jsonl').read_text('utf-8').splitlines() if line]
    messages=[e for e in events if e['event'] in {'message_insert_seen','message_update_seen'}]
    sockets=[e for e in events if e['event']=='socket_enter']
    lines=['# 微信发送过程：实测记录','',
           f'网络发送调用：{len(sockets)} 次。测试消息数据库观察：{len(messages)} 次。','',
           '网络调用包括心跳、同步与其他活动。下表的时间接近只能用于筛选线索，不能据此证明某个网络包就是测试消息。','']
    if messages:
        chats={}
        def chat(e):
            key=(e['source'],e.get('conversation_table','filehelper'))
            return chats.setdefault(key,len(chats)+1)
        lines+=['## 测试消息的数据库变化','','| 本机观察时间 | 会话编号 | 变化 | local_id | server_id |','|---|---|---|---:|---|']
        for e in messages:lines.append(f"| {e['observed_at']} | {chat(e)} | {e['event']} | {e['local_id']} | {e['server_id']} |")
        lines+=['','轮询每轮暂停 200 毫秒，查询耗时另计；未观察到的中间状态可能已在两次轮询之间完成。`server_id` 非零不证明对方已经阅读。','']
        for message in messages:
            if message['event']!='message_insert_seen':continue
            anchor=dt.datetime.fromisoformat(message['observed_at']).timestamp()*1000
            lines += [f"## 会话 {chat(message)} / 消息 {message['local_id']} 入库观察前后 3 秒的网络调用",'',
                      '| 相对首次观察 / ms | API | 字节数 | 线程 | 最近的微信调用帧 |','|---:|---|---:|---:|---|']
            for e in sockets:
                delta=e['wall_ms']-anchor
                if abs(delta)<=3000:
                    frames=' ← '.join(s for s in e.get('stack',[]) if 'weixin' in s.lower())
                    lines.append(f"| {delta:+.0f} | {e['api']} | {e['bytes']} | {e['thread']} | `{frames}` |")
            lines.append('')
    else:lines+=['尚未捕捉到带指定标记的新测试消息。当前日志只证明观察到了微信的网络发送活动。','']
    frames=collections.Counter(frame for e in sockets for frame in e.get('stack',[]) if 'weixin' in frame.lower())
    lines+=['','## 调用栈线索','','| 模块偏移 | 出现次数 |','|---|---:|']
    lines += [f'| `{frame}` | {count} |' for frame,count in frames.most_common(10)]
    lines += ['','偏移指调用栈中的返回位置，不是已经确认的函数入口。它们只适用于当前二进制；当前日志未定位业务发送入口、序列化或加密函数。','',
              '## 如何理解这个实验','',
              '1. 手动点击发送是你提供的实验动作。',
              '2. Frida 在 Windows socket API 入口/返回处记录字节数、线程和调用栈，未读取网络载荷。',
              '3. 独立查询监听范围内本人发送的标记测试消息，记录已提交数据库中可见的字段变化。',
              '4. 两条时间线帮助定位后续研究位置；实际业务函数、任务队列、封包和应答过程仍需进一步验证。','',
              'Windows `send` 成功返回不保证远端已收到。参考：[Microsoft send 文档](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-send)。',
              '入口与返回观察机制参考：[Frida Interceptor](https://frida.re/docs/javascript-api/#interceptor)。','']
    path=folder/'发送过程实测.md';path.write_text('\n'.join(lines),'utf-8')
    return {'report':str(path),'message_observations':len(messages),'socket_calls':len(sockets)}


if __name__=='__main__':print(json.dumps(analyze(sys.argv[1]),ensure_ascii=True))
