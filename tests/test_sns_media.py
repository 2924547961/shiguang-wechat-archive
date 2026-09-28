import hashlib
import io
import json

from PIL import Image

from wxdesk import moments
from wxdesk.sns_crypto import keystream, repair_partial_image
from wxdesk.store import Archive, create_store
from wxdesk.messages import parse_message


def test_isaac64_matches_independent_wasm_vector():
    assert keystream(2136343393, 16).hex() == '23766a3699fb876a75d5a232994844ab'


def test_recall_message_keeps_target_reference():
    parsed = parse_message(10000, '<sysmsg type="revokemsg"><revokemsg><newmsgid>123456</newmsgid><content>撤回了一条消息</content></revokemsg></sysmsg>')
    assert parsed['kind'] == 'system'
    assert parsed['detail']['revoke'] is True
    assert parsed['detail']['revoked_server_id'] == '123456'


def test_moments_cdn_image_stays_with_its_own_post(tmp_path, monkeypatch):
    picture = Image.new('RGB', (12, 9), '#557755')
    buffer = io.BytesIO()
    picture.save(buffer, format='JPEG')
    plain = buffer.getvalue()
    seed = '2136343393'
    encrypted = bytes(a ^ b for a, b in zip(plain, keystream(int(seed), len(plain))))
    requested = []

    class Response(io.BytesIO):
        headers = {'Content-Length': str(len(encrypted))}

    class Opener:
        def open(self, request, timeout):
            requested.append(request.full_url)
            return Response(encrypted)

    monkeypatch.setattr(moments, '_cdn_opener', Opener())
    archive = Archive(tmp_path)
    with create_store(archive.path) as c:
        for mid, body in [('one', '第一条的文字'), ('two', '另一条的文字')]:
            raw = f'<SnsDataItem><TimelineObject><id>{mid}</id><username>friend</username><createTime>1750000000</createTime><contentDesc>{body}</contentDesc><ContentObject><mediaList>'
            if mid == 'one':
                raw += f'<media><id>photo</id><type>2</type><url key="{seed}" token="sample-token">http://shmmsns.qpic.cn/mmsns/sample/0</url><size width="12" height="9" totalSize="{len(plain)}"/></media>'
            raw += '</mediaList></ContentObject></TimelineObject></SnsDataItem>'
            parsed = moments.parse_moment(raw)
            c.execute('INSERT INTO moments VALUES(?,?,?,?,?,?,?,?)',
                      (mid, 'friend', '', parsed['ts'], body, json.dumps(parsed['detail']), raw, hashlib.sha256(raw.encode()).hexdigest()))
    result = moments.restore_moments_media(archive, ['one', 'two'])
    assert result['recovered'] == 1
    assert 'token=sample-token&idx=1' in requested[0]
    rows = moments.list_moments(archive)['items']
    linked = next(item for item in rows if item['id'] == 'one')
    assert linked['body'] == '第一条的文字'
    assert linked['detail']['media'][0]['media_path']
    assert (archive.directory / linked['detail']['media'][0]['media_path']).read_bytes() == plain


def test_old_half_image_repairs_locally_and_is_idempotent(tmp_path):
    from random import Random
    seed='2136343393'
    random=Random(4)
    image=Image.frombytes('RGB',(560,560),random.randbytes(560*560*3))
    buffer=io.BytesIO();image.save(buffer,format='JPEG',quality=92)
    plain=buffer.getvalue()
    assert len(plain)>131072
    stream=keystream(int(seed),len(plain))
    partial=plain[:131072]+bytes(a^b for a,b in zip(plain[131072:],stream[131072:]))
    assert repair_partial_image(partial,seed)==plain
    archive=Archive(tmp_path)
    with create_store(archive.path) as db:
        path=tmp_path/'assets'/'moments'/'old.jpg';path.parent.mkdir(parents=True)
        path.write_bytes(partial)
        detail={'media':[{'type':'2','media_path':'assets/moments/old.jpg','url_key':seed}]}
        db.execute('INSERT INTO moments(id,detail) VALUES(?,?)',('post',json.dumps(detail)))
    first=moments.repair_archived_moments(archive)
    assert first['repaired']==1 and first['remaining_broken']==0
    with archive.connect() as db:
        linked=json.loads(db.execute("SELECT detail FROM moments WHERE id='post'").fetchone()[0])['media'][0]['media_path']
    assert (tmp_path/linked).read_bytes()==plain
    assert moments.repair_archived_moments(archive)['repaired']==0
