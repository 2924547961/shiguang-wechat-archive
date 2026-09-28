import csv
import io
import json
import sqlite3
import struct
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from Crypto.Cipher import AES
from PIL import Image

from wxdesk.common import Cancelled, identity, inside
from wxdesk.demo import make_demo
from wxdesk.discovery import legacy_archives
from wxdesk.exporter import export_data, export_report
from wxdesk.media import V1, V2, decode_dat
from wxdesk.messages import parse_message, xml_root
from wxdesk.server import Application, LocalServer
from wxdesk.store import Archive, import_legacy


@pytest.fixture
def demo(tmp_path):
    state = tmp_path / "state"
    make_demo(state)
    return state, Archive(state / "accounts" / identity("wxid_demo_only_a1b2"))


@pytest.mark.parametrize("type_,xml,kind", [
    (1, '文本 <script>alert(1)</script>', 'text'),
    (3, '<msg><img md5="abc"/></msg>', 'image'),
    (34, '<msg><voicemsg voicelength="3000"/></msg>', 'audio'),
    (43, '<msg><videomsg playlength="5"/></msg>', 'video'),
    (47, '<msg><emoji md5="abc"/></msg>', 'emoji'),
    (42, '<msg username="test" nickname="名片"/>', 'contact'),
    (48, '<msg><location x="31" y="121" label="公园"/></msg>', 'location'),
    (49, '<msg><appmsg><type>6</type><title>文档.pdf</title></appmsg></msg>', 'file'),
    (49, '<msg><appmsg><type>57</type><title>回复</title><refermsg><svrid>123</svrid><content>原文</content></refermsg></appmsg></msg>', 'quote'),
    (49, '<msg><appmsg><type>19</type><title>聊天记录</title></appmsg></msg>', 'forward'),
    (49, '<msg><appmsg><type>2000</type><wcpayinfo><feedesc>￥12.00</feedesc></wcpayinfo></appmsg></msg>', 'transfer'),
    (49, '<msg><appmsg><type>36</type><title>小程序</title><url>https://example.org/</url></appmsg></msg>', 'miniapp'),
    (49, '<msg><appmsg><type>51</type><finderFeed><nickname>测试</nickname><desc>视频号内容</desc></finderFeed></appmsg></msg>', 'channel'),
    (50, '<voipmsg><msg>通话时长 01:20</msg></voipmsg>', 'call'),
    (10000, '你拍了拍对方', 'system'),
])
def test_message_kinds(type_, xml, kind):
    assert parse_message(type_, xml)["kind"] == kind


def test_xml_entities_links_and_reference():
    assert xml_root('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///secret">]><msg>&x;</msg>') is None
    bad = parse_message(49, '<msg><appmsg><type>5</type><title>测试</title><url>javascript:alert(1)</url></appmsg></msg>')
    assert bad['detail']['url'] == ''
    q = parse_message(49, '<msg><appmsg><type>57</type><refermsg><svrid>999999999999999999</svrid><content>被引用</content></refermsg></appmsg></msg>')
    assert q['detail']['quote']['server_id'] == '999999999999999999'


def test_zstd_and_sender():
    import zstandard
    encoded = zstandard.ZstdCompressor().compress('wxid_test:\n正文\n第二行'.encode())
    assert parse_message(1, encoded, 'wxid_test')['body'] == '正文\n第二行'


def test_emoji_identifier_and_bare_url_ampersand():
    xml = '<msg><emoji md5="old" androidmd5="new" cdnurl="https://example.org/a?x=1&y=2"/><caption><![CDATA[A&B &amp;]]></caption></msg>'
    root = xml_root(xml)
    assert root.find('emoji').get('cdnurl').endswith('x=1&y=2')
    assert root.find('caption').text == 'A&B &amp;'
    parsed = parse_message(47, xml)
    assert parsed['detail']['md5'] == 'new'
    assert parsed['detail']['rawmd5'] == 'old'


def test_forward_nested():
    data = '<msg><appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="1"><sourcename>朋友</sourcename><datadesc>你好</datadesc></dataitem><dataitem datatype="2"><datamd5>hash</datamd5></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>'
    result = parse_message(49, data)
    assert [x['kind'] for x in result['detail']['items']] == ['text', 'image']
    assert result['detail']['items'][0]['body'] == '你好'


@pytest.mark.parametrize('version', [V1, V2])
def test_real_dat_layout(version):
    buf = io.BytesIO(); Image.new('RGB', (48, 48), '#4d8256').save(buf, format='PNG')
    raw = buf.getvalue(); key = b'cfcd208495d565ef' if version == V1 else b'demoKey000000001'
    prefix, rest = raw[:48], raw[48:]
    padded = prefix + bytes([16]) * 16
    xor = 91; xor_len = min(30, len(rest))
    encrypted = version + struct.pack('<II', len(prefix), xor_len) + b'\x00' + AES.new(key,AES.MODE_ECB).encrypt(padded) + rest[:-xor_len] + bytes(v ^ xor for v in rest[-xor_len:])
    actual, ext = decode_dat(encrypted, key, xor)
    assert actual == raw and ext == 'png'
    if version == V2:
        with pytest.raises(ValueError): decode_dat(encrypted, b'badKey0000000000', xor)


