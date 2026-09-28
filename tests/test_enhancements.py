import json
import sqlite3
import subprocess
import threading
from pathlib import Path
import pytest
from PIL import Image
from wxdesk.common import Cancelled
from wxdesk.moments import parse_moment, list_moments, export_moments, media_candidates, bind_media


def test_new_moment_images_restore_in_background_without_blocking_sync(monkeypatch):
    import threading
    from wxdesk.server import Application
    done = threading.Event()
    calls = []
    def restore(archive, ids, max_images):
        calls.append((archive, ids, max_images))
        done.set()
        return {'recovered': 1}
    monkeypatch.setattr('wxdesk.server.restore_moments_media', restore)
    app = Application.__new__(Application)
    app._moments_media_lock = threading.Lock()
    app._moments_media_pending = []
    app._moments_media_thread = None
    app.live_status = {'moments_revision': 0, 'error': ''}
    app.archive = lambda account: account
    app._queue_moments_media('account-a', ['post-1', 'post-2'])
    assert done.wait(2)
    app._moments_media_thread.join(2)
    assert calls == [('account-a', ['post-1', 'post-2'], 30)]
    assert app.live_status['moments_revision'] == 1
from wxdesk.store import create_store, Archive
from wxdesk.poster import annual_poster


def test_moments_parse_and_portable_export(tmp_path):
    raw='''<SnsDataItem><TimelineObject><id>18446744073709551615</id><username>friend</username><createTime>1740000000</createTime><contentDesc>&lt;script&gt;测试&lt;/script&gt;</contentDesc><ContentObject><mediaList><media><id>media1</id><type>2</type><url md5="abcd">https://example.com/photo</url></media></mediaList></ContentObject></TimelineObject><LocalExtraInfo><nickname>朋友</nickname><like_user_list><user_comment><username>a</username><nickname>好友</nickname></user_comment></like_user_list><comment_user_list><user_comment><nickname>小雨</nickname><content>真好看</content></user_comment></comment_user_list></LocalExtraInfo></SnsDataItem>'''
    m=parse_moment(raw)
    assert m['detail']['likes'][0]['nickname']=='好友'
    assert m['detail']['comments'][0]['content']=='真好看'
    assert m['detail']['media'][0]['md5']=='abcd'
    with pytest.raises(ValueError):parse_moment('<!DOCTYPE x>'+raw)
    root=tmp_path/'archive';root.mkdir();c=create_store(root/'archive.sqlite')
    c.execute('INSERT INTO moments VALUES(?,?,?,?,?,?,?,?)',(m['id'],m['username'],m['nickname'],m['ts'],m['body'],json.dumps(m['detail']),raw,'digest'));c.commit();c.close()
    archive=Archive(root);assert list_moments(archive,'测试')['total']==1
    picture=root/'assets'/'moments'/'fixture.png';picture.parent.mkdir(parents=True)
    Image.new('RGB',(4,5),'green').save(picture)
    with sqlite3.connect(archive.path) as c:
        c.execute('CREATE TABLE sns_media_catalog(asset TEXT PRIMARY KEY,width INTEGER,height INTEGER,bytes INTEGER,mtime INTEGER)')
        c.execute('INSERT INTO sns_media_catalog VALUES(?,?,?,?,?)',('assets/moments/fixture.png',4,5,picture.stat().st_size,0))
    assert media_candidates(archive,m['id'],0)['total']==1
    with pytest.raises(ValueError):bind_media(archive,m['id'],0,'../outside.png')
    bind_media(archive,m['id'],0,'assets/moments/fixture.png')
    assert list_moments(archive)['items'][0]['detail']['media'][0]['manual']
    result=export_moments(archive,{},tmp_path/'exports',lambda *x:None)
    assert (Path(result['path'])/'assets/moments/fixture.png').read_bytes()==picture.read_bytes()
    page=Path(result['index']).read_text('utf-8')
    assert '\\u003cscript>' in page and '<script>测试' not in page
    assert (Path(result['path'])/'朋友圈.json').exists()
    event=threading.Event();event.set()
    with pytest.raises(Cancelled):export_moments(archive,{},tmp_path/'exports',lambda *x:None,event)
    assert len(list((tmp_path/'exports').iterdir()))==1


def test_annual_image_characters(tmp_path):
    c=create_store(tmp_path/'archive.sqlite')
    c.execute("INSERT INTO conversations(id,title,kind) VALUES('friend','朋友','direct')")
    import datetime as dt
    ts=int(dt.datetime(2025,1,1,12).timestamp())
    for i,text in enumerate(['你 好！','第二天']):
        c.execute("INSERT INTO messages(origin,conversation_id,sender,sender_name,is_self,ts,local_type,kind,body,raw,source_db,source_table,local_id,server_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(i),'friend','me','我',1,ts+i*86400,1,'text',text,text,'fixture','table',i,str(i)))
    c.commit();c.close();data=Archive(tmp_path).annual(2025)
    assert data['characters']==6 and data['sent_characters']==6 and data['longest_streak']==2
    annual_poster(data,tmp_path/'report.png')
    with Image.open(tmp_path/'report.png') as image:assert image.size==(1200,1960)


def test_video_export_playback(tmp_path):
    import imageio_ffmpeg
    from wxdesk.playback import compatible_video
    from wxdesk.exporter import copy_media
    source=tmp_path/'archive'/'assets'/'sample.mkv';source.parent.mkdir(parents=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-v','error','-f','lavfi','-i','color=c=green:s=64x64:d=0.2','-c:v','mpeg4',str(source)],check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    output=tmp_path/'export';output.mkdir()
    item={'kind':'video','media_path':'assets/sample.mkv','detail':{}}
    copy_media(item,source.parent.parent,output)
    video=output/item['media_path'];assert video.suffix=='.mp4' and video.stat().st_size>0
    assert compatible_video(source).name==video.name
