"""Apply the vendored RevokeMsgPatcher Weixin rule after every Weixin process exits.

The byte pattern comes from the GPL-3.0 upstream rule file. This module reports
file state only; effectiveness requires a real incoming-message recall test.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading

import psutil

from .common import atomic_json, read_json


RULE_FILE = Path(__file__).resolve().parent.parent / 'third_party' / 'RevokeMsgPatcher' / 'RevokeMsgPatcher.Assistant' / 'Data' / '2.1' / 'patch.json'
VERSION_RE = re.compile(r'^\d+\.\d+\.\d+\.\d+$')


def _version(value):
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ValueError('无法识别微信安装版本。')
    return tuple(map(int, value.split('.')))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def running_weixin():
    """Count all Weixin processes, including helpers that can retain the DLL."""
    result = []
    for proc in psutil.process_iter(['name', 'exe']):
        if (proc.info.get('name') or '').lower() == 'weixin.exe':
            result.append(proc)
    return result


def current_file():
    mains = []
    for proc in running_weixin():
        try:
            if not any(arg.startswith('--type=') for arg in proc.cmdline()):
                mains.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    paths = set()
    for proc in mains:
        try:
            exe = Path(proc.exe()).resolve()
            import win32api
            info = win32api.GetFileVersionInfo(str(exe), '\\')
            version = '.'.join(map(str, (info['FileVersionMS'] >> 16, info['FileVersionMS'] & 65535,
                                         info['FileVersionLS'] >> 16, info['FileVersionLS'] & 65535)))
            candidate = exe.parent / version / 'Weixin.dll'
            if candidate.is_file():
                paths.add(candidate.resolve())
        except (OSError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(paths) != 1:
        raise ValueError('请先登录唯一的微信安装版本，再预约关闭后安装补丁。')
    return paths.pop()


def _rule(path, category='防撤回'):
    version = _version(path.parent.name)
    bundle = json.loads(RULE_FILE.read_text(encoding='utf-8'))
    blocks = bundle['Apps']['Weixin']['FileCommonModifyInfos']['Weixin.dll']
    matching = [block for block in blocks if version >= _version(block['StartVersion']) and
                (not block.get('EndVersion') or version < _version(block['EndVersion']))]
    if len(matching) != 1:
        raise ValueError('上游规则尚未覆盖此微信版本。')
    patterns = [item for item in matching[0]['ReplacePatterns'] if item['Category'] == category]
    if not patterns:
        raise ValueError('此版本没有' + category + '规则。')
    return bundle['PatchVersion'], patterns


def _matches(data, pattern):
    expression = b''.join(b'.' if byte == 63 else re.escape(bytes([byte])) for byte in pattern)
    return [match.start() for match in re.finditer(expression, data, re.DOTALL)]


def inspect(path, category='防撤回'):
    path = Path(path).resolve()
    if path.name.lower() != 'weixin.dll' or not path.is_file():
        raise ValueError('目标必须是微信版本目录中的 Weixin.dll。')
    patch_version, patterns = _rule(path, category)
    data = path.read_bytes()
    changes, reverse_changes = [], []
    states = []
    for item in patterns:
        search, replacement = item['Search'], item['Replace']
        if len(search) != len(replacement):
            raise ValueError('上游补丁规则长度不一致。')
        original, patched = _matches(data, search), _matches(data, replacement)
        if (len(original), len(patched)) == (1, 0):
            states.append('original')
            changes.append((original[0], search, replacement))
        elif (len(original), len(patched)) == (0, 1):
            states.append('patched')
            reverse_changes.append((patched[0], replacement, search))
        else:
            raise ValueError(category + '特征不唯一或文件已被其他补丁修改，已停止操作。')
    if len(set(states)) != 1:
        raise ValueError('补丁处于部分写入状态，已停止操作。')
    return {'path': str(path), 'version': path.parent.name, 'patch_version': patch_version,
            'state': states[0], 'sha256': _sha(data), 'changes': changes,
            'reverse_changes': reverse_changes, 'data': data}


class DiskPatcher:
    def __init__(self, state):
        self.state = Path(state)
        self.path = self.state / 'revoke_disk.json'
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None

    def _record(self):
        value = read_json(self.path)
        return value if isinstance(value, dict) else {}

    def status(self):
        with self.lock:
            record = self._record()
            target = Path(record['path']) if record.get('path') else current_file()
            plan = inspect(target)
            queued = record.get('desired') if record.get('path') == str(target) else None
            running = bool(running_weixin())
            error = record.get('error', '') if record.get('path') == str(target) else ''
            return {'version': plan['version'], 'path': plan['path'], 'patch_version': plan['patch_version'],
                    'file_state': plan['state'], 'queued': queued, 'running': running,
                    'supported': True, 'enabled': False, 'verified': False, 'error': error,
                    'message': ('上次操作失败：' + error) if error else
                               ('已预约；全部微信进程关闭后自动' + ('安装' if queued == 'patched' else '恢复') + '。') if queued else
                               ('补丁已写入文件；重新启动微信后请实际测试对方撤回。' if plan['state'] == 'patched' else
                                '文件可匹配上游规则；可预约关闭微信后安装。')}

    def queue(self, desired):
        if desired not in {'patched', 'original'}:
            raise ValueError('补丁操作无效。')
        with self.lock:
            record = self._record()
            target = Path(record['path']) if record.get('path') else current_file()
            plan = inspect(target)
            if desired == 'original' and plan['state'] == 'original':
                atomic_json(self.path, {**record, 'path': str(target), 'desired': None, 'error': ''})
            elif plan['state'] == desired:
                atomic_json(self.path, {**record, 'path': str(target), 'desired': None, 'error': ''})
            else:
                atomic_json(self.path, {**record, 'path': str(target), 'desired': desired, 'error': ''})
        return self.status()

    def multi_status(self):
        with self.lock:
            record = self._record()
            target = Path(record['path']) if record.get('path') else current_file()
            plan = inspect(target, '多开')
            return {'version': plan['version'], 'file_state': plan['state'],
                    'queued': record.get('multi_desired'), 'running': bool(running_weixin()),
                    'error': record.get('multi_error', ''), 'path': plan['path']}

    def queue_multi(self, desired):
        if desired not in {'patched', 'original'}: raise ValueError('多开操作无效。')
        with self.lock:
            record = self._record()
            target = Path(record['path']) if record.get('path') else current_file()
            plan = inspect(target, '多开')
            record.update(path=str(target), multi_desired=desired if plan['state'] != desired else None,
                          multi_error='')
            atomic_json(self.path, record)
        return self.multi_status()

    def apply_pending(self):
        with self.lock:
            record = self._record()
            category = '防撤回' if record.get('desired') in {'patched', 'original'} else '多开'
            desired_key = 'desired' if category == '防撤回' else 'multi_desired'
            desired = record.get(desired_key)
            if desired not in {'patched', 'original'}:
                return False
            if running_weixin():
                return False
            target = Path(record['path']).resolve()
            plan = inspect(target, category)
            if plan['state'] == desired:
                record[desired_key] = None
                atomic_json(self.path, record)
                return True
            backup = self.state / ('weixin-' + plan['version'] + '.original.bak')
            if not backup.exists():
                temp = backup.with_suffix('.tmp')
                shutil.copy2(target, temp)
                if _sha(temp.read_bytes()) != plan['sha256']:
                    raise ValueError('备份验证失败，停止安装。')
                os.replace(temp, backup)
                record['original_sha256'] = plan['sha256']
            changes = plan['changes'] if desired == 'patched' else plan['reverse_changes']
            data = bytearray(plan['data'])
            for offset, source, replacement in changes:
                for index, value in enumerate(replacement):
                    if value != 63: data[offset + index] = value
            expected = _sha(data)
            if running_weixin() or _sha(target.read_bytes()) != plan['sha256']:
                raise ValueError('微信文件或进程已变化，停止安装。')
            try:
                with target.open('r+b') as stream:
                    for offset, source, replacement in changes:
                        stream.seek(offset)
                        stream.write(data[offset:offset + len(replacement)])
                    stream.flush(); os.fsync(stream.fileno())
                if _sha(target.read_bytes()) != expected or inspect(target, category)['state'] != desired:
                    raise ValueError('补丁写入校验失败。')
            except Exception:
                # Restore exactly the bytes seen before this operation; another
                # independent patch may already be present in the file.
                with target.open('r+b') as stream:
                    for offset, source, replacement in changes:
                        stream.seek(offset)
                        stream.write(plan['data'][offset:offset + len(source)])
                    stream.flush(); os.fsync(stream.fileno())
                raise
            if category == '防撤回': record['patched_sha256'] = expected
            record[desired_key] = None
            record['error' if category == '防撤回' else 'multi_error'] = ''
            atomic_json(self.path, record)
            return True

    def start(self):
        if self.thread:
            return
        def watch():
            while not self.stop_event.wait(2):
                try:
                    self.apply_pending()
                except Exception as exc:
                    with self.lock:
                        record = self._record()
                        key = 'error' if record.get('desired') else 'multi_error'
                        record[key] = str(exc)[:300]
                        record['desired' if key == 'error' else 'multi_desired'] = None
                        atomic_json(self.path, record)
        self.thread = threading.Thread(target=watch, name='revoke-disk-patcher', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
