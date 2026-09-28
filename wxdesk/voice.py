"""Send one native Weixin voice bubble using the user's microphone.

The verified 4.1.15.13 path is the right Alt hold shortcut. It does not move
the pointer; keyboard focus is briefly transferred to the already-open chat.
"""
from __future__ import annotations

import ctypes
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes


class VoiceSender:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.held = False

    def release(self):
        import win32api
        import win32con
        with self.lock:
            if self.held:
                win32api.keybd_event(win32con.VK_RMENU, win32api.MapVirtualKey(win32con.VK_RMENU, 0),
                                     win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0)
                self.held = False

    def _press(self):
        import win32api
        import win32con
        with self.lock:
            if self.held: raise ValueError('已有语音正在录制。')
            win32api.keybd_event(win32con.VK_RMENU, win32api.MapVirtualKey(win32con.VK_RMENU, 0),
                                 win32con.KEYEVENTF_EXTENDEDKEY, 0)
            self.held = True

    @staticmethod
    def _voice_count(root):
        stack, count = [root], 0
        while stack:
            control = stack.pop()
            if 'ChatVoiceItemView' in (control.ClassName or ''):
                count += 1
            stack.extend(control.GetChildren())
        return count

    @contextmanager
    def _accessibility(self, driver, hwnd):
        from mywxplus._vendor.wechatauto.uia_driver import (
            SPI_GETSCREENREADER, SPI_SETSCREENREADER, SPIF_SENDCHANGE,
            PROCESS_QUERY_INFORMATION, PROCESS_VM_READ, PROCESS_VM_WRITE, PROCESS_VM_OPERATION)
        pid = driver._pid_from_hwnd(hwnd)
        module = driver._weixin_dll_module(pid)
        if not module: raise ValueError('无法读取当前微信的 UIA 控件。')
        base, _, path = module
        rvas = driver._qaccessible_candidate_rvas(path)[:4]
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                                    PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
        if not handle: raise ValueError('无法访问当前微信进程。')
        flag = wintypes.BOOL()
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(flag), 0)
        old = {rva: driver._read_process_byte(handle, int(base) + int(rva)) for rva in rvas}
        try:
            driver._set_screen_reader_flag(True)
            if not driver._hot_activate_accessibility(hwnd):
                raise ValueError('当前微信的 UIA 控件未成功加载。')
            yield
        finally:
            for rva, value in old.items():
                if value is not None and driver._read_process_byte(handle, int(base) + int(rva)) != value:
                    driver._write_process_byte(handle, int(base) + int(rva), value)
            ctypes.windll.user32.SystemParametersInfoW(SPI_SETSCREENREADER, 1 if flag.value else 0,
                                                       0, SPIF_SENDCHANGE)
            kernel.CloseHandle(handle)

    def send(self, context, payload):
        import comtypes
        import uiautomation as auto
        import win32gui
        from mywxplus._vendor.wechatauto.uia_driver import WeChatUIA

        account_id, cid = payload.get('account_id'), payload.get('cid')
        duration = payload.get('duration')
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1.5 <= duration <= 10:
            raise ValueError('录音时长应为 1.5–10 秒。')
        if self.app.account()['id'] != account_id:
            raise ValueError('当前登录账号已切换，语音未发送。')
        name = self.app.automation._direct_target(cid, '语音')
        with self.app.archive(account_id).connect() as db:
            row = db.execute('SELECT kind FROM conversations WHERE id=?', (cid,)).fetchone()
        if not row or row['kind'] != 'direct':
            raise ValueError('目前原生语音仅支持好友单聊。')
        comtypes.CoInitialize()
        try:
            with self.app.automation.gui_lock:
                driver = WeChatUIA(search_timeout=.5)
                pids = set(self.app.account()['pids'])
                hwnd = next((h for h in driver._wechat_hwnds() if driver._pid_from_hwnd(h) in pids), None)
                if not hwnd: raise ValueError('找不到当前账号的微信窗口。')
                previous = win32gui.GetForegroundWindow()
                try:
                    with self._accessibility(driver, hwnd):
                        driver._win = auto.ControlFromHandle(hwnd)
                        if driver.current_chat() != name:
                            raise ValueError('请先在微信中打开同一位好友的聊天，再点击录音；没有发送。')
                        before = self._voice_count(driver._win)
                        if not driver._force_foreground(hwnd):
                            raise ValueError('无法将当前微信窗口置于前台；没有发送。')
                        if driver.current_chat() != name or self.app.account()['id'] != account_id:
                            raise ValueError('微信会话或账号已变化；没有发送。')
                        context.progress(20, '正在用电脑麦克风录制原生语音')
                        def record():
                            self._press()
                            try:
                                time.sleep(duration)
                            finally:
                                self.release()
                        context.external('weixin.voice', {'account_id': account_id, 'cid': cid,
                                                          'duration': duration}, record)
                        context.progress(85, '已松开录音键，正在核对语音气泡')
                        time.sleep(.8)
                        after = self._voice_count(auto.ControlFromHandle(hwnd))
                        confirmed = after > before
                        return {'confirmed': confirmed,
                                'message': ('已在微信中看到新的原生语音气泡。' if confirmed else
                                            '录音操作已结束，但未在控件树确认语音气泡；请先到微信核对，避免重复发送。')}
                finally:
                    self.release()
                    if previous and previous != hwnd and win32gui.IsWindow(previous) and win32gui.GetForegroundWindow() == hwnd:
                        try: win32gui.SetForegroundWindow(previous)
                        except OSError: pass
        finally:
            comtypes.CoUninitialize()
