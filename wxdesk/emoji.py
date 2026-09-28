"""Explicit, optional download of missing stickers from WeChat CDN addresses."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from Crypto.Cipher import AES
from PIL import Image

from .common import atomic_json, check_cancel, inside, read_json
from .discovery import find_verified
from .media import image_type
from .messages import xml_root


def cdn_url(url):
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
        host = (parsed.hostname or '').lower()
        allowed = host == 'qpic.cn' or host.endswith('.qpic.cn') or host == 'wxapp.tc.qq.com'
        if parsed.scheme not in {'https', 'http'} or not allowed or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return ''
        return urllib.parse.urlunsplit(parsed)
    except ValueError:
        return ''


class CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe = cdn_url(newurl)
        if not safe:
            raise ValueError('表情链接跳转到了非微信表情服务')
        return super().redirect_request(req, fp, code, msg, headers, safe)


def download(url, cancel=None):
    safe = cdn_url(url)
    if not safe:
        raise ValueError('不是支持的微信表情链接')
    opener = urllib.request.build_opener(CDNRedirect())
    request = urllib.request.Request(safe, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*'})
    with opener.open(request, timeout=8) as response:
        chunks, size = [], 0
        deadline = time.monotonic() + 30
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise TimeoutError('表情下载超时')
            block = response.read(65536)
            if not block:
                return b''.join(chunks)
            size += len(block)
            if size > 15 * 1024 * 1024:
                raise ValueError('表情文件超过 15 MB')
            chunks.append(block)


def decode_sticker(raw, key=''):
    candidates = [raw]
    encoded = key.encode() if isinstance(key, str) else bytes(key or b'')
    keys = [encoded]
    try:
        keys.insert(0, bytes.fromhex(encoded.decode()))
    except (ValueError, UnicodeError):
        pass
    if raw and len(raw) % 16 == 0:
        for aes_key in keys:
            if len(aes_key) not in {16, 24, 32}:
                continue
            for mode, iv in [(AES.MODE_ECB, None), (AES.MODE_CBC, aes_key[:16]), (AES.MODE_CBC, bytes(16))]:
                decrypted = AES.new(aes_key, mode, **({'iv': iv} if iv is not None else {})).decrypt(raw)
                pad = decrypted[-1]
                if 1 <= pad <= 16 and decrypted[-pad:] == bytes([pad]) * pad:
                    decrypted = decrypted[:-pad]
                candidates.append(decrypted)
    for data in candidates:
        ext = image_type(data)
        if ext not in {'png', 'jpg', 'gif', 'webp', 'bmp'}:
            continue
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            return data, ext
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
    raise ValueError('表情数据无法解码')


def restore_emojis(archive, account, progress, cancel=None, message_ids=None, retry_failed=False):
    from .importer import encrypted, tables
    with archive.connect() as c:
        query="SELECT id,raw,detail FROM messages WHERE kind='emoji' AND media_path=''"
        if not retry_failed:
            query += " AND media_status NOT IN ('消息中没有可用的表情下载地址','下载地址失效或数据暂时无法解码')"
        args=[]
        if message_ids is not None:
            args=[int(v) for v in message_ids][:100]
            if not args:return {'messages':0,'unavailable':0,'unique':0}
            query+=' AND id IN ('+','.join('?' for _ in args)+')'
        rows=c.execute(query+' ORDER BY ts DESC',args).fetchall()
    if not rows:
        progress('done',100,'没有新增待补全的表情')
        return {'messages':0,'unavailable':0,'unique':0}
    metadata = {}
    verified = find_verified(account['account'], account['dbdir'])
    if verified:
        manifest = read_json(verified)
        for rel, entry in manifest.get('keys', {}).items():
            if Path(rel).name != 'emoticon.db':
                continue
            try:
                with encrypted(inside(Path(account['dbdir']), Path(account['dbdir']) / rel), entry['key']) as source:
                    if 'kNonStoreEmoticonTable' in tables(source):
                        for md5, key, cdn, encrypted_url, thumb in source.execute('SELECT md5,aes_key,cdn_url,encrypt_url,thumb_url FROM kNonStoreEmoticonTable'):
                            metadata[str(md5).lower()] = (key, [cdn, encrypted_url, thumb])
            except Exception:
                # URLs embedded in saved messages remain available without this optional database.
                pass
    assets=archive.directory/'assets';assets.mkdir(exist_ok=True)
    sticker_index=read_json(assets/'emoji_index.json')
    groups={}
    for row in rows:
        check_cancel(cancel)
        detail=json.loads(row['detail']);root=xml_root(row['raw'])
        node=root if root is not None and root.tag=='emoji' else root.find('.//emoji') if root is not None else None
        attrs=node.attrib if node is not None else {}
        md5=(attrs.get('androidmd5') or attrs.get('md5') or detail.get('md5') or '').lower()
        candidates=[]
        key=attrs.get('aeskey','')
        for name in ['cdnurl','encrypturl','externurl','thumburl']:
            if cdn_url(attrs.get(name)):candidates.append((attrs[name],key,'缩略图' if name=='thumburl' else '已恢复'))
        for digest in [md5,attrs.get('md5','').lower()]:
            key,urls=metadata.get(digest,('',[]))
            candidates.extend((u,key,'已恢复') for u in urls if cdn_url(u))
        fingerprint=md5 if re.fullmatch(r'[0-9a-f]{32}',md5) else hashlib.sha256(str(candidates).encode()).hexdigest()
        group=groups.setdefault(fingerprint,{'ids':[],'candidates':[]})
        group['ids'].append(row['id'])
        for candidate in candidates:
            if candidate not in group['candidates']:group['candidates'].append(candidate)
    from concurrent.futures import ThreadPoolExecutor,as_completed
    def fetch(item):
        fingerprint,group=item;check_cancel(cancel)
        saved=sticker_index.get(fingerprint)
        if saved and inside(assets,assets/saved).is_file():return fingerprint,'assets/'+saved,'已恢复',group['ids']
        status='消息中没有可用的表情下载地址'
        for url,key,quality in group['candidates']:
            check_cancel(cancel)
            try:
                raw,ext=decode_sticker(download(url,cancel),key)
                name='emoji_'+hashlib.sha256(raw).hexdigest()[:32]+'.'+ext
                destination=assets/name
                # Identical content can be shared by several MD5 variants.
                if not destination.exists():
                    temporary=assets/(name+'.'+fingerprint[:12]+'.tmp');temporary.write_bytes(raw);temporary.replace(destination)
                return fingerprint,'assets/'+name,quality,group['ids']
            except (OSError,ValueError,TimeoutError,urllib.error.URLError):status='下载地址失效或数据暂时无法解码'
        return fingerprint,'',status,group['ids']
    restored=failed=done=0;items=list(groups.items())
    # Bounded batches save completed work; a cancelled run can resume from disk.
    with ThreadPoolExecutor(max_workers=12,thread_name_prefix='emoji-download') as pool:
        for offset in range(0,len(items),60):
            check_cancel(cancel);updates=[]
            futures=[pool.submit(fetch,item) for item in items[offset:offset+60]]
            try:
                for future in as_completed(futures):
                    fingerprint,path,status,ids=future.result();done+=1
                    if path:
                        sticker_index[fingerprint]=Path(path).name;restored+=len(ids)
                    else:failed+=1
                    updates.extend((path,status,mid) for mid in ids)
                    progress('emoji',int(95*done/max(1,len(items))),f'正在补全表情 · {done:,} / {len(items):,} 个 · 已恢复 {restored:,} 条消息')
            finally:
                if updates:
                    with sqlite3.connect(str(archive.path),timeout=30) as c:
                        c.executemany('UPDATE messages SET media_path=?,media_status=? WHERE id=?',updates)
                    atomic_json(assets/'emoji_index.json',sticker_index)
    progress('done',100,f'已补全 {restored:,} 条表情消息，{failed:,} 个表情暂不可用')
    return {'messages':restored,'unavailable':failed,'unique':len(groups)}