def test_legacy_xor():
    buf=io.BytesIO();Image.new('RGB',(10,10),'green').save(buf,'JPEG');raw=buf.getvalue()
    assert decode_dat(bytes(b ^ 177 for b in raw))[0] == raw


def test_archive_search_reference_and_year(demo):
    _, archive=demo
    rows=archive.messages('demo_xiaoyu')
    quoted=next(m for m in rows['items'] if m['kind']=='quote')
    sid=quoted['detail']['quote']['server_id']
    target=archive.messages('demo_xiaoyu',server_id=sid)
    assert any(m['server_id']==sid for m in target['items'])
    assert archive.messages('demo_xiaoyu',server_id='not-found')['not_found']
    assert archive.messages('demo_xiaoyu',query='完全不存在的内容')['total']==0
    result=archive.annual(2025)
    assert result['total']==sum(result['months'])==sum(result['hours'])==result['sent']+result['received']
    assert result['total']>0 and result['words']
    pair=archive.annual(2025,'demo_xiaoyu')
    assert 0<pair['total']<result['total']
    with pytest.raises(ValueError): archive.annual(2025,'demo_family@chatroom')


def test_export_all_formats_and_filter(demo,tmp_path):
    _,archive=demo
    result=export_data(archive,{'formats':['html','txt','csv','docx','sqlite','contacts'],'conversations':['demo_xiaoyu'],'include_media':True},tmp_path/'exports',lambda *a:None)
    folder=Path(result['path'])
    assert all(list(folder.glob('*.'+ext)) for ext in ['html','txt','csv','docx','sqlite'])
    assert (folder/'assets'/'demo_landscape.png').is_file()
    page=next(p for p in folder.glob('*.html') if p.name!='打开归档.html').read_text('utf-8')
    assert 'data-ref=' in page and 'archive-data' in page
    with zipfile.ZipFile(next(folder.glob('*.docx'))) as z:
        assert any(n.startswith('word/media/') for n in z.namelist())
    with sqlite3.connect(folder/'聊天记录.sqlite') as c:
        assert c.execute('SELECT count(DISTINCT conversation_id) FROM messages').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM messages').fetchone()[0]==result['messages']
    assert not any(folder.parent.glob('*.partial'))


def test_contacts_only_and_cancel_preserves_exports(demo,tmp_path):
    _,archive=demo
    result=export_data(archive,{'formats':['contacts'],'conversations':[],'contacts':['demo_xiaoyu']},tmp_path/'out',lambda *a:None)
    with (Path(result['path'])/'联系人.csv').open(encoding='utf-8-sig',newline='') as f: rows=list(csv.reader(f))
    assert len(rows)==2
    event=threading.Event();event.set()
    with pytest.raises(Cancelled): export_data(archive,{'formats':['html']},tmp_path/'out',lambda *a:None,event)
    assert (Path(result['path'])/'联系人.csv').exists()
    assert not list((tmp_path/'out').glob('*.partial'))


def test_report_is_standalone(demo,tmp_path):
    _,archive=demo
    result=export_report(archive,2025,None,tmp_path,lambda *a:None)
    content=Path(result['index']).read_text('utf-8')
    assert '{{DATA}}' not in content and '<script src=' not in content
    assert result['report']['year']==2025
    with pytest.raises(ValueError): export_report(archive,2000,None,tmp_path,lambda *a:None)


def test_legacy_account_from_summary_not_directory(tmp_path):
    folder=tmp_path/'export_wrong_account';folder.mkdir()
    (folder/'_summary.txt').write_text('账号: wxid_right\n数据库目录: C:\\test\\wxid_right\\db_storage\n','utf-8')
    (folder/'index_message_0.csv').write_text('idx\ttitle\tusername\trows\tkind\tfile\n1\t朋友\twxid_friend\t1\t单聊\tchat.txt\n','utf-8')
    (folder/'chat.txt').write_text('[2025-01-01 08:00:00] 我[文本] 第一行\n第二行\n','utf-8')
    assert legacy_archives(tmp_path)[0]['account']=='wxid_right'
    with pytest.raises(ValueError): import_legacy(folder,tmp_path/'bad.sqlite','wxid_wrong',lambda *a:None)
    target=tmp_path/'import';target.mkdir();import_legacy(folder,target/'archive.sqlite','wxid_right',lambda *a:None)
    assert Archive(target).messages('wxid_friend')['items'][0]['body'].strip()=='第一行\n第二行'
    with pytest.raises(ValueError): inside(folder,folder/'..'/'secret')


def test_http_auth_and_media_range(demo):
    state,_=demo;application=Application(state=state,demo=True);server=LocalServer(application)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err: urllib.request.urlopen(server.origin+'/api/state')
        assert err.value.code==401
        headers={'Authorization':'Bearer '+server.token}
        request=urllib.request.Request(server.origin+'/api/state',headers=headers)
        data=json.load(urllib.request.urlopen(request));assert data['demo'] and data['selected']
        media=server.origin+'/media/'+data['selected']+'/assets/demo_landscape.png'
        with urllib.request.urlopen(urllib.request.Request(media,headers={**headers,'Range':'bytes=0-7'})) as response:
            assert response.status==206 and response.read()==b'\x89PNG\r\n\x1a\n'
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(server.origin+'/api/settings',data=b'{}',headers={**headers,'Content-Type':'application/json','Origin':'https://evil.example'}))
    finally:
        application.close();server.shutdown();server.server_close()
