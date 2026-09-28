import hashlib
import json
import threading
from pathlib import Path

import pytest
import sqlcipher3

from wxdesk import importer
from wxdesk.common import Cancelled
from wxdesk.store import Archive


def fixture_database(path, statements):
    path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlcipher3.connect(str(path));c.execute('PRAGMA key="x\'%s\'"'%('01'*32))
    for sql,args in statements:c.execute(sql,args)
    c.commit();c.close()


def test_encrypted_import_account_and_cancel(tmp_path,monkeypatch):
    account='wxid_fixture_abcd';dbdir=tmp_path/'xwechat_files'/account/'db_storage'
    me='wxid_fixture';friend='wxid_friend';table='Msg_'+hashlib.md5(friend.encode()).hexdigest()
    fixture_database(dbdir/'contact/contact.db',[
        ('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,alias TEXT,verify_flag INTEGER)',()),
        ('INSERT INTO contact VALUES(?,?,?,?,?)',(me,'自己','','fixture',0)),
        ('INSERT INTO contact VALUES(?,?,?,?,?)',(friend,'朋友','','friend',0)),
    ])
    fixture_database(dbdir/'message/message_0.db',[
        ('CREATE TABLE Name2Id(user_name TEXT)',()),
        ('INSERT INTO Name2Id VALUES(?)',(me,)),('INSERT INTO Name2Id VALUES(?)',(friend,)),
        (f'CREATE TABLE "{table}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content TEXT,packed_info_data BLOB)',()),
        (f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',(1,123,1,1740000000,1,'我发送的消息',b'')),
        (f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',(2,124,1,1740000010,2,'朋友回复',b'')),
    ])
    verified=tmp_path/'fixture.verified.json'
    verified.write_text(json.dumps({'account':account,'dbdir':str(dbdir),'keys':{p.relative_to(dbdir).as_posix():{'key':'01'*32} for p in dbdir.rglob('*.db')}}),'utf-8')
    monkeypatch.setattr(importer,'find_verified',lambda *args:str(verified))
    source_hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in dbdir.rglob('*.db')}
    output=tmp_path/'archive';a={'account':account,'dbdir':str(dbdir),'root':str(dbdir.parent.parent),'active':False,'username':me}
    importer.sync_account(a,output,lambda *args:None,include_media=False)
    archive=Archive(output);rows=archive.messages(friend)['items']
    assert len(rows)==2 and rows[0]['is_self']==1 and rows[1]['sender_name']=='朋友'
    assert archive.stats()['messages']==2
    assert source_hashes=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source_hashes}
    first=importer.sync_account(a,output,lambda *args:None,include_media=False)
    assert first['incremental'] and first['new_messages']==0
    unchanged=importer.sync_account(a,output,lambda *args:None,include_media=False)
    assert unchanged['checked']==0
    fixture_database(dbdir/'message/message_0.db',[
        (f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',(3,0,1,1740000020,1,'等待发送',b'')),
    ])
    result=importer.sync_account(a,output,lambda *args:None,include_media=False)
    assert result['new_messages']==1 and archive.stats()['messages']==3
    fixture_database(dbdir/'message/message_0.db',[
        (f'UPDATE "{table}" SET server_id=125,message_content=? WHERE local_id=3',('发送成功',)),
    ])
    result=importer.sync_account(a,output,lambda *args:None,include_media=False)
    assert result['updated_messages']==1 and archive.stats()['messages']==3
    # A rebuilt source may reuse local IDs. Preserve older messages with different server IDs.
    fixture_database(dbdir/'message/message_0.db',[
        (f'DELETE FROM "{table}"',()),
        (f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',(1,126,1,1740000030,2,'数据库重建后的消息',b'')),
    ])
    result=importer.sync_account(a,output,lambda *args:None,include_media=False)
    assert result['new_messages']==1 and archive.stats()['messages']==4
    event=threading.Event();event.set()
    with pytest.raises(Cancelled):importer.sync_account(a,output,lambda *args:None,event,include_media=False)
    assert archive.stats()['messages']==4
    assert not list(output.glob('.build-*'))
    wrong=dict(a,account='wxid_different_abcd')
    with pytest.raises(ValueError):importer.sync_account(wrong,output,lambda *args:None,include_media=False)
