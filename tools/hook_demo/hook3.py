# -*- coding: utf-8 -*-
"""教学: hook "发消息"函数 —— 看到发出去的话、改写它、拿返回值。

用法: python hook3.py [进程名|pid] [观察秒数]
"""
import sys
import time
import frida

JS = r"""
Interceptor.attach(Module.getGlobalExportByName("send_message"), {
    onEnter: function (args) {
        // args[0]=收件人, args[1]=要发送的文本
        var to   = args[0].readUtf8String();
        var text = args[1].readUtf8String();
        send("拦截发送: to=" + to + "  text=\"" + text + "\"");

        // 演示"改参数":把要发出去的话换掉
        var modified = Memory.allocUtf8String("[被hook改写] " + text);
        args[1] = modified;                    // 第二个参数指针换成新的
    },
    onLeave: function (retval) {
        // 演示"读返回值":拿到发送后的消息 id
        send("  -> 发送完成, 消息 id=" + retval.toInt32());
    }
});
send("[+] 已挂到 send_message 上");
"""


def on_message(msg, data):
    t = msg.get("type")
    if t == "send":
        print("[hook]", msg["payload"])
    elif t == "error":
        print("[hook-ERROR]", msg.get("description"))


def attach(target):
    for _ in range(20):
        try:
            return frida.attach(target)
        except frida.ProcessNotFoundError:
            time.sleep(0.5)
    raise SystemExit("cannot attach: %s" % target)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "target3.exe"
    sec = float(sys.argv[2]) if len(sys.argv) > 2 else 6
    s = attach(name)
    sc = s.create_script(JS)
    sc.on("message", on_message)
    sc.load()
    time.sleep(sec)
    s.detach()
