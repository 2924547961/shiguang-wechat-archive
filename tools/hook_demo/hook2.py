# -*- coding: utf-8 -*-
"""进阶:把钩子挂到具体处理器上,并演示"命中一次即撤销(detach)"。

用法: python hook2.py [进程名|pid] [观察秒数]
"""
import sys
import time
import frida

JS = r"""
// 挂到"红包处理器"上:命中一次就撤销钩子(后续红包不再拦截)
var redListener = Interceptor.attach(
    Module.getGlobalExportByName("handle_redpacket"),
    {
        onEnter: function (args) {
            send(">>> 监听到红包! type=" + args[0].toString() +
                 "  from=" + args[1].readUtf8String());
            // 立马撤销:把这个钩子从函数上摘掉
            redListener.detach();
            send("[撤销] redpacket 钩子已 detach —— 之后再来红包将不再拦截");
        }
    }
);

// 挂到"转账处理器"上:常驻,一直监听(对照用,不撤销)
Interceptor.attach(
    Module.getGlobalExportByName("handle_transfer"),
    {
        onEnter: function (args) {
            send("    [转账] type=" + args[0].toString() +
                 "  from=" + args[1].readUtf8String());
        }
    }
);

send("[+] 钩子已就绪: redpacket(一次即撤) + transfer(常驻)");
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
    name = sys.argv[1] if len(sys.argv) > 1 else "target2.exe"
    sec = float(sys.argv[2]) if len(sys.argv) > 2 else 6
    s = attach(name)
    sc = s.create_script(JS)
    sc.on("message", on_message)
    sc.load()
    time.sleep(sec)
    s.detach()
