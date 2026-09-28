"""Launch an additional Weixin instance after the version-matched disk patch."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .revoke_disk import running_weixin


def _main_ids(exe):
    ids = set()
    for process in running_weixin():
        try:
            if Path(process.exe()).resolve() == exe and not any(
                    arg.startswith('--type=') for arg in process.cmdline()):
                ids.add(process.pid)
        except Exception:
            continue
    return ids


def launch(context, payload, patcher):
    status = patcher.multi_status()
    if status['file_state'] != 'patched' or status['queued']:
        raise ValueError('请先启用多开补丁，关闭所有微信进程并重新启动微信。')
    exe = (Path(status['path']).resolve().parent.parent / 'Weixin.exe').resolve()
    if exe.name.lower() != 'weixin.exe' or not exe.is_file():
        raise ValueError('没有找到与补丁匹配的微信主程序。')
    previous = _main_ids(exe)
    context.progress(20, '正在启动另一个微信窗口')
    context.external('weixin.launch', {'exe': str(exe)},
                     lambda: subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True).pid)
    for index in range(40):
        context.check()
        current = _main_ids(exe)
        if current - previous:
            return {'opened': True, 'new_main_pids': sorted(current - previous),
                    'main_count': len(current), 'message': '新微信进程已启动，请在新窗口登录。'}
        time.sleep(0.2)
        if index % 5 == 0: context.progress(30 + int(index * 1.5), '等待新微信主进程')
    return {'opened': False, 'main_count': len(_main_ids(exe)),
            'message': '已执行启动，但没有检测到新主进程；请查看微信窗口和补丁状态。'}
