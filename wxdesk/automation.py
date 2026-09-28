"""Opt-in message rules and UIA actions for the locally logged-in account."""
from __future__ import annotations

import json
import os
import hashlib
import random
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from .common import atomic_json, read_json
from .store import friends_clause
from .roles import ROLES, ROLE_DETAILS, BASE_RULES

PROVIDERS = {
    'deepseek': ('DeepSeek', 'https://api.deepseek.com', 'deepseek-chat'),
    'openai': ('OpenAI', 'https://api.openai.com/v1', 'gpt-4o-mini'),
    'siliconflow': ('硅基流动', 'https://api.siliconflow.cn/v1', ''),
    'openrouter': ('OpenRouter', 'https://openrouter.ai/api/v1', ''),
    'ollama': ('Ollama 本机', 'http://127.0.0.1:11434/v1', ''),
    'custom': ('自定义兼容接口', '', ''),
}


def emoji_files(folder):
    if not folder.is_dir():
        return []
    return [path for path in folder.rglob('*') if path.is_file() and path.suffix.lower() in {'.gif', '.png', '.jpg', '.jpeg'}][:2000]


class Automation:
    def __init__(self, application):
        self.app = application
        self.path = application.state / 'automation.json'
        self.config = read_json(self.path) or {}
        self.config.setdefault('enabled', False)
        self.config.setdefault('contacts', [])
        self.config.setdefault('reply', '')
        self.config.setdefault('mode', 'fixed')
        self.config.setdefault('api_url', 'https://api.deepseek.com')
        self.config.setdefault('model', 'deepseek-flash')
        self.config.setdefault('provider_id', 'deepseek')
        self.config.setdefault('provider_profiles', {})
        self.config.setdefault('system_prompt', '请用简洁、自然的中文回复。')
        self.config.setdefault('contact_prompts', {})
        self.config.setdefault('role_id', 'natural')
        self.config.setdefault('contact_roles', {})
        self.config.setdefault('batch_seconds', 0)
        self.config.setdefault('time_awareness', False)
        self.config.setdefault('image_recognition', False)
        self.config.setdefault('emoji_recognition', False)
        self.config.setdefault('context_turns', 0)
        self.config.setdefault('random_emoji', False)
        self.config.setdefault('emoji_dir', '')
        self.config.setdefault('emoji_probability', 25)
        self.config.setdefault('watermarks', {})
        self.config.setdefault('moment_auto_like', False)
        self.config.setdefault('moment_auto_comment', False)
        self.config.setdefault('moment_auto_reply', False)
        self.config.setdefault('moment_self_name', '')
        self.config.setdefault('moment_prompt', '友好、自然、具体地回应这条朋友圈，不要套话，不要杜撰。')
        self.config.setdefault('moment_interval', 60)
        self.config.setdefault('moment_seen', {})
        self.secret_dir = (application.state if getattr(application, 'demo', False) else
                           Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'Shiguang' / 'secrets')
        self.secret_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_secrets(application.state)
        self.secret_path = self.secret_dir / 'automation_secret.json'
        self.events_path = application.state / 'automation_events.json'
        past_events = read_json(self.events_path)
        self.events = deque((x for x in past_events if isinstance(x, dict)) if isinstance(past_events, list) else [], maxlen=100)
        self.events_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.gui_lock = threading.Lock()
        self.last_sent = {}
        self.pending = {}
        self.reply_workers = None
        self.scheduled_executor = None
        self.scheduled_future = None
        self.moment_executor = None
        self.moment_future = None
        self.moment_next_check = 0.0
        self.active_replies = {}
        self.reply_lock = threading.Lock()
        self.wake = threading.Event()
        self.send_jobs = {}
        self.send_jobs_lock = threading.Lock()
        self.scheduled_path = application.state / 'scheduled_messages.json'
        self.scheduled = read_json(self.scheduled_path) or []
        if not isinstance(self.scheduled, list):
            self.scheduled = []
        interrupted = False
        for job in self.scheduled:
            if isinstance(job, dict) and job.get('status') == 'running':
                job.update(status='uncertain', result='程序中断，发送结果待微信会话核对；不会自动重发。')
                interrupted = True
        if interrupted:
            atomic_json(self.scheduled_path, self.scheduled)
        self._uia_local = threading.local()
        self.thread = None

    def status(self):
        try:
            current_account = self.app.account()['id']
        except ValueError:
            current_account = None
        profiles = self._profiles(current_account)
        return {'enabled': bool(self.config['enabled'] and self.config.get('account_id') == current_account),
                'contacts': list(self.config['contacts']),
                'reply': self.config['reply'], 'mode': self.config['mode'],
                'api_url': self.config['api_url'], 'model': self.config['model'],
                'provider_id': self.config['provider_id'], 'providers': profiles,
                'system_prompt': self.config['system_prompt'],
                'contact_prompts': self.config['contact_prompts'],
                'role_id': self.config['role_id'], 'contact_roles': self.config['contact_roles'],
                'roles': [{'id': key, 'name': name, 'description': description, 'details': ROLE_DETAILS[key]}
                          for key, (name, description) in ROLES.items()],
                'batch_seconds': self.config['batch_seconds'],
                'time_awareness': self.config['time_awareness'],
                'image_recognition': self.config['image_recognition'],
                'emoji_recognition': self.config['emoji_recognition'],
                'context_turns': self.config['context_turns'],
                'random_emoji': self.config['random_emoji'],
                'emoji_dir': self.config['emoji_dir'],
                'emoji_probability': self.config['emoji_probability'],
                'moment_auto_like': self.config['moment_auto_like'],
                'moment_auto_comment': self.config['moment_auto_comment'],
                'moment_auto_reply': self.config['moment_auto_reply'],
                'moment_self_name': self.config['moment_self_name'],
                'moment_prompt': self.config['moment_prompt'],
                'moment_interval': self.config['moment_interval'],
                'scheduled': [dict(j) for j in self.scheduled if j.get('account_id') == current_account][-30:],
                'has_key': bool(read_json(self._secret_for(self.config['provider_id'])).get('account_id') == current_account),
                'events': list(self.events)[-30:]}

    def _secret_for(self, provider_id):
        return self.secret_path if provider_id == 'deepseek' else self.secret_dir / ('automation_secret_' + provider_id + '.json')

    def _migrate_legacy_secrets(self, old_state):
        if Path(old_state).resolve() == Path(self.secret_dir).resolve(): return
        from .llm import load_key, write_protected_key
        for old in Path(old_state).glob('automation_secret*.json'):
            value = read_json(old)
            account_id = value.get('account_id') if isinstance(value, dict) else None
            if not account_id or not value.get('protected'): continue
            target = self.secret_dir / old.name
            try:
                original = load_key(old, account_id)
                if not original: continue
                if not target.exists(): write_protected_key(target, value)
                if load_key(target, account_id) == original:
                    old.unlink()
            except (OSError, ValueError):
                # Keep the older encrypted copy if verification was impossible.
                continue

    def _profiles(self, account_id):
        profiles = {}
        for provider_id, (title, default_url, default_model) in PROVIDERS.items():
            saved = self.config['provider_profiles'].get(provider_id, {})
            if provider_id == 'deepseek' and not saved:
                saved = {'api_url': self.config['api_url'], 'model': self.config['model']}
            profiles[provider_id] = {'title': title, 'api_url': saved.get('api_url', default_url),
                                     'model': saved.get('model', default_model),
                                     'has_key': bool(account_id and read_json(self._secret_for(provider_id)).get('account_id') == account_id)}
        return profiles

    def models(self, data):
        from .llm import list_models, load_key
        account_id = self.app.account()['id']
        provider_id = data.get('provider_id', self.config['provider_id'])
        if provider_id not in PROVIDERS:
            raise ValueError('服务商无效。')
        api_url = data.get('api_url', '')
        key = data.get('api_key', '')
        if not isinstance(api_url, str) or len(api_url) > 500 or not isinstance(key, str) or len(key) > 500:
            raise ValueError('模型连接设置无效。')
        if not key:
            key = load_key(self._secret_for(provider_id), account_id)
        return {'models': list_models(api_url, key)}

    def test_model(self, data):
        """Explicit connectivity check with no archived or private chat text."""
        from .llm import complete_long, load_key
        account_id = self.app.account()['id']
        provider_id = data.get('provider_id', self.config['provider_id'])
        if provider_id not in PROVIDERS:
            raise ValueError('服务商无效。')
        api_url, model, key = (data.get('api_url'), data.get('model'), data.get('api_key', ''))
        if not all(isinstance(v, str) for v in (api_url, model, key)) or not model.strip() or len(model) > 150 or len(api_url) > 500 or len(key) > 500:
            raise ValueError('请填写有效的接口地址与模型名称。')
        key = key or load_key(self._secret_for(provider_id), account_id)
        if provider_id != 'ollama' and not key:
            raise ValueError('请先输入 API Key，或使用已保存的密钥。')
        started = time.perf_counter()
        answer = complete_long(api_url, key, model, '这是模型连通性测试。只回复 OK。', 'ping', 512)
        if self.app.account()['id'] != account_id:
            raise ValueError('登录账号已变化，请重新测试。')
        return {'ok': True, 'latency_ms': round((time.perf_counter() - started) * 1000),
                'model': model, 'reply': answer[:100]}

    def update(self, data):
        account = self.app.account()
        if not account.get('active') or not account.get('has_archive'):
            raise ValueError('请先登录当前微信账号并完成聊天同步。')
        selected = data.get('contacts', self.config['contacts'])
        if not isinstance(selected, list) or len(selected) > 100 or any(not isinstance(x, str) for x in selected):
            raise ValueError('联系人列表无效，最多选择 100 位。')
        selected = list(dict.fromkeys(selected))
        reply = data.get('reply', self.config['reply'])
        if not isinstance(reply, str) or len(reply) > 500:
            raise ValueError('自动回复最多 500 字。')
        mode = data.get('mode', self.config['mode'])
        if mode not in {'fixed', 'ai'}:
            raise ValueError('回复模式无效。')
        api_url = data.get('api_url', self.config['api_url'])
        model = data.get('model', self.config['model'])
        provider_id = data.get('provider_id', self.config['provider_id'])
        if provider_id not in PROVIDERS:
            raise ValueError('服务商无效。')
        system_prompt = data.get('system_prompt', self.config['system_prompt'])
        contact_prompts = data.get('contact_prompts', self.config['contact_prompts'])
        role_id = data.get('role_id', self.config['role_id'])
        contact_roles = data.get('contact_roles', self.config['contact_roles'])
        batch_seconds = data.get('batch_seconds', self.config['batch_seconds'])
        time_awareness = data.get('time_awareness', self.config['time_awareness'])
        image_recognition = data.get('image_recognition', self.config['image_recognition'])
        emoji_recognition = data.get('emoji_recognition', self.config['emoji_recognition'])
        context_turns = data.get('context_turns', self.config['context_turns'])
        random_emoji = data.get('random_emoji', self.config['random_emoji'])
        emoji_dir = data.get('emoji_dir', self.config['emoji_dir'])
        emoji_probability = data.get('emoji_probability', self.config['emoji_probability'])
        moment_auto_like = data.get('moment_auto_like', self.config['moment_auto_like'])
        moment_auto_comment = data.get('moment_auto_comment', self.config['moment_auto_comment'])
        moment_auto_reply = data.get('moment_auto_reply', self.config['moment_auto_reply'])
        moment_self_name = data.get('moment_self_name', self.config['moment_self_name'])
        moment_prompt = data.get('moment_prompt', self.config['moment_prompt'])
        moment_interval = data.get('moment_interval', self.config['moment_interval'])
        if not all(isinstance(x, str) for x in (api_url, model, system_prompt)) or not model.strip() or len(model) > 100 or len(system_prompt) > 2000:
            raise ValueError('模型设置无效。')
        if not isinstance(contact_prompts, dict) or len(contact_prompts) > 100 or any(
            not isinstance(k, str) or not isinstance(v, str) or len(v) > 12000 for k, v in contact_prompts.items()
        ) or set(contact_prompts) - set(selected):
            raise ValueError('联系人提示词无效；只能为已选好友设置。')
        if role_id not in ROLES or not isinstance(contact_roles, dict) or set(contact_roles) - set(selected) or any(
            not isinstance(k, str) or v not in ROLES for k, v in contact_roles.items()
        ):
            raise ValueError('角色风格无效；只能为已选好友设置。')
        if isinstance(batch_seconds, bool) or not isinstance(batch_seconds, int) or not 0 <= batch_seconds <= 20:
            raise ValueError('合并等待时间应为 0–20 秒。')
        if not all(isinstance(x, bool) for x in (time_awareness, image_recognition, emoji_recognition)):
            raise ValueError('AI 功能开关无效。')
        if isinstance(context_turns, bool) or not isinstance(context_turns, int) or not 0 <= context_turns <= 10:
            raise ValueError('上下文消息数量应为 0–10。')
        if not isinstance(random_emoji, bool) or not isinstance(emoji_dir, str) or len(emoji_dir) > 500 or isinstance(emoji_probability, bool) or not isinstance(emoji_probability, int) or not 0 <= emoji_probability <= 100:
            raise ValueError('随机表情设置无效。')
        if not all(isinstance(x, bool) for x in (moment_auto_like, moment_auto_comment, moment_auto_reply)):
            raise ValueError('朋友圈自动操作开关无效。')
        if not isinstance(moment_self_name, str) or len(moment_self_name) > 80 or not isinstance(moment_prompt, str) or len(moment_prompt) > 2000:
            raise ValueError('朋友圈名称或提示词无效。')
        if isinstance(moment_interval, bool) or not isinstance(moment_interval, int) or not 30 <= moment_interval <= 3600:
            raise ValueError('朋友圈检查间隔应为 30–3600 秒。')
        if moment_auto_reply and not moment_self_name.strip():
            raise ValueError('自动回复评论前请填写你在朋友圈显示的昵称。')
        if random_emoji:
            folder = Path(emoji_dir)
            if not emoji_files(folder):
                raise ValueError('启用随机表情前，请选择含 GIF、PNG 或 JPG 图片的文件夹。')
        from .llm import endpoint, save_key
        endpoint(api_url)
        new_key = data.get('api_key', '')
        if new_key and (not isinstance(new_key, str) or len(new_key) > 500):
            raise ValueError('API Key 格式无效。')
        with self.app.archive().connect() as c:
            known = {r[0] for r in c.execute('SELECT username FROM contacts WHERE ' + friends_clause(c))}
            if set(selected) - known:
                raise ValueError('所选联系人必须来自当前账号的好友列表。')
            if data.get('enabled') and not selected:
                raise ValueError('启用前请选择联系人。')
            if data.get('enabled') and mode == 'fixed' and not reply.strip():
                raise ValueError('启用前请填写回复内容。')
            if data.get('enabled') and mode == 'ai' and provider_id != 'ollama' and not (new_key or read_json(self._secret_for(provider_id)).get('account_id') == account['id']):
                raise ValueError('启用 AI 回复前请填写 API Key。')
            if (moment_auto_comment or moment_auto_reply) and provider_id != 'ollama' and not (
                    new_key or read_json(self._secret_for(provider_id)).get('account_id') == account['id']):
                raise ValueError('启用朋友圈 AI 评论前，请先在 AI 模型页保存 API Key。')
            if data.get('enabled') and not self.config['enabled']:
                # Starting is prospective: never reply to an imported backlog.
                key = account['id']
                self.config['watermarks'][key] = c.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
        if new_key: save_key(self._secret_for(provider_id), account['id'], new_key)
        self.config['provider_profiles'][provider_id] = {'api_url': api_url.strip(), 'model': model.strip()}
        self.config.update(enabled=bool(data.get('enabled', self.config['enabled'])),
                           contacts=selected, reply=reply.strip(), account_id=account['id'],
                           mode=mode, provider_id=provider_id, api_url=api_url.strip(), model=model.strip(), system_prompt=system_prompt.strip())
        self.config.update(contact_prompts={k: v.strip() for k, v in contact_prompts.items() if v.strip()},
                           role_id=role_id, contact_roles={k: v for k, v in contact_roles.items() if v != role_id},
                           batch_seconds=batch_seconds, time_awareness=time_awareness,
                           image_recognition=image_recognition, emoji_recognition=emoji_recognition,
                           context_turns=context_turns, random_emoji=random_emoji,
                           emoji_dir=emoji_dir.strip(), emoji_probability=emoji_probability,
                           moment_auto_like=moment_auto_like, moment_auto_comment=moment_auto_comment,
                           moment_auto_reply=moment_auto_reply, moment_self_name=moment_self_name.strip(),
                           moment_prompt=moment_prompt.strip(), moment_interval=moment_interval)
        self.pending.clear()
        atomic_json(self.path, self.config)
        self.wake.set()
        return self.status()

    def start(self):
        if self.app.demo or self.thread:
            return
        self.reply_workers = ThreadPoolExecutor(max_workers=6, thread_name_prefix='reply-contact')
        self.scheduled_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='scheduled-send')
        self.moment_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='moment-automation')
        self.thread = threading.Thread(target=self._run, name='message-automation', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.reply_workers:
            self.reply_workers.shutdown(wait=False, cancel_futures=True)
        if self.scheduled_executor:
            self.scheduled_executor.shutdown(wait=False, cancel_futures=True)
        if self.moment_executor:
            self.moment_executor.shutdown(wait=False, cancel_futures=True)

    def _event(self, message):
        with self.events_lock:
            self.events.append({'time': time.strftime('%H:%M:%S'), 'message': message})
            try:
                atomic_json(self.events_path, list(self.events))
            except OSError:
                pass

    def _wait_for_desktop_idle(self):
        """Do not take focus while the user is actively using the desktop.

        WeChat 4.x requires real focus/clicks for sending. This gates those
        actions rather than claiming that UIA can send invisibly.
        """
        if os.name != 'nt' or self.thread is None: return
        import ctypes
        from ctypes import wintypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        user32 = ctypes.windll.user32
        while not self.stop_event.is_set():
            if not user32.GetLastInputInfo(ctypes.byref(info)):
                return
            idle_ms = (user32.GetTickCount() - info.dwTime) & 0xffffffff
            if idle_ms >= 1800:
                return
            self.stop_event.wait(0.2)
        raise RuntimeError('软件已退出，发送已取消。')

    @contextmanager
    def _restore_focus_after_send(self, driver):
        """Return focus to the user's window after the UIA send completes."""
        if os.name != 'nt':
            yield
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        previous = user32.GetForegroundWindow()
        try:
            yield
        finally:
            wechat_window = getattr(getattr(driver, '_win', None), 'NativeWindowHandle', None)
            current = user32.GetForegroundWindow()
            if previous and wechat_window and current == wechat_window and user32.IsWindow(previous) and current != previous:
                try:
                    user32.SetForegroundWindow(previous)
                except OSError:
                    pass

    def _send_once(self, name, message):
        # The vendor GUI retry path can occupy the desktop for 75 seconds and
        # may report a false DB-negative for self-sent messages. One UIA attempt
        # avoids duplicate sends; archive confirmation arrives independently.
        if getattr(self._uia_local, 'driver', None) is None:
            from mywxplus._vendor.wechatauto.uia_driver import WeChatUIA
            self._uia_local.driver = WeChatUIA()
        with self._restore_focus_after_send(self._uia_local.driver):
            if not self._uia_local.driver.send_text_to(message, name):
                raise ValueError('UIA 未确认发送操作；没有自动重试，请检查微信会话。')
        return '已操作发送，等待本地记录确认'

    def _run(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            while not self.stop_event.is_set():
                self.wake.wait(0.25)
                self.wake.clear()
                if self.stop_event.is_set(): break
                if self.scheduled_executor and (self.scheduled_future is None or self.scheduled_future.done()):
                    if self.scheduled_future is not None:
                        try:
                            self.scheduled_future.result()
                        except Exception as exc:
                            self._event('主动消息检查失败：' + str(exc)[:120])
                    self.scheduled_future = self.scheduled_executor.submit(self._run_scheduled)
                moment_enabled = any(self.config.get(k) for k in ('moment_auto_like', 'moment_auto_comment', 'moment_auto_reply'))
                if (moment_enabled and self.moment_executor and time.monotonic() >= self.moment_next_check
                        and (self.moment_future is None or self.moment_future.done())):
                    if self.moment_future is not None:
                        try:
                            self.moment_future.result()
                        except Exception as exc:
                            self._event('朋友圈自动操作失败：' + str(exc)[:120])
                    self.moment_next_check = time.monotonic() + self.config['moment_interval']
                    self.moment_future = self.moment_executor.submit(self._run_moment_automation)
                if not self.config['enabled']:
                    continue
                try:
                    self.poll_once()
                except Exception as exc:
                    self._event('监听失败：' + str(exc)[:160])
        finally:
            comtypes.CoUninitialize()

    def poll_once(self):
        account = self.app.account()
        if not account.get('active') or not account.get('has_archive'):
            self.pending.clear()
            return
        aid = account['id']
        if self.config.get('account_id') != aid:
            self.pending.clear()
            return
        selected = set(self.config['contacts'])
        if not selected:
            return
        with self.app.archive().connect() as c:
            if aid not in self.config['watermarks']:
                self.config['watermarks'][aid] = c.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
                atomic_json(self.path, self.config)
                return
            last = self.config['watermarks'][aid]
            rows = c.execute('SELECT id,conversation_id,body,kind,is_self,media_path FROM messages WHERE id>? ORDER BY id LIMIT 200', (last,)).fetchall()
            names = {r['id']: r['display'] for r in c.execute(
                'SELECT conversations.id,coalesce(nullif(contacts.remark,\'\'),nullif(contacts.nickname,\'\'),conversations.title) display '
                'FROM conversations LEFT JOIN contacts ON contacts.username=conversations.id WHERE conversations.id IN ('
                + ','.join('?' for _ in selected) + ')', tuple(selected))}
            ambiguous = {r[0] for r in c.execute('SELECT title FROM conversations GROUP BY title HAVING count(*)>1')}
        if rows:
            self.config['watermarks'][aid] = rows[-1]['id']
            atomic_json(self.path, self.config)
        for row in rows:
            cid = row['conversation_id']
            kind = row['kind']
            media_enabled = self.config['mode'] == 'ai' and (
                (kind == 'image' and self.config['image_recognition']) or
                (kind == 'emoji' and self.config['emoji_recognition']))
            if cid not in selected or row['is_self'] or (kind != 'text' and not media_enabled) or (kind == 'text' and not row['body']):
                continue
            name = names.get(cid)
            if not name or name in ambiguous:
                self._event('跳过重名联系人；请保持目标名称唯一。')
                continue
            pending = self.pending.setdefault(cid, {'name': name, 'bodies': [], 'images': [], 'updated': 0,
                                                    'created': time.monotonic(),
                                                    'first_id': row['id']})
            if media_enabled:
                relative = row['media_path'] or ''
                root = getattr(self.app.archive(), 'directory', None)
                path = (Path(root) / relative).resolve() if root and relative else None
                if not path or not path.is_relative_to(Path(root).resolve()) or not path.is_file():
                    self._event(name + '：图片尚未恢复到归档，无法识别。')
                    continue
                if len(pending['images']) < 4:
                    pending['images'].append(path)
                    pending['bodies'].append('[图片]' if kind == 'image' else '[表情包]')
            else:
                pending['bodies'].append(row['body'])
            pending['updated'] = time.monotonic()
        self._flush_pending(aid)

    def _flush_pending(self, account_id):
        now = time.monotonic()
        for cid, pending in list(self.pending.items()):
            if not pending['bodies']:
                del self.pending[cid]
                continue
            if now - pending['updated'] < self.config['batch_seconds'] and now - pending['created'] < max(5,self.config['batch_seconds']*2):
                continue
            with self.reply_lock:
                if cid in self.active_replies:
                    continue
            del self.pending[cid]
            self.last_sent[cid] = now
            if self.reply_workers:
                with self.reply_lock:
                    future = self.reply_workers.submit(self._process_pending, account_id, cid, pending)
                    self.active_replies[cid] = future
                future.add_done_callback(lambda done, contact=cid: self._reply_finished(contact, done))
            else:
                self._process_pending(account_id, cid, pending)

    def _reply_finished(self, cid, future):
        with self.reply_lock:
            if self.active_replies.get(cid) is future:
                self.active_replies.pop(cid, None)
        self.wake.set()

    def _reply_prompt(self, cid, account_id=None):
        from .personal import reply_memory
        role = self.config['contact_roles'].get(cid, self.config['role_id'])
        custom = self.config['contact_prompts'].get(cid, '')
        try:
            memory = reply_memory(self.app, account_id or self.app.account()['id'], cid)
        except (OSError, ValueError):
            memory = ''
        return '\n'.join((BASE_RULES, '当前回复风格：' + ROLES.get(role, ROLES['natural'])[1],
                          '角色执行规则：' + ROLE_DETAILS.get(role, ROLE_DETAILS['natural']),
                          '通用要求：' + self.config['system_prompt'],
                          '该好友专属要求：' + custom if custom else '', memory))

    def _process_pending(self, account_id, cid, pending):
        import comtypes
        comtypes.CoInitialize()
        try:
            self._process_pending_com(account_id, cid, pending)
        finally:
            comtypes.CoUninitialize()

    def _process_pending_com(self, account_id, cid, pending):
            name = pending['name']
            phase = '生成回复'
            try:
                message = self.config['reply']
                if self.config['mode'] == 'ai':
                    from .llm import complete, load_key
                    prompt = self._reply_prompt(cid, account_id)
                    if self.config['time_awareness']:
                        prompt += '\n当前本地时间：' + datetime.now().astimezone().strftime('%Y-%m-%d %A %H:%M %Z')
                    incoming = '\n'.join(pending['bodies'])[-4000:]
                    options = {'images': pending['images']} if pending['images'] else {}
                    if self.config['context_turns']:
                        with self.app.archive().connect() as c:
                            previous = c.execute(
                                "SELECT body,is_self FROM messages WHERE conversation_id=? AND id<? AND kind='text' "
                                "AND body<>'' ORDER BY id DESC LIMIT ?",
                                (cid, pending['first_id'], self.config['context_turns'])).fetchall()
                        options['history'] = [{'role': 'assistant' if item['is_self'] else 'user',
                                               'content': item['body'][:500]} for item in reversed(previous)]
                    message = complete(self.config['api_url'], load_key(self._secret_for(self.config['provider_id']), account_id),
                                       self.config['model'], prompt, incoming, name, **options)
                phase = '发送操作'
                with self.gui_lock:
                    self._wait_for_desktop_idle()
                    if self.app.account()['id']!=account_id or not self.config['enabled'] or cid not in self.config['contacts']:
                        self._event(name+'：账号或自动回复规则已变化，本批取消。')
                        return
                    result = self._send_once(name, message)
                self._event(name + '：' + result)
                self._maybe_send_emoji(name, account_id, cid)
            except Exception as exc:
                if phase == '生成回复':
                    self._event(name + '：AI 回复未生成（' + str(exc)[:120] + '）。请检查模型连接。')
                else:
                    # The UI send may have succeeded. Never retry this batch automatically.
                    self._event(name + '：发送结果待确认（' + str(exc)[:100] + '）')

    def _maybe_send_emoji(self, name, account_id=None, conversation_id=None):
        if not self.config['random_emoji'] or random.randrange(100) >= self.config['emoji_probability']:
            return
        folder = Path(self.config['emoji_dir'])
        files = emoji_files(folder)
        if not files:
            self._event(name + '：随机表情目录没有可用图片。')
            return
        try:
            from mywxplus._vendor.wechatauto.guia import WeChatGUI
            with self.gui_lock:
                self._wait_for_desktop_idle()
                if account_id and (self.app.account()['id']!=account_id or not self.config['enabled'] or conversation_id not in self.config['contacts']):
                    return
                if not WeChatGUI(calibrate=False).send_image(str(random.choice(files)), name):
                    raise ValueError('微信未确认图片发送操作。')
            self._event(name + '：已操作发送随机表情图片。')
        except Exception as exc:
            self._event(name + '：随机表情发送失败（' + str(exc)[:100] + '）')

    def schedule_message(self, conversation_id, message, due_at):
        name = self._direct_target(conversation_id, message)
        if isinstance(due_at, bool) or not isinstance(due_at, (int, float)) or not time.time() + 2 <= due_at <= time.time() + 30 * 86400:
            raise ValueError('请选择 2 秒后至 30 天内的发送时间。')
        job = {'id': secrets.token_hex(8), 'account_id': self.app.account()['id'],
               'conversation_id': conversation_id, 'name': name, 'message': message.strip(),
               'due_at': due_at, 'status': 'queued'}
        with self.send_jobs_lock:
            if sum(j.get('status') == 'queued' for j in self.scheduled) >= 100:
                raise ValueError('待发送消息已达到 100 条。')
            self.scheduled.append(job)
            atomic_json(self.scheduled_path, self.scheduled)
        return {'id': job['id'], 'status': job['status']}

    def cancel_scheduled(self, job_id):
        with self.send_jobs_lock:
            for job in self.scheduled:
                if job.get('id') == job_id and job.get('account_id') == self.app.account()['id'] and job.get('status') == 'queued':
                    job['status'] = 'cancelled'
                    atomic_json(self.scheduled_path, self.scheduled)
                    return {'ok': True}
        raise ValueError('这条主动消息无法取消，可能已经开始发送。')

    def schedule_multi(self, drafts, due_at):
        if not isinstance(drafts,list) or not 1<=len(drafts)<=20 or len({x.get('cid') for x in drafts if isinstance(x,dict)})!=len(drafts):
            raise ValueError('请选择 1–20 位不重复好友。')
        if isinstance(due_at,bool) or not isinstance(due_at,(int,float)) or not time.time()+2<=due_at<=time.time()+30*86400:
            raise ValueError('请选择 2 秒后至 30 天内的发送时间。')
        account_id=self.app.account()['id'];jobs=[]
        for item in drafts:
            if not isinstance(item,dict) or item.get('account_id')!=account_id:
                raise ValueError('登录账号已变化，请重新生成预览。')
            name=self._direct_target(item.get('cid'),item.get('message'))
            jobs.append({'id':secrets.token_hex(8),'account_id':account_id,'conversation_id':item['cid'],
                         'name':name,'message':item['message'].strip(),'due_at':due_at,'status':'queued'})
        with self.send_jobs_lock:
            if sum(j.get('status')=='queued' for j in self.scheduled)+len(jobs)>100:
                raise ValueError('待发送消息将超过 100 条。')
            self.scheduled.extend(jobs)
            atomic_json(self.scheduled_path,self.scheduled)
        return {'jobs':[{'id':j['id'],'name':j['name']} for j in jobs]}

    def _run_scheduled(self):
        try:
            account = self.app.account()
        except ValueError:
            return
        if not account.get('active') or not account.get('has_archive'):
            return
        now = time.time()
        with self.send_jobs_lock:
            due = [j for j in self.scheduled if j.get('account_id') == account['id'] and j.get('status') == 'queued' and j.get('due_at', 0) <= now]
            if not due:
                return
            job = min(due, key=lambda j: j['due_at'])
            job['status'] = 'running'
            atomic_json(self.scheduled_path, self.scheduled)
        try:
            self._uia_local.background_send = True
            self.send_direct(job['conversation_id'], job['message'], job['account_id'])
            status, note = 'done', '已操作发送，等待本地记录确认'
        except Exception as exc:
            status, note = 'uncertain', str(exc)[:150]
        finally:
            self._uia_local.background_send = False
        with self.send_jobs_lock:
            job.update(status=status, result=note)
            atomic_json(self.scheduled_path, self.scheduled)

    def send_direct(self, conversation_id, message, expected_account=None):
        if expected_account and self.app.account()['id'] != expected_account:
            raise ValueError('当前登录账号已变化，已取消这条发送。')
        name = self._direct_target(conversation_id, message)
        import comtypes
        comtypes.CoInitialize()
        try:
            from mywxplus._vendor.wechatauto.uia_driver import WeChatUIA
            with self.gui_lock:
                if getattr(self._uia_local, 'background_send', False):
                    self._wait_for_desktop_idle()
                if expected_account and self.app.account()['id'] != expected_account:
                    raise ValueError('当前登录账号已变化，已取消这条发送。')
                driver = WeChatUIA()
                with self._restore_focus_after_send(driver):
                    if not driver.send_text_to(message.strip(), name):
                        raise ValueError('UIA 未确认发送操作，请检查微信会话，避免重复点击。')
        finally:
            comtypes.CoUninitialize()
        self._event(name + '：手动发送已操作，等待本地记录确认')
        return {'ok': True, 'message': '已操作发送，等待本地记录确认'}

    def _direct_target(self, conversation_id, message):
        account = self.app.account()
        if not account.get('active') or not account.get('has_archive'):
            raise ValueError('请先登录当前微信账号并同步聊天。')
        if not isinstance(conversation_id, str) or not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ValueError('请选择会话并填写 1–2000 字的消息。')
        with self.app.archive().connect() as c:
            rows = c.execute('SELECT title FROM conversations WHERE id=?', (conversation_id,)).fetchall()
            if len(rows) != 1:
                raise ValueError('会话不存在。')
            name = rows[0][0]
            if c.execute('SELECT count(*) FROM conversations WHERE title=?', (name,)).fetchone()[0] != 1:
                raise ValueError('存在同名会话，无法确认发送目标。')
        return name

    def queue_direct(self, conversation_id, message, expected_account=None):
        if expected_account and self.app.account()['id']!=expected_account:
            raise ValueError('登录账号已变化，请重新生成发送预览。')
        self._direct_target(conversation_id, message)
        account_id=self.app.account()['id']
        if expected_account and account_id!=expected_account:
            raise ValueError('登录账号已变化，请重新生成发送预览。')
        with self.send_jobs_lock:
            if any(j['status'] == 'running' and j['conversation_id'] == conversation_id for j in self.send_jobs.values()):
                raise ValueError('此会话已有消息正在发送，请等待结果。')
            job_id = secrets.token_hex(8)
            self.send_jobs[job_id] = {'id': job_id, 'status': 'running', 'conversation_id': conversation_id,
                                      'account_id': account_id,
                                      'message': '正在交给微信发送'}
            if len(self.send_jobs) > 50:
                for old in list(self.send_jobs)[:-50]:
                    if self.send_jobs[old]['status'] != 'running':
                        del self.send_jobs[old]
        def run():
            try:
                result = self.send_direct(conversation_id, message, account_id)
                status, note = 'done', result['message']
            except Exception as exc:
                status, note = 'error', str(exc)[:200]
            with self.send_jobs_lock:
                self.send_jobs[job_id].update(status=status, message=note)
        threading.Thread(target=run, name='manual-send-' + job_id, daemon=True).start()
        return {'id': job_id, 'status': 'running', 'message': '正在交给微信发送'}

    def prepare_multi(self, contacts, mode, text):
        """Preview one message per recipient. This never sends through WeChat."""
        if not isinstance(contacts,list) or not 1<=len(contacts)<=20 or len(set(contacts))!=len(contacts):
            raise ValueError('请选择 1–20 位不重复的好友。')
        if mode not in {'fixed','topic'} or not isinstance(text,str) or not text.strip() or len(text)>1000:
            raise ValueError('请填写 1–1000 字的文字或主题。')
        account_id=self.app.account()['id']
        names={cid:self._direct_target(cid,text) for cid in contacts}
        if mode=='fixed':
            return {'drafts':[{'cid':cid,'name':names[cid],'message':text.strip(),'account_id':account_id} for cid in contacts]}
        from .llm import complete_long,load_key
        cfg=self.config
        key=load_key(self._secret_for(cfg['provider_id']),account_id)
        if cfg['provider_id']!='ollama' and not key:
            raise ValueError('请先在 AI 模型页设置 API Key。')
        drafts=[]
        with self.app.archive().connect() as db:
            for cid in contacts:
                if self.app.account()['id']!=account_id:
                    raise ValueError('登录账号已变化，请重新生成预览。')
                recent=db.execute("SELECT body,is_self FROM messages WHERE conversation_id=? AND kind='text' AND body<>'' ORDER BY ts DESC,id DESC LIMIT 8",(cid,)).fetchall()
                context='\n'.join(('我' if r['is_self'] else '对方')+'：'+r['body'][:200] for r in reversed(recent))
                instruction=('为用户拟一条将由用户预览和确认后发送给好友的私人消息。主题由用户指定。'
                             '根据最近聊天语境写自然、简短、尊重边界的内容；不要杜撰事实、承诺或利用对方弱点。'
                             '只输出可发送的消息正文，最多300字。')
                content=complete_long(cfg['api_url'],key,cfg['model'],instruction,
                                      '好友：'+names[cid]+'\n主题：'+text.strip()+'\n最近聊天：\n'+context,500)[:300]
                if self.app.account()['id']!=account_id:
                    raise ValueError('登录账号已变化，请重新生成预览。')
                drafts.append({'cid':cid,'name':names[cid],'message':content,'account_id':account_id})
        return {'drafts':drafts}

    def queue_multi(self, drafts):
        if not isinstance(drafts,list) or not 1<=len(drafts)<=20 or len({x.get('cid') for x in drafts if isinstance(x,dict)})!=len(drafts):
            raise ValueError('请确认 1–20 位不重复好友的发送草稿。')
        for item in drafts:
            if not isinstance(item,dict):raise ValueError('发送草稿无效。')
            if item.get('account_id')!=self.app.account()['id']:
                raise ValueError('登录账号已变化，请重新生成发送预览。')
            self._direct_target(item.get('cid'),item.get('message'))
        jobs=[]
        for item in drafts:
            try:jobs.append({'cid':item['cid'],**self.queue_direct(item['cid'],item['message'],item['account_id'])})
            except Exception as exc:jobs.append({'cid':item['cid'],'status':'error','message':str(exc)[:160]})
        return {'jobs':jobs}

    def direct_status(self, job_id):
        with self.send_jobs_lock:
            result = self.send_jobs.get(job_id)
            if result is None:
                raise ValueError('发送任务不存在或程序已重启；请核对微信会话。')
            return dict(result)

    @staticmethod
    def _moment_fingerprint(item):
        source='\x1f'.join([item.publisher or '',item.text or '',item.timestamp or '',str(item.image_count)])
        return hashlib.sha256(source.encode('utf-8')).hexdigest()

    @staticmethod
    def _accessibility_snapshot():
        """Capture the screen-reader flag and Weixin Qt gate for later restore."""
        if os.name != 'nt':
            return None
        import ctypes
        from mywxplus._vendor.wechatauto.uia_driver import WeChatUIA, SPI_GETSCREENREADER
        previous = ctypes.c_int(0)
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(previous), 0)
        gates = []
        engine = WeChatUIA()
        for hwnd in engine._wechat_hwnds():
            pid = engine._pid_from_hwnd(hwnd)
            module = engine._weixin_dll_module(pid) if pid else None
            if not module:
                continue
            base, _, dll = module
            for rva in engine._qaccessible_candidate_rvas(dll)[:4]:
                handle = ctypes.windll.kernel32.OpenProcess(0x0400|0x0010|0x0020|0x0008, False, pid)
                if not handle:
                    continue
                value = engine._read_process_byte(handle, base + rva)
                ctypes.windll.kernel32.CloseHandle(handle)
                if value is not None:
                    gates.append((pid, base + rva, value))
                    break
        return previous.value, gates

    @staticmethod
    def _restore_accessibility(snapshot):
        if not snapshot or os.name != 'nt':
            return
        import ctypes
        from mywxplus._vendor.wechatauto.uia_driver import WeChatUIA
        previous, gates = snapshot
        engine = WeChatUIA()
        for pid, address, value in gates:
            handle = ctypes.windll.kernel32.OpenProcess(0x0400|0x0010|0x0020|0x0008, False, pid)
            if handle:
                engine._write_process_byte(handle, address, value)
                ctypes.windll.kernel32.CloseHandle(handle)
        engine._set_screen_reader_flag(bool(previous))

    @staticmethod
    def _prepare_moment_uia():
        from mywxplus._vendor.wechatauto.uia_driver import WeChatUIA
        if not WeChatUIA().ensure_materialized(timeout=8, force=True):
            raise ValueError('当前微信没有可访问的朋友圈 UIA 树。')

    def _moment_ai_text(self, purpose, author, body, comment=''):
        from .llm import complete, load_key
        account_id = self.app.account()['id']
        key = load_key(self._secret_for(self.config['provider_id']), account_id)
        if self.config['provider_id'] != 'ollama' and not key:
            raise ValueError('AI 模型尚未保存密钥')
        instruction = (self.config['moment_prompt'] + '\n只输出一条可直接发送的中文，1至80字。'
                       '尊重对方，不评价隐私，不杜撰共同经历，不使用营销套话。')
        incoming = '朋友圈作者：' + author + '\n朋友圈正文：' + (body or '[无文字]')
        if purpose == 'reply':
            incoming += '\n对方评论：' + comment + '\n任务：回复这条评论。'
        else:
            incoming += '\n任务：评论这条朋友圈。'
        return complete(self.config['api_url'], key, self.config['model'], instruction,
                        incoming, author)[:80]

    def _run_moment_automation(self):
        """Process only newly archived posts/comments and persist dedupe keys."""
        import comtypes
        comtypes.CoInitialize()
        try:
            account = self.app.account()
            if not account.get('active') or not account.get('has_archive'):
                return
            aid = account['id']
            with self.app.archive().connect() as db:
                rows = db.execute('SELECT id,nickname,body,detail FROM moments ORDER BY ts DESC LIMIT 100').fetchall()
            known = set(self.config['moment_seen'].get(aid, []))
            current = set()
            candidates = []
            own_name = self.config['moment_self_name'].strip()
            for row in rows:
                mid, author, body = str(row['id']), (row['nickname'] or '').strip(), (row['body'] or '').strip()
                like_key, comment_post_key = 'like:' + mid, 'comment:' + mid
                current.update((like_key, comment_post_key))
                if author and author != own_name:
                    if self.config['moment_auto_like'] and like_key not in known:
                        candidates.append(('like', mid, author, body, '', ''))
                    if self.config['moment_auto_comment'] and comment_post_key not in known:
                        candidates.append(('comment', mid, author, body, '', ''))
                try:
                    detail = json.loads(row['detail'] or '{}')
                except (TypeError, ValueError):
                    detail = {}
                if own_name and author == own_name:
                    for item in detail.get('comments') or []:
                        who = (item.get('nickname') or item.get('username') or '').strip()
                        content = (item.get('content') or '').strip()
                        if not who or not content or who == own_name:
                            continue
                        comment_key = 'reply:' + hashlib.sha256((mid+'\0'+who+'\0'+content).encode()).hexdigest()
                        current.add(comment_key)
                        if comment_key not in known:
                            candidates.append(('reply', mid, author, body, who, content))
            if aid not in self.config['moment_seen']:
                self.config['moment_seen'][aid] = sorted(current)[-1500:]
                atomic_json(self.path, self.config)
                self._event('朋友圈自动操作已建立起点，只处理之后出现的新动态和评论。')
                return
            completed = set(known)
            actions = 0
            for kind, mid, author, body, who, content in candidates:
                if actions >= 3 or self.stop_event.is_set():
                    break
                try:
                    if kind == 'like':
                        self._wait_for_desktop_idle()
                        self.moment_action('like', mid)
                        actions += 1
                        completed.add('like:' + mid)
                    elif kind == 'comment':
                        text = self._moment_ai_text('comment', author, body)
                        self._wait_for_desktop_idle()
                        self.moment_action('comment', mid, text)
                        actions += 1
                        completed.add('comment:' + mid)
                    elif self.config['moment_auto_reply']:
                        text = self._moment_ai_text('reply', who, body, content)
                        self._wait_for_desktop_idle()
                        self.moment_action('reply', mid, text, reply_to=who, target_text=content)
                        actions += 1
                        completed.add('reply:' + hashlib.sha256((mid+'\0'+who+'\0'+content).encode()).hexdigest())
                except Exception as exc:
                    self._event(('回复评论' if kind == 'reply' else ('点赞' if kind == 'like' else '评论')) + '失败：' + str(exc)[:120])
            # Keep successful keys plus current baseline; failed candidates remain retryable.
            self.config['moment_seen'][aid] = sorted(completed | (current & known))[-1500:]
            atomic_json(self.path, self.config)
        finally:
            comtypes.CoUninitialize()

    def live_moments(self):
        if not self.app.account().get('active'):
            raise ValueError('请先登录微信。')
        import comtypes
        comtypes.CoInitialize()
        accessibility = self._accessibility_snapshot()
        try:
            self._prepare_moment_uia()
            from mywxplus._vendor.wechatauto import WeChat
            with self.gui_lock:
                wx=WeChat(); moments=wx.Moment
                if moments is None or not wx.SwitchToMoments():
                    raise ValueError('当前微信没有可访问的朋友圈 UIA 树。')
                items=moments.GetMoments(refresh=True)
                return {'items':[{'index':i,'publisher':x.publisher,'text':x.text[:160],
                                  'time':x.timestamp,'image_count':x.image_count,
                                  'fingerprint':self._moment_fingerprint(x)} for i,x in enumerate(items[:50])]}
        finally:
            self._restore_accessibility(accessibility)
            comtypes.CoUninitialize()

    def moment_action_live(self, action, index, fingerprint, content=''):
        if action not in {'like','comment'} or not isinstance(index,int) or not isinstance(fingerprint,str):
            raise ValueError('请选择有效动态。')
        if not self.app.account().get('active'):
            raise ValueError('请先登录当前微信账号。')
        if action=='comment' and (not isinstance(content,str) or not content.strip() or len(content)>300):
            raise ValueError('评论内容应为 1–300 字。')
        import comtypes
        comtypes.CoInitialize()
        accessibility = self._accessibility_snapshot()
        try:
            self._prepare_moment_uia()
            from mywxplus._vendor.wechatauto import WeChat
            with self.gui_lock:
                wx=WeChat(); moments=wx.Moment
                if moments is None or not wx.SwitchToMoments():
                    raise ValueError('当前微信没有可访问的朋友圈 UIA 树。')
                items=moments.GetMoments(refresh=True)
                if not 0<=index<len(items) or self._moment_fingerprint(items[index])!=fingerprint:
                    raise ValueError('朋友圈列表已变化，请刷新后重新选择。')
                if sum(self._moment_fingerprint(x)==fingerprint for x in items)!=1:
                    raise ValueError('当前界面存在重复动态，无法安全定位。')
                result=moments.Like(items[index]) if action=='like' else moments.Comment(items[index],content.strip())
        finally:
            self._restore_accessibility(accessibility)
            comtypes.CoUninitialize()
        message=result.get('message','')
        self._event(('点赞' if action=='like' else '评论')+'：'+message)
        if not result:raise ValueError(message or '微信未确认操作。')
        return {'ok':True,'message':message}

    def moment_action(self, action, moment_id, content='', reply_to='', target_text=''):
        if action not in {'like', 'comment', 'reply'}:
            raise ValueError('不支持的朋友圈操作。')
        account = self.app.account()
        if not account.get('active'):
            raise ValueError('请先登录当前微信账号。')
        if action in {'comment', 'reply'} and (not isinstance(content, str) or not content.strip() or len(content) > 300):
            raise ValueError('评论内容应为 1–300 字。')
        if action == 'reply' and (not isinstance(reply_to, str) or not reply_to.strip()):
            raise ValueError('请选择要回复的评论作者。')
        with self.app.archive().connect() as c:
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT id,nickname,body FROM moments WHERE id=?', (moment_id,)).fetchone()
            if not row:
                raise ValueError('动态不在当前账号归档中。')
            author, body = (row['nickname'] or '').strip(), (row['body'] or '').strip()
            if not author or len(body) < 2:
                raise ValueError('这条动态缺少可定位的作者或文字，请从微信界面选择动态。')
            duplicates = c.execute('SELECT count(*) FROM moments WHERE nickname=? AND body=?', (author, body)).fetchone()[0]
            if duplicates != 1:
                raise ValueError('存在作者与文字相同的动态，无法唯一定位。')
        import comtypes
        comtypes.CoInitialize()
        accessibility = self._accessibility_snapshot()
        try:
            self._prepare_moment_uia()
            from mywxplus._vendor.wechatauto import WeChat
            with self.gui_lock:
                wx = WeChat()
                moments = wx.Moment
                if moments is None or not wx.SwitchToMoments():
                    raise ValueError('微信朋友圈 UIA 树不可用，请打开并解锁微信窗口。')
                keyword = ''.join(body.split())[:20]
                item = moments.find_moment(publisher=author, keyword=keyword, max_screens=30)
                visible = ''.join((item.text or '').split()) if item is not None else ''
                if item is None or item.publisher != author or not visible or (
                        keyword not in visible and visible[:10] not in keyword):
                    raise ValueError('未在微信界面唯一确认这条动态；没有执行操作。')
                if action == 'like':
                    result = moments.Like(item)
                elif action == 'comment':
                    result = moments.Comment(item, content.strip())
                else:
                    result = moments.ReplyComment(item, reply_to.strip(), content.strip(), target_text=target_text)
        finally:
            self._restore_accessibility(accessibility)
            comtypes.CoUninitialize()
        message = result.get('message', '')
        self._event(({'like':'点赞','comment':'评论','reply':'回复评论'}[action]) + '：' + author + ' · ' + message)
        if not result:
            raise ValueError(message or '微信界面未确认操作成功。')
        return {'ok': True, 'message': message}
