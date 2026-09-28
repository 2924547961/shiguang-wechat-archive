import hashlib
import io
import json
import sqlite3
import threading

import pytest
from Crypto.Cipher import AES
from PIL import Image

from wxdesk import emoji
from wxdesk.common import Cancelled
from wxdesk.media import MediaResolver
from wxdesk.store import Archive, create_store


def png():
    stream = io.BytesIO()
    Image.new('RGB', (3, 3), '#93a866').save(stream, 'PNG')
    return stream.getvalue()


def test_cdn_boundary_and_encrypted_sticker():
    for url in ['file:///tmp/a', 'http://127.0.0.1/a', 'https://qpic.cn.evil.test/a', 'https://evil.test/?url=qpic.cn', 'https://user@qpic.cn/a', 'https://qpic.cn:8080/a']:
        assert not emoji.cdn_url(url)
    assert emoji.cdn_url('https://wx.qpic.cn/a?x=1&y=2')
    assert emoji.cdn_url('http://wxapp.tc.qq.com/a')
    raw = png(); pad = 16 - len(raw) % 16; key = b'0123456789abcdef'
    encrypted = AES.new(key, AES.MODE_CBC, iv=key).encrypt(raw + bytes([pad]) * pad)
    assert emoji.decode_sticker(encrypted, key.hex()) == (raw, 'png')
    with pytest.raises(ValueError):
        emoji.decode_sticker(b'<html>not an image</html>')


def test_restore_reuses_cache_and_cancellation(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    c = create_store(archive.path)
    md5 = hashlib.md5(png()).hexdigest()
    raw = f'<msg><emoji md5="{md5}" cdnurl="https://wx.qpic.cn/fixture"/></msg>'
    for i in range(2):
        c.execute('INSERT INTO messages(origin,conversation_id,is_self,ts,kind,detail,raw) VALUES(?,?,?,?,?,?,?)', (str(i), 'friend', 0, i, 'emoji', json.dumps({'md5': md5}), raw))
    c.commit(); c.close()
    monkeypatch.setattr(emoji, 'find_verified', lambda *args: None)
    calls = []
    monkeypatch.setattr(emoji, 'download', lambda *args: calls.append(args[0]) or png())
    account = {'account': 'fixture', 'dbdir': str(tmp_path)}
    cancelled = threading.Event(); cancelled.set()
    with pytest.raises(Cancelled):
        emoji.restore_emojis(archive, account, lambda *args: None, cancelled)
    assert not calls
    result = emoji.restore_emojis(archive, account, lambda *args: None)
    assert result['messages'] == 2 and result['unique'] == 1 and len(calls) == 1
    resolver = MediaResolver(tmp_path / 'wechat', tmp_path / 'assets', None)
    path, status = resolver.resolve({'kind': 'emoji', 'detail': {'md5': md5}})
    assert (tmp_path / path).read_bytes() == png() and status == '已恢复'
    with archive.connect() as c:
        assert c.execute("SELECT count(*) FROM messages WHERE media_path<>''").fetchone()[0] == 2


def test_encrypted_url_fallback_and_scope(tmp_path,monkeypatch):
    archive=Archive(tmp_path);c=create_store(archive.path)
    for i in range(2):
        raw=f'<msg><emoji md5="{i:032x}" encrypturl="https://wx.qpic.cn/encrypted" aeskey=""/></msg>'
        c.execute('INSERT INTO messages(origin,conversation_id,is_self,ts,kind,detail,raw) VALUES(?,?,?,?,?,?,?)',(str(i),'friend',0,i,'emoji','{}',raw))
    c.commit();c.close()
    monkeypatch.setattr(emoji,'find_verified',lambda *a:None)
    calls=[];monkeypatch.setattr(emoji,'download',lambda url,*a:calls.append(url) or png())
    result=emoji.restore_emojis(archive,{'account':'fixture','dbdir':str(tmp_path)},lambda *a:None,message_ids=[1])
    assert result['messages']==1 and len(calls)==1
    with archive.connect() as c:
        assert c.execute('SELECT media_path FROM messages WHERE id=1').fetchone()[0]
        assert not c.execute('SELECT media_path FROM messages WHERE id=2').fetchone()[0]


def test_failed_emoji_is_not_downloaded_again_by_incremental_refresh(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    c = create_store(archive.path)
    raw = '<msg><emoji md5="0123456789abcdef0123456789abcdef" cdnurl="https://wx.qpic.cn/expired"/></msg>'
    c.execute('INSERT INTO messages(origin,conversation_id,is_self,ts,kind,detail,raw) VALUES(?,?,?,?,?,?,?)',
              ('one', 'friend', 0, 1, 'emoji', '{}', raw))
    c.commit(); c.close()
    monkeypatch.setattr(emoji, 'find_verified', lambda *args: None)
    calls = []
    def expired(url, *args):
        calls.append(url)
        raise ValueError('expired')
    monkeypatch.setattr(emoji, 'download', expired)
    account = {'account': 'fixture', 'dbdir': str(tmp_path)}
    first = emoji.restore_emojis(archive, account, lambda *args: None)
    second = emoji.restore_emojis(archive, account, lambda *args: None)
    assert first['unavailable'] == 1 and second['unique'] == 0 and len(calls) == 1
    emoji.restore_emojis(archive, account, lambda *args: None, retry_failed=True)
    assert len(calls) == 2


def test_no_new_emoji_returns_before_opening_encrypted_database(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    create_store(archive.path).close()
    monkeypatch.setattr(emoji, 'find_verified', lambda *args: pytest.fail('unnecessary key lookup'))
    updates = []
    result = emoji.restore_emojis(archive, {'account': 'fixture', 'dbdir': str(tmp_path)},
                                  lambda *args: updates.append(args))
    assert result == {'messages': 0, 'unavailable': 0, 'unique': 0}
    assert updates[-1][-1] == '没有新增待补全的表情'
