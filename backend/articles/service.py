"""Application adapter for the built-in public-article downloader.

Session parameters live only in this process. Harness payloads contain an
opaque ticket, so a shareable project or persisted task never contains them.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .cache_keys import find_session_params
from .downloader import (AUTH_KEYS, SUPPORTED_FORMATS, DownloadError, Options,
                         WeChatClient, parse_article_links, parse_input_url, run_download)


MODES = {'single', 'links', 'history', 'album', 'channel'}


def _safe_url(value: str) -> tuple[str, dict[str, str]]:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('公众号链接无效。')
    _, parameters = parse_input_url(value)
    parsed = urlsplit(value)
    safe_query = urlencode([(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                            if key not in AUTH_KEYS])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, '')), {
        key: parameters[key] for key in AUTH_KEYS if parameters.get(key)}


def _safe_channel_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('视频号链接无效。')
    parsed = urlsplit(value.strip())
    if parsed.scheme != 'https' or parsed.hostname not in {'weixin.qq.com', 'channels.weixin.qq.com'}:
        raise ValueError('请输入微信视频号 HTTPS 分享链接。')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


def _redact(value: object, auth: dict[str, str]) -> str:
    text = str(value)
    for secret in auth.values():
        if len(secret) >= 4:
            text = text.replace(secret, '[已隐藏]')
    return re.sub(r'https?://[^\s，：]+', lambda match: re.sub(r'[?#].*', '', match.group()), text)[:250]


class ArticleService:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.download_lock = threading.Lock()
        self.pending: dict[str, tuple[float, str, dict]] = {}
        self.scanned_auth: dict[str, dict[str, str]] = {}
        self.yuanbao_cookie = ''

    def set_yuanbao_cookie(self, value: str) -> None:
        with self.lock:
            self.yuanbao_cookie = value.strip() if isinstance(value, str) else ''

    def status(self, account_id: str) -> dict:
        with self.lock:
            cached = bool(self.scanned_auth.get(account_id))
        return {'built_in': True, 'modes': sorted(MODES), 'formats': list(SUPPORTED_FORMATS),
                'output': str(Path(self.app.settings['output_dir']) / '公众号文章'),
                'session_ready': cached, 'yuanbao_ready': bool(self.yuanbao_cookie)}

    def clear_session(self, account_id: str):
        with self.lock:
            self.scanned_auth.pop(account_id, None)
        return {'session_ready': False}

    def start_scan(self, account_id: str, root: object) -> dict:
        if not isinstance(root, str) or not root.strip() or len(root) > 2000:
            raise ValueError('请选择自己的微信缓存目录。')
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError('缓存目录不存在。')
        return self.app.harness.submit(account_id, '', 'article.scan', {'root': str(path), 'account_id': account_id})

    def scan(self, context, payload):
        context.progress(10, '正在扫描所选缓存目录中的近期文件')
        auth, _ = find_session_params(Path(payload['root']), context.cancel)
        context.check()
        if (self.app.selected or 'local-articles') != payload['account_id']:
            raise ValueError('当前账号已切换，扫描结果未保存。')
        if auth:
            with self.lock:
                self.scanned_auth[payload['account_id']] = auth
        return {'found': bool(auth), 'message': '已在运行内存中取得会话参数。' if auth else '没有找到完整的明文会话参数。'}

    def start(self, account_id: str, data: dict) -> dict:
        mode = data.get('mode', 'single')
        if mode not in MODES | {'verify'}:
            raise ValueError('下载模式无效。')
        source = data.get('source', '')
        if not isinstance(source, str) or not source.strip() or len(source) > 200000:
            raise ValueError('请填写文章来源链接。')
        supplied = data.get('auth') or {}
        if not isinstance(supplied, dict):
            raise ValueError('会话参数格式无效。')
        with self.lock:
            auth = dict(self.scanned_auth.get(account_id, {}))
        for key in AUTH_KEYS:
            value = supplied.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 4096):
                raise ValueError('会话参数无效。')
            if value:
                auth[key] = value.strip()
        with self.lock:
            yuanbao_cookie = self.yuanbao_cookie
        if mode == 'links':
            links = parse_article_links(source)
            safe_links = []
            for link in links:
                safe, found = _safe_url(link)
                auth.update(found)
                safe_links.append(safe)
            source = '\n'.join(dict.fromkeys(safe_links))
        elif mode == 'channel':
            source = _safe_channel_url(source)
        else:
            source, found = _safe_url(source.strip())
            auth.update(found)
        if mode in {'history', 'verify'} and not all(auth.get(k) for k in ('uin', 'key', 'pass_ticket')):
            raise ValueError('公众号历史消息需要当前有效会话的 uin、key、pass_ticket。')
        if mode == 'album' and urlsplit(source).path != '/mp/appmsgalbum':
            raise ValueError('请选择公众号合集链接。')
        if mode == 'single' and not urlsplit(source).path.startswith('/s'):
            raise ValueError('请选择公众号文章链接。')
        output_raw = data.get('output') or str(Path(self.app.settings['output_dir']) / '公众号文章')
        if not isinstance(output_raw, str) or len(output_raw) > 2000:
            raise ValueError('输出目录无效。')
        output = Path(output_raw).expanduser().resolve()
        formats = data.get('formats', ['html'])
        if not isinstance(formats, list) or not formats or len(formats) > len(SUPPORTED_FORMATS) or any(
                fmt not in SUPPORTED_FORMATS for fmt in formats):
            raise ValueError('请选择至少一种有效导出格式。')
        try:
            start = date.fromisoformat(data['start_date']) if data.get('start_date') else None
            end = date.fromisoformat(data['end_date']) if data.get('end_date') else None
            delay = float(data.get('delay', 1.5))
            max_pages = int(data.get('max_pages', 0))
            batch_size = int(data.get('batch_size', 10))
            retries = int(data.get('retries', 1))
        except (TypeError, ValueError) as exc:
            raise ValueError('日期、页数或请求设置无效。') from exc
        if start and end and start > end:
            raise ValueError('开始日期晚于结束日期。')
        if not (0 <= delay <= 60 and 0 <= max_pages <= 10000 and 1 <= batch_size <= 500 and 0 <= retries <= 5):
            raise ValueError('请求间隔、页数、批量大小或重试次数超出允许范围。')
        options = Options(output=output, formats=tuple(dict.fromkeys(formats)), images=bool(data.get('images', True)),
                          keyword=str(data.get('keyword') or '')[:200], start_date=start, end_date=end,
                          delay=delay, max_pages=max_pages, cover=bool(data.get('cover')),
                          audio=bool(data.get('audio')), video=bool(data.get('video')),
                          comments=bool(data.get('comments')), skip_existing=bool(data.get('skip_existing', True)),
                          batch_size=batch_size, retries=retries)
        ticket = secrets.token_urlsafe(24)
        with self.lock:
            self.pending[ticket] = (time.monotonic(), account_id, {'mode': mode, 'source': source,
                                                                    'auth': auth, 'options': options,
                                                                    'yuanbao_cookie': yuanbao_cookie.strip()})
        try:
            return self.app.harness.submit(account_id, '', 'article.download', {'ticket': ticket, 'account_id': account_id})
        except Exception:
            with self.lock:
                self.pending.pop(ticket, None)
            raise

    def run(self, context, payload):
        with self.lock:
            item = self.pending.pop(payload.get('ticket'), None)
        if not item or item[1] != payload['account_id'] or time.monotonic() - item[0] > 600:
            raise ValueError('下载会话已过期，请重新提交。')
        if (self.app.selected or 'local-articles') != payload['account_id']:
            raise ValueError('当前微信账号已切换，任务未开始。')
        data = item[2]
        auth = data['auth']
        client = WeChatClient(auth)
        context.progress(5, '准备公众号请求')
        try:
            if data['mode'] == 'verify':
                return context.external('article.download', {'mode': 'verify'}, lambda: self._verify(client, data))

            saved = [0]
            def progress_line(line):
                safe = _redact(line, auth)
                if line.startswith('已保存：'):
                    saved[0] += 1
                context.progress(min(95, 20 + saved[0] * 3), safe)

            while not self.download_lock.acquire(timeout=.5):
                context.progress(10, '正在等待另一项文章下载完成')
            try:
                context.check()
                if data['mode'] == 'channel':
                    from .channel_video import download_channel_video
                    result = context.external('article.download', {'mode': data['mode']},
                                              lambda: download_channel_video(data['source'], data['options'].output,
                                                                             context.cancel, progress_line,
                                                                             data.get('yuanbao_cookie', '')))
                else:
                    result = context.external('article.download', {'mode': data['mode']},
                                              lambda: run_download(client, data['source'], data['options'],
                                                                   context.cancel, progress_line, data['mode']))
            finally:
                self.download_lock.release()
            context.check()
            return result
        except Exception as exc:
            if isinstance(exc, DownloadError):
                if '腾讯元宝登录态已失效' in str(exc):
                    self.set_yuanbao_cookie('')
                raise ValueError(_redact(exc, auth)) from None
            raise
        finally:
            client.session.close()

    @staticmethod
    def _verify(client: WeChatClient, data: dict) -> dict:
        biz, _ = parse_input_url(data['source'])
        result = client.verify_session(biz or client.resolve_biz(data['source']))
        return {'verified': result, 'message': '会话探测通过，实际分页仍以微信返回为准。' if result else
                '会话探测未通过；参数可能过期，或微信要求在客户端内访问。'}
