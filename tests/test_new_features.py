import contextlib
import json
import sqlite3
import time

from wxdesk.automation import Automation
from wxdesk.exporter import export_data
from wxdesk.insights import InsightService, _payload, _sample
from wxdesk.store import Archive, create_store


def _archive(tmp_path):
    root=tmp_path/'archive';root.mkdir()
    with create_store(root/'archive.sqlite') as db:
        db.execute("INSERT INTO conversations(id,title,kind) VALUES('alice','Alice','direct')")
    return Archive(root)


def _message(db, index, self_sent=False, kind='text', media=''):
    db.execute('INSERT INTO messages(origin,conversation_id,is_self,ts,kind,body,media_path) VALUES(?,?,?,?,?,?,?)',
               (str(index),'alice',int(self_sent),1700000000+index,kind,'样本文字'+str(index),media))


def test_insight_sampling_spans_time_and_own_style_uses_only_self(tmp_path):
    archive=_archive(tmp_path)
    with sqlite3.connect(archive.path) as db:
        for i in range(600):_message(db,i,i%2==0)
    data=_sample(archive,'alice')
    assert data['total']==600 and data['sampled']<=180
    assert int(data['sample'][0]['id'])<int(data['sample'][-1]['id'])
    assert int(data['sample'][-1]['id'])>450
    assert len(_payload(data))<60000
    own=_sample(archive,'alice',self_only=True)
    assert own['total']==300 and all(x['side']=='我' for x in own['sample'])


def test_skill_apply_preserves_enabled_mode_and_contacts(tmp_path):
    archive=_archive(tmp_path)
    class Rules:
        def status(self):return {'contacts':['alice'],'contact_prompts':{},'enabled':False,'mode':'fixed'}
        def update(self,data):return data
    class App:
        state=tmp_path
        settings={'output_dir':str(tmp_path)}
        automation=Rules()
        def account(self):return {'id':'account-1'}
        def archive(self):return archive
    service=InsightService(App())
    service.save_skill('my_style','alice','Reply with short, clear, considerate sentences.')
    result=service.apply_skill('alice','my_style')
    assert result['enabled'] is False and result['mode']=='fixed' and result['contacts']==['alice']


def test_multi_send_reports_each_rejection_and_keeps_account_binding(tmp_path,monkeypatch):
    class App:
        state=tmp_path
        def account(self):return {'id':'account-1'}
    automation=Automation(App())
    monkeypatch.setattr(automation,'_direct_target',lambda cid,message:cid)
    seen=[]
    def fake_queue(cid,message,expected_account=None):
        seen.append(expected_account)
        if cid=='second':raise ValueError('该好友正在发送')
        return {'id':'job-1','status':'running'}
    monkeypatch.setattr(automation,'queue_direct',fake_queue)
    result=automation.queue_multi([{'cid':'first','message':'hello','account_id':'account-1'},
                                   {'cid':'second','message':'hello','account_id':'account-1'}])
    assert [x['status'] for x in result['jobs']]==['running','error']
    assert seen==['account-1','account-1']
    try:automation.queue_multi([{'cid':'first','message':'hello','account_id':'other'}])
    except ValueError:pass
    else:assert False,'other account draft was accepted'


def test_consecutive_batches_are_not_discarded(tmp_path,monkeypatch):
    archive=_archive(tmp_path)
    class App:
        state=tmp_path
        def account(self):return {'id':'account-1','active':True,'has_archive':True}
        def archive(self):return archive
    automation=Automation(App())
    automation.config.update(enabled=True,account_id='account-1',contacts=['alice'],reply='收到',
                             batch_seconds=0,mode='fixed')
    automation.config['watermarks']['account-1']=0
    sent=[]
    monkeypatch.setattr(automation,'_send_once',lambda name,message:sent.append((name,message)) or '已发送')
    with sqlite3.connect(archive.path) as db:_message(db,1)
    automation.poll_once()
    with sqlite3.connect(archive.path) as db:_message(db,2)
    automation.poll_once()
    assert len(sent)==2


def test_emoji_only_export_copies_gif_even_when_media_toggle_off(tmp_path):
    archive=_archive(tmp_path)
    source=archive.directory/'assets'/'emoji.gif';source.parent.mkdir()
    source.write_bytes(b'GIF89a'+b'\x00'*20)
    with sqlite3.connect(archive.path) as db:_message(db,1,kind='emoji',media='assets/emoji.gif')
    result=export_data(archive,{'formats':['emoji'],'conversations':['alice'],'include_media':False},
                       tmp_path/'out',lambda *args:None)
    from pathlib import Path
    folder=Path(result['path'])
    assert list(folder.rglob('*.gif'))
    assert '表情包' in (folder/'打开归档.html').read_text('utf-8')
