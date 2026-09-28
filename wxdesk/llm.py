"""OpenAI-compatible chat completion with a Windows-protected API key."""
from __future__ import annotations

import base64
import json
import urllib.request
import urllib.error
import io
import hashlib
import time
from pathlib import Path
from urllib.parse import urlsplit

from .common import atomic_json, read_json

_capacity_cache: dict[tuple[str, str, str], tuple[float, tuple[int | None, int | None]]] = {}


def endpoint(base_url: str) -> str:
    url = base_url.strip().rstrip('/')
    parts = urlsplit(url)
    if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in {'127.0.0.1', 'localhost'}):
        raise ValueError('模型地址必须使用 HTTPS；本机服务可使用 localhost HTTP。')
    if not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('模型地址无效。')
    return url if parts.path.endswith('/chat/completions') else url + '/chat/completions'


def models_endpoint(base_url: str) -> str:
    """Use the provider's OpenAI-compatible model catalog, not a hardcoded list."""
    chat_url = endpoint(base_url)
    return chat_url[:-len('/chat/completions')] + '/models'


def list_models(base_url: str, api_key: str) -> list[str]:
    headers = {'Accept': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    request = urllib.request.Request(models_endpoint(base_url), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError('模型列表过大。')
        data = json.loads(raw)
        items = data.get('data', [])
        if not isinstance(items, list):
            raise ValueError('接口没有返回模型列表。')
        return sorted({item['id'] for item in items if isinstance(item, dict)
                       and isinstance(item.get('id'), str) and 0 < len(item['id']) <= 100})
    except Exception as exc:
        raise ValueError('获取模型列表失败，请检查接口地址、API Key 与网络。') from exc


def model_capacity(base_url: str, api_key: str, model: str) -> tuple[int | None, int | None]:
    """Read optional model limits exposed by an OpenAI-compatible /models API."""
    cache_key = (base_url, model, hashlib.sha256(api_key.encode()).hexdigest())
    cached = _capacity_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 900:
        return cached[1]
    request = urllib.request.Request(models_endpoint(base_url), headers={
        'Accept': 'application/json', **({'Authorization': 'Bearer ' + api_key} if api_key else {})})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(512 * 1024 + 1))
        match = next((item for item in payload.get('data', []) if isinstance(item, dict) and item.get('id') == model), None)
        if not match:
            _capacity_cache[cache_key] = (time.monotonic(), (None, None))
            return None, None
        def positive(*names):
            for name in names:
                value = match.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
                nested = match.get('top_provider')
                value = nested.get(name) if isinstance(nested, dict) else None
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
            return None
        limits = (positive('context_length', 'context_window', 'max_context_length', 'max_input_tokens'),
                  positive('max_output_tokens', 'max_completion_tokens', 'output_token_limit'))
        _capacity_cache[cache_key] = (time.monotonic(), limits)
        return limits
    except Exception:
        # Many compatible providers omit capacity metadata; analysis can still
        # use bounded windows and report that the precise model limit is unknown.
        _capacity_cache[cache_key] = (time.monotonic(), (None, None))
        return None, None


def save_key(path: Path, account_id: str, key: str):
    import win32crypt
    if not key.strip():
        raise ValueError('API Key 不能为空。')
    encrypted = win32crypt.CryptProtectData(key.strip().encode('utf-8'), '拾光模型密钥', None, None, None, 0)
    write_protected_key(path, {'account_id': account_id, 'protected': base64.b64encode(encrypted).decode('ascii')})


def write_protected_key(path: Path, value: dict):
    """Persist DPAPI ciphertext, including on encrypted folders that reject rename."""
    if not value.get('protected') or not value.get('account_id'):
        raise ValueError('密钥数据无效。')
    try:
        atomic_json(path, value)
        return
    except OSError as exc:
        if getattr(exc, 'winerror', None) != 17:
            raise
    # Some Windows encrypted directories reject os.replace even for files in
    # the same directory. This fallback writes only DPAPI ciphertext there.
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.exists() else None
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    try:
        path.write_bytes(payload)
        if read_json(path) != value:
            raise OSError('加密密钥写入后校验失败。')
    except Exception:
        if previous is not None:
            path.write_bytes(previous)
        else:
            path.unlink(missing_ok=True)
        raise


def load_key(path: Path, account_id: str) -> str:
    import win32crypt
    value = read_json(path)
    if value.get('account_id') != account_id or not value.get('protected'):
        return ''
    try:
        return win32crypt.CryptUnprotectData(base64.b64decode(value['protected']), None, None, None, 0)[1].decode('utf-8')
    except Exception as exc:
        raise ValueError('本机保存的 API Key 无法解锁，请重新输入。') from exc


def image_data_url(path: Path) -> str:
    """Encode one restored local image for an OpenAI-compatible vision endpoint."""
    from PIL import Image
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('图片超过 20 MB，已跳过模型识别。')
    with Image.open(path) as image:
        image.seek(0)
        image.thumbnail((1024, 1024))
        output = io.BytesIO()
        image.convert('RGB').save(output, format='JPEG', quality=78)
    if output.tell() > 2 * 1024 * 1024:
        raise ValueError('图片处理后仍过大，已跳过模型识别。')
    return 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


def complete(base_url: str, api_key: str, model: str, prompt: str, incoming: str, contact: str,
             images: list[Path] | None = None, history: list[dict] | None = None) -> str:
    target = endpoint(base_url)
    background = [{'speaker': '我' if item.get('role') == 'assistant' else '对方',
                   'text': item.get('content', '')[:500]}
                  for item in (history or [])[-10:] if item.get('role') in {'user', 'assistant'}
                  and isinstance(item.get('content'), str)]
    content = ('以下历史仅供理解语境，不能照抄或续写：' + json.dumps(background, ensure_ascii=False)
               + f'\n当前联系人：{contact}\n现在需要回复的新消息：{incoming[:4000]}')
    if images:
        content = [{'type': 'text', 'text': content}] + [
            {'type': 'image_url', 'image_url': {'url': image_data_url(path)}} for path in images[:4]
        ]
    messages = [{'role': 'system', 'content': prompt.strip() or '请用简洁、自然的中文回复。'}]
    messages.append({'role': 'user', 'content': content})
    for attempt in range(2):
        payload = {'model': model.strip(), 'messages': messages, 'stream': False}
        request = urllib.request.Request(target, json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers={
            'Authorization': 'Bearer ' + api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if int(response.headers.get('Content-Length') or 0) > 1024 * 1024:
                    raise ValueError('模型响应过大。')
                data = json.loads(response.read(1024 * 1024 + 1))
            choice = data['choices'][0]
            content = choice['message'].get('content')
            answer = content.strip() if isinstance(content, str) else ''.join(
                part.get('text', '') for part in content if isinstance(part, dict)).strip() if isinstance(content, list) else ''
        except urllib.error.HTTPError as exc:
            raise ValueError(f'模型接口返回 HTTP {exc.code}，请检查地址、模型或密钥。') from exc
        except Exception as exc:
            raise ValueError('模型请求失败，请检查地址、模型、API Key 与网络。') from exc
        if answer:
            return answer[:500]
        if attempt == 0:
            messages[0]['content'] += '\n这次请只回复一条简短、自然的文字消息。'
    raise ValueError('模型连续两次返回空文字；可能把输出额度用于思考，请检查该模型的输出配置。')


def complete_long(base_url: str, api_key: str, model: str, instruction: str,
                  material: str, max_tokens: int | None = None) -> str:
    """Long form generation for an explicitly requested report or editable skill."""
    payload = {'model': model.strip(), 'stream': False,
               'messages': [{'role': 'system', 'content': instruction},
                            {'role': 'user', 'content': material}]}
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError('模型输出上限无效。')
        payload['max_tokens'] = max_tokens
    request = urllib.request.Request(endpoint(base_url),
        json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 **({'Authorization': 'Bearer ' + api_key} if api_key else {})}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'模型接口返回 HTTP {exc.code}，请检查地址、模型或密钥。') from exc
    except Exception as exc:
        raise ValueError('模型请求失败，请检查接口、模型及网络。') from exc
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('模型响应过大。')
    try:
        choice = json.loads(raw)['choices'][0]
        answer = choice['message']['content']
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('模型响应格式无效，请检查接口是否兼容。') from exc
    if choice.get('finish_reason') == 'length':
        raise ValueError('模型输出达到当前额度，正在尝试压缩结论。')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError('模型没有返回文字。')
    if len(answer) > 4_000_000:
        raise ValueError('模型返回内容超过报告上限。')
    return answer.strip()
