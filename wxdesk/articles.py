"""Local WeChat article downloader MCP bridge.

Only the documented localhost service is used. No article URL or account
credential is silently sent to a remote fallback.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ENDPOINT = 'http://127.0.0.1:4545/mcp'
TOOLS = {'single_article_download', 'get_public_account_id',
         'batch_download_articles', 'export_article_data'}


def _rpc(method, params=None, timeout=5):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method,
                       **({'params': params} if params is not None else {})}).encode()
    request = Request(ENDPOINT, body, {'Content-Type': 'application/json',
                                       'Accept': 'application/json, text/event-stream'})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024).decode('utf-8-sig')
    if raw.startswith('data:') or '\ndata:' in raw:
        data = [line[5:].strip() for line in raw.splitlines() if line.startswith('data:')]
        raw = next((line for line in reversed(data) if line.startswith('{')), '')
    value = json.loads(raw)
    if value.get('error'): raise ValueError('公众号工具返回错误：' + str(value['error'])[:200])
    return value.get('result', {})


def status():
    try:
        found = _rpc('tools/list')
        names = [t.get('name') for t in found.get('tools', [])]
        return {'connected': True, 'tools': [name for name in names if name in TOOLS]}
    except Exception as exc:
        return {'connected': False, 'tools': [],
                'message': '请打开公众号文章下载工具并启动本地 MCP 服务（127.0.0.1:4545）。',
                'detail': str(exc)[:120]}


def _article_url(value):
    if not isinstance(value, str) or len(value) > 3000:
        raise ValueError('文章链接无效。')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname != 'mp.weixin.qq.com' or not parsed.path:
        raise ValueError('请填写 mp.weixin.qq.com 的 HTTPS 文章链接。')
    return value


def run(context, payload):
    name = payload.get('name')
    if name not in TOOLS: raise ValueError('不支持此公众号工具。')
    args = {'url': _article_url(payload.get('url'))} if name == 'single_article_download' else {}
    if name == 'get_public_account_id' and payload.get('url'):
        args = {'url': _article_url(payload['url'])}
    if not status()['connected']:
        raise ValueError('公众号工具本地 MCP 未运行；请先在该工具中启动 MCP 服务。')
    context.progress(20, '已连接公众号工具，正在执行 ' + name)
    value = context.external('article.mcp', {'name': name, 'arguments': args},
                             lambda: _rpc('tools/call', {'name': name, 'arguments': args}, timeout=180))
    context.check()
    context.progress(90, '工具已返回，正在保存任务结果')
    # The credential step may return secrets. Do not persist or display them.
    if name == 'get_public_account_id':
        return {'message': '已调用公众号 ID 获取。请到公众号工具窗口查看生成的链接和密钥状态。'}
    content = value.get('content', []) if isinstance(value, dict) else []
    text = '\n'.join(str(item.get('text', '')) for item in content if isinstance(item, dict))[:10000]
    return {'message': text or '工具已接受任务；文件保存位置以公众号工具中的设置为准。',
            'is_error': bool(value.get('isError')) if isinstance(value, dict) else False}
