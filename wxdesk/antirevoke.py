"""Reversible, current-process Weixin patch with an exact reviewed binary gate.

Rule reference: huiyadanli/RevokeMsgPatcher, Data/2.1/patch.json,
PatchVersion 20260816, Weixin >= 4.1.12.0, category 防撤回.
No remote rules or arbitrary addresses are accepted by the application API.
"""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import re
import struct
import threading
import psutil

LOCK = threading.RLock()
SUPPORTED_SHA256 = '10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5'
VERSION = '4.1.15.13'
UNSUPPORTED_REASON = '微信 4.1.15.13 已出现补丁写入成功但实际撤回仍生效的反馈；当前规则已停用。'
PATTERN = re.compile(re.escape(bytes.fromhex('48 89 86')) + b'.{4}' + re.escape(bytes.fromhex('4c 89 ad 08 02 00 00 4c 8d 05')), re.S)
_cache = {}


class Module(c.Structure):
    _fields_ = [('dwSize', w.DWORD), ('th32ModuleID', w.DWORD), ('th32ProcessID', w.DWORD),
                ('GlblcntUsage', w.DWORD), ('ProccntUsage', w.DWORD), ('modBaseAddr', c.c_void_p),
                ('modBaseSize', w.DWORD), ('hModule', w.HMODULE), ('szModule', w.WCHAR * 256),
                ('szExePath', w.WCHAR * 260)]


def kernel():
    if os.name != 'nt':
        raise ValueError('当前微信防撤回仅支持 Windows。')
    k = c.WinDLL('kernel32', use_last_error=True)
    declarations = {
        'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
        'Module32FirstW': ([w.HANDLE, c.POINTER(Module)], w.BOOL),
        'Module32NextW': ([w.HANDLE, c.POINTER(Module)], w.BOOL),
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'CloseHandle': ([w.HANDLE], w.BOOL),
        'ReadProcessMemory': ([w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL),
        'WriteProcessMemory': ([w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL),
        'VirtualProtectEx': ([w.HANDLE, c.c_void_p, c.c_size_t, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
        'FlushInstructionCache': ([w.HANDLE, c.c_void_p, c.c_size_t], w.BOOL),
    }
    for name, (args, result) in declarations.items():
        fn = getattr(k, name); fn.argtypes = args; fn.restype = result
    return k


def check(ok):
    if not ok:
        err = c.get_last_error()
        if err == 5:
            raise PermissionError('无法访问微信进程，请以管理员身份重新打开拾光后重试。')
        raise OSError(err, c.FormatError(err))


def target(k):
    candidates = []
    for p in psutil.process_iter(['pid', 'name']):
        if (p.info['name'] or '').lower() != 'weixin.exe':
            continue
        try:
            if any(arg.startswith('--type=') for arg in p.cmdline()):
                continue
            candidates.append(p)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise PermissionError('无法识别微信主进程，请以管理员身份重新打开拾光。')
    if len(candidates) != 1:
        raise ValueError('未找到唯一的微信主进程；请只保留一个正在登录的微信后重试。')
    p = candidates[0]
    snapshot = k.CreateToolhelp32Snapshot(0x18, p.pid)
    check(snapshot != c.c_void_p(-1).value)
    try:
        m = Module(); m.dwSize = c.sizeof(m)
        ok = k.Module32FirstW(snapshot, c.byref(m))
        while ok:
            if m.szModule.lower() == 'weixin.dll':
                return p, Path(m.szExePath), m.modBaseAddr
            ok = k.Module32NextW(snapshot, c.byref(m))
    finally:
        k.CloseHandle(snapshot)
    raise ValueError('微信尚未加载主模块，请完成登录后重试。')


def binary_plan(data):
    if hashlib.sha256(data).hexdigest() != SUPPORTED_SHA256:
        raise ValueError('当前微信文件尚未适配或已被修改，已停止操作。当前已核对版本：4.1.15.13。')
    matches = list(PATTERN.finditer(data))
    if len(matches) != 1:
        raise ValueError('防撤回特征不是唯一匹配，已停止操作。')
    offset = matches[0].start()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0' or struct.unpack_from('<H', data, pe+4)[0] != 0x8664:
        raise ValueError('微信模块格式不受支持。')
    count = struct.unpack_from('<H', data, pe+6)[0]
    optional = struct.unpack_from('<H', data, pe+20)[0]
    for i in range(count):
        section = pe + 24 + optional + i * 40
        rva, size, raw = struct.unpack_from('<III', data, section+12)
        flags = struct.unpack_from('<I', data, section+36)[0]
        if raw <= offset and offset + 17 <= raw + size and flags & 0x20000000:
            return rva + offset - raw, matches[0].group()
    raise ValueError('防撤回特征不在可执行代码段，已停止操作。')


def plan(path):
    stat = path.stat(); key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _cache:
        _cache.clear(); _cache[key] = binary_plan(path.read_bytes())
    return _cache[key]


def read(k, handle, address, size):
    data = c.create_string_buffer(size); n = c.c_size_t()
    check(k.ReadProcessMemory(handle, address, data, size, c.byref(n)))
    if n.value != size: raise ValueError('微信内存读取不完整。')
    return data.raw


def change(k, handle, address, expected, desired):
    # Only the opcode byte changes; never overwrite a multi-byte instruction in flight.
    if read(k, handle, address, len(expected)) != expected:
        raise ValueError('微信内存已发生变化，已停止操作。')
    old = w.DWORD(); ignored = w.DWORD(); n = c.c_size_t()
    check(k.VirtualProtectEx(handle, address+1, 1, 0x40, c.byref(old)))
    try:
        byte = c.create_string_buffer(desired[1:2])
        try:
            check(k.WriteProcessMemory(handle, address+1, byte, 1, c.byref(n)))
            if n.value != 1: raise ValueError('补丁写入不完整。')
            check(k.FlushInstructionCache(handle, address+1, 1))
            if read(k, handle, address, len(desired)) != desired:
                raise ValueError('补丁回读验证失败。')
        except Exception:
            rollback = c.create_string_buffer(expected[1:2])
            check(k.WriteProcessMemory(handle, address+1, rollback, 1, c.byref(n)))
            check(k.FlushInstructionCache(handle, address+1, 1))
            if read(k, handle, address, len(expected)) != expected:
                raise RuntimeError('补丁失败且回滚验证失败，请重启微信恢复。')
            raise
    finally:
        check(k.VirtualProtectEx(handle, address+1, 1, old.value, c.byref(ignored)))


def operate(action='status'):
    if action not in {'status', 'enable', 'disable'}:
        raise ValueError('防撤回操作无效。')
    if action == 'enable':
        raise ValueError(UNSUPPORTED_REASON)
    with LOCK:
        k = kernel(); process, path, base = target(k)
        rva, original = plan(path)
        patched = original[:1] + b'\x29' + original[2:]
        handle = k.OpenProcess(0x410 if action == 'status' else 0x438, False, process.pid)
        check(handle)
        try:
            current = read(k, handle, base+rva, len(original))
            if current not in (original, patched):
                raise ValueError('当前微信内存与已验证补丁不匹配，已停止操作。')
            desired = patched if action == 'enable' else original
            if action != 'status' and current != desired:
                change(k, handle, base+rva, current, desired)
                current = desired
            enabled = current == patched
            return {'enabled': enabled, 'supported': False, 'pid': process.pid,
                    'version': VERSION, 'path': str(path), 'verified': True,
                    'message': UNSUPPORTED_REASON + ('当前进程仍有旧补丁，请点击恢复原始行为。' if enabled else '当前进程为原始状态。')}
        finally:
            k.CloseHandle(handle)
