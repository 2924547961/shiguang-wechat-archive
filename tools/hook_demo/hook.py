# -*- coding: utf-8 -*-
"""教学:用 Frida 拦截"消息分发"函数,识别红包/转账类型。

用法: python hook.py [进程名|pid] [观察秒数]
"""
import sys
import time
import frida

JS = r"""
var RED = ptr("0x7d100000031");   // 红包  local_type = 49 + 2001<<32
var TRN = ptr("0x7d000000031");   // 转账  local_type = 49 + 2000<<32

// Frida 17 的新 API: getGlobalExportByName(名字) 直接按导出名全局查找。
// (旧教程里的 Module.findExportByName / getExportByName 在 17.x 已移除。)
var addr = null;
try {
  addr = Module.getGlobalExportByName("dispatch_message");
} catch (e) {
  send("[!] 查找导出出错: " + e);
}

if (addr === null) {
  send("[!] dispatch_message 没找到(符号没导出?)");
} else {
  send("[+] 已定位 dispatch_message @ " + addr);

  // 挂钩子: 进函数前 onEnter,出函数后 onLeave(本例只用 onEnter)
  Interceptor.attach(addr, {
    onEnter: function (args) {
      // Windows x64 调用约定: args[0]=第1个参数(local_type), args[1]=第2个(from_user 指针)
      var typeHex = args[0].toString();            // 十六进制字符串,避开 JS 大整数丢精度
      var from = args[1].readUtf8String();         // 把指针当字符串读出来
      var label = "";
      if (args[0].equals(RED)) label = "   <<< RED PACKET (红包) >>>";
      else if (args[0].equals(TRN)) label = "   <<< TRANSFER (转账) >>>";
      send("intercept: type=" + typeHex + "  from=" + from + label);
    }
  });
}
"""


def on_message(msg, data):
    t = msg.get("type")
    if t == "send":
        print("[hook]", msg["payload"])
    elif t == "error":
        print("[hook-ERROR]", msg.get("description"))
        if msg.get("stack"):
            print(msg["stack"])
    else:
        print("[hook]", msg)


def attach(target):
    for _ in range(20):
        try:
            return frida.attach(target)
        except frida.ProcessNotFoundError:
            time.sleep(0.5)
    raise SystemExit("cannot attach: %s" % target)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "target.exe"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6
    sess = attach(name)
    sc = sess.create_script(JS)
    sc.on("message", on_message)
    sc.load()
    time.sleep(seconds)
    sess.detach()
