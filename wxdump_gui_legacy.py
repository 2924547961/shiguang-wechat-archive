# -*- coding: utf-8 -*-
"""
wxdump_gui —— wxdump 的图形界面外壳(tkinter)
================================================
在 wxdump.py 五条命令外再包一个可点按钮的小窗:
  ① 找数据   ② 取钥   ③ 验钥   ④ 导出   ⑤ 一键全流程

底层就是反复实测过的命令行,逐个按钮 = 依次调用:
  keys --data <目录> --acct <账号> [--pid N]
  verify --raw <raw> --out <verified>
  export --verified <verified> --out <导出目录>
  all  --data <目录> --acct <账号> --out <导出目录>

用法:
  python wxdump_gui.py          # 打开窗口(微信保持登录运行)
"""
import os
import re
import sys
import json
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
WXDUMP_PY = os.path.join(HERE, "wxdump.py")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("wxdump · 微信 4.x 本地库 取钥/验钥/导出")
        self.geometry("920x640")
        self.minsize(760, 520)
        self._q = queue.Queue()
        self._busy = False
        self._proc = None
        self._accounts = []            # [(dir_name, dbdir), ...]
        self._data_root = ""
        self._build_ui()
        self._poll_queue()
        self.after(200, self._auto_scan)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        f = ttk.Frame(self)
        f.pack(fill="x", **pad)

        ttk.Label(f, text="微信数据目录 (xwechat_files):").grid(row=0, column=0, sticky="w")
        self.var_root = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_root, width=46).grid(row=0, column=1, columnspan=2, sticky="we", pady=2)
        ttk.Button(f, text="浏览…", command=self._browse_root).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(f, text="账号:").grid(row=1, column=0, sticky="w")
        self.var_acct = tk.StringVar()
        self.cmb_acct = ttk.Combobox(f, textvariable=self.var_acct, state="readonly", width=44)
        self.cmb_acct.grid(row=1, column=1, columnspan=2, sticky="we", pady=2)

        ttk.Label(f, text="微信 PID(留空自动):").grid(row=2, column=0, sticky="w")
        self.var_pid = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_pid, width=12).grid(row=2, column=1, sticky="w", pady=2)

        self.var_nosnap = tk.BooleanVar(value=False)
        self.var_keepsnap = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="不建快照直接读(--no-snap)", variable=self.var_nosnap).grid(row=2, column=2, sticky="w")
        ttk.Checkbutton(f, text="导出后保留快照", variable=self.var_keepsnap).grid(row=2, column=3, sticky="w")

        ttk.Label(f, text="导出目录:").grid(row=3, column=0, sticky="w")
        self.var_outdir = tk.StringVar()
        ttk.Entry(f, textvariable=self.var_outdir, width=46).grid(row=3, column=1, columnspan=2, sticky="we", pady=2)
        ttk.Button(f, text="选择…", command=self._browse_out).grid(row=3, column=3, padx=(4, 0))

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.bt_find = ttk.Button(btns, text="① 找数据", command=lambda: self._run_thread(self._scan))
        self.bt_find.grid(row=0, column=0, padx=4)
        self.bt_keys = ttk.Button(btns, text="② 取钥(需微信运行)", command=self._do_keys)
        self.bt_keys.grid(row=0, column=1, padx=4)
        self.bt_ver = ttk.Button(btns, text="③ 验钥", command=self._do_verify)
        self.bt_ver.grid(row=0, column=2, padx=4)
        self.bt_exp = ttk.Button(btns, text="④ 导出聊天", command=self._do_export)
        self.bt_exp.grid(row=0, column=3, padx=4)
        self.bt_all = ttk.Button(btns, text="⑤ 一键全流程", command=self._do_all)
        self.bt_all.grid(row=0, column=4, padx=4)
        ttk.Button(btns, text="清空日志", command=self._clear_log).grid(row=0, column=5, padx=(24, 4))

        ttk.Label(self, text="实时日志:").pack(anchor="w", padx=8)
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.txt = tk.Text(wrap, state="disabled", bg="#101418", fg="#e6edf3",
                           insertbackground="white", wrap="word",
                           font=("Microsoft YaHei UI", 9))
        sb = ttk.Scrollbar(wrap, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.var_status, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    # ------------------------------------------------------------ helpers
    def _log(self, text):
        self._q.put(("log", text))

    def _set_status(self, text):
        self._q.put(("status", text))

    def _set_busy(self, busy):
        self._q.put(("busy", busy))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "busy":
                    self._busy = payload
                    st = "disabled" if payload else "normal"
                    for b in (self.bt_find, self.bt_keys, self.bt_ver,
                              self.bt_exp, self.bt_all):
                        b.configure(state=st)
                    if payload:
                        self.var_status.set("运行中…(请勿关闭微信;完成后会提示)")
                elif kind == "accts":
                    self._accounts = payload
                    names = [a[0] for a in payload]
                    self.cmb_acct["values"] = names
                    if names:
                        self.var_acct.set(names[0])
                elif kind == "done":
                    self._set_busy(False)
                    self._set_status(payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _append(self, text):
        self.txt.configure(state="normal")
        self.txt.insert("end", text)
        if not text.endswith("\n"):
            self.txt.insert("end", "\n")
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _run_thread(self, fn):
        if self._busy:
            return
        self._set_busy(True)
        threading.Thread(target=fn, daemon=True).start()

    # ----------------------------------------------------------- detection
    def _auto_scan(self):
        if self._busy:
            return
        self._log("—— 启动自动扫描 ——")
        self._run_thread(self._scan)

    def _scan(self):
        try:
            import wxdump
        except Exception as e:  # pragma: no cover
            self._log("无法导入 wxdump: %s" % e)
            self._set_busy(False)
            return
        try:
            root = self.var_root.get().strip() or None
            roots = wxdump.find_data_roots([root] if root else None)
        except Exception as e:
            self._log("扫描出错: %s" % e)
            roots = []
        if not roots:
            self._log("未找到微信数据目录(预期含 xwechat_files)。"
                      "若微信装在非常规位置,请手动在上方填数据目录再点 ①。")
            self._set_busy(False)
            return
        self._data_root = roots[0]
        self.var_root.set(self._data_root)
        self._log("数据根目录: %s" % self._data_root)
        try:
            accts = wxdump.accounts_of_root(self._data_root)
            running = wxdump.running_wechat_pids()
        except Exception as e:
            self._log("枚举账号出错: %s" % e)
            accts, running = [], []
        if not accts:
            self._log("该根目录下没有账号目录。")
        for name, dbdir in accts:
            n = len([f for f in wxdump.list_db_files(dbdir)])
            self._log("  账号 %-32s  %d 个 .db" % (name, n))
        if running:
            self._log("运行中的微信进程 PID: %s(取钥需要)" % ", ".join(map(str, running)))
        else:
            self._log("提示: 微信当前未运行 —— 取钥(keys)需要先登录运行微信;验钥/导出可离线。")
        try:
            served = wxdump.served_account_dirs(self._data_root)
            if served:
                self._log("微信当前打开的账号: %s —— 取钥只能取它,请让「账号」下拉选它。"
                          % "、".join(served))
            elif running:
                self._log("提示: 未能判定微信正打开的账号,取钥前请确认下拉账号=已登录账号。")
        except Exception:
            pass
        self._q.put(("accts", accts))
        self._set_busy(False)

    def _browse_root(self):
        d = filedialog.askdirectory(title="选择 xwechat_files 数据目录")
        if d:
            self.var_root.set(d)

    def _browse_out(self):
        d = filedialog.askdirectory(title="选择导出目录(将在此下新建导出文件夹)")
        if d:
            self.var_outdir.set(d)

    # ---------------------------------------------------------- subprocess
    def _acct(self):
        """返回当前选中的账号目录名(完整,如 wxid_xxx_abcd);自动补齐默认导出目录。"""
        acct = self.var_acct.get().strip()
        if not acct and self._accounts:
            acct = self._accounts[0][0]
        if not self.var_outdir.get().strip():
            self.var_outdir.set(os.path.join(HERE, "export_%s" % acct))
        return acct

    def _rawpaths(self, acct):
        raw = os.path.join(HERE, "keys_%s.raw.json" % acct)
        ver = os.path.join(HERE, "keys_%s.verified.json" % acct)
        return raw, ver

    def _start_cmd(self, cmd, tag):
        """禁用按钮并起一个线程跑命令;结束自动恢复。"""
        if self._busy:
            return
        self._set_busy(True)
        threading.Thread(target=self._run, args=(cmd, tag), daemon=True).start()

    def _run(self, cmd, tag):
        self._log("\n$ " + " ".join(cmd) + "\n")
        base = [sys.executable, "-u", WXDUMP_PY]
        try:
            p = subprocess.Popen(base + cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 encoding="utf-8", errors="replace",
                                 creationflags=getattr(subprocess,
                                                       "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self._q.put(("done", "启动失败: %s" % e))
            return
        self._proc = p
        while True:
            line = p.stdout.readline()
            if not line:
                break
            self._q.put(("log", line.rstrip("\n")))
        p.wait()
        self._proc = None
        rc = p.returncode
        if rc == 0:
            self._q.put(("done", "%s: 完成 (退出码 0)" % tag))
        else:
            self._q.put(("done", "%s: 失败/未完全成功 (退出码 %d) —— 见上方日志" % (tag, rc)))

    def _pid_flag(self):
        pid = self.var_pid.get().strip()
        return ["--pid", pid] if pid else []

    def _common_flags(self):
        fl = []
        if self.var_nosnap.get():
            fl.append("--no-snap")
        if self.var_keepsnap.get():
            fl.append("--keep-snap")
        return fl

    def _need(self, msg):
        if not self.var_root.get().strip() and not self._data_root:
            self._log("缺少微信数据目录 —— 先点 ① 找数据,或手动填写。")
            return False
        if not self._acct():
            self._log("缺少账号 —— 先点 ① 找数据。")
            return False
        self._log("提示: " + msg)
        return True

    # ------------------------------------------------------------- actions
    def _do_keys(self):
        if not self._need("取钥要求微信处于登录运行状态;多进程会自动逐个尝试。"):
            return
        acct = self._acct()
        raw, _ = self._rawpaths(acct)
        cmd = ["keys", "--data", self.var_root.get().strip() or self._data_root,
               "--acct", acct] + self._pid_flag() + ["--out", raw]
        self._start_cmd(cmd, "取钥")

    def _do_verify(self):
        acct = self._acct()
        raw, ver = self._rawpaths(acct)
        if not os.path.exists(raw):
            self._log("找不到候选清单 %s —— 请先点 ② 取钥。" % raw)
            return
        cmd = ["verify", "--raw", raw, "--out", ver]
        self._start_cmd(cmd, "验钥")

    def _do_export(self):
        if not self._need("微信运行时也会先做一致快照再导出;想最全最稳可先退出微信再导。"):
            return
        acct = self._acct()
        raw, ver = self._rawpaths(acct)
        if not os.path.exists(ver):
            self._log("找不到已验证清单 %s —— 请先点 ②取钥 ③验钥,再导出。" % ver)
            return
        outdir = self.var_outdir.get().strip() or os.path.join(HERE, "export_%s" % acct)
        cmd = ["export", "--verified", ver, "--out", outdir] + self._common_flags()
        self._start_cmd(cmd, "导出")

    def _do_all(self):
        if not self._need("一键 = 取钥→验钥→导出。导出目录同上。"):
            return
        acct = self._acct()
        outdir = self.var_outdir.get().strip() or os.path.join(HERE, "export_%s" % acct)
        cmd = ["all", "--data", self.var_root.get().strip() or self._data_root,
               "--acct", acct] + self._pid_flag() + \
              ["--out", outdir] + self._common_flags()
        self._start_cmd(cmd, "一键全流程")

    def destroy(self):
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
