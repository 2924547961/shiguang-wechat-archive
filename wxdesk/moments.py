from __future__ import annotations
import csv
import hashlib
import html
import json
import shutil
import sqlite3
import uuid
import io
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .common import check_cancel, read_json, inside
from .discovery import find_verified
from .importer import encrypted
from .store import ensure_schema


def _image_complete(data):
    """A valid header can still contain a truncated lower half."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as picture:
        picture.load()
        return picture.size


def _linked_image_ok(archive, relative):
    try:
        path = inside(archive.directory / 'assets', archive.directory / relative)
        if not path.is_file():
            return False
        _image_complete(path.read_bytes())
        return True
    except (OSError, ValueError):
        return False


def _linked_media_ok(archive, relative):
    if Path(relative).suffix.lower()=='.mp4':
        try:
            path=inside(archive.directory/'assets',archive.directory/relative)
            with path.open('rb') as source:return source.read(8)[4:8]==b'ftyp'
        except (OSError,ValueError):return False
    return _linked_image_ok(archive,relative)


def _write_asset(destination, payload):
    if destination.exists() and destination.read_bytes() == payload:
        return
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def parse_moment(raw):
    if not raw or '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise ValueError('朋友圈 XML 无效')
    root = ET.fromstring(raw)
    timeline = root if root.tag == 'TimelineObject' else root.find('TimelineObject')
    if timeline is None: raise ValueError('不是朋友圈动态')
    extra = root.find('LocalExtraInfo')
    def value(node, key): return node.findtext(key, '') if node is not None else ''
    media=[]
    for item in timeline.findall('ContentObject/mediaList/media'):
        url=item.find('url'); thumb=item.find('thumb')
        media.append({'id':value(item,'id'),'type':value(item,'type'),'url':value(item,'url'),
                      'md5':url.get('md5','').lower() if url is not None else '',
                      'url_key':url.get('key','') if url is not None else '',
                      'url_token':url.get('token','') if url is not None else '',
                      'size':dict(item.find('size').attrib) if item.find('size') is not None else {},
                      'thumb':value(item,'thumb'),'thumb_md5':thumb.get('md5','').lower() if thumb is not None else '',
                      'thumb_key':thumb.get('key','') if thumb is not None else '',
                      'thumb_token':thumb.get('token','') if thumb is not None else '',
                      'video_key':item.find('enc').get('key','') if item.find('enc') is not None else '',
                      'media_path':'','media_status':'缓存尚未关联到这条动态'})
    def people(tag):
        return [{k:value(item,k) for k in ('username','nickname','content','create_time','ref_nickname')}
                for item in (extra.findall(tag+'/user_comment') if extra is not None else []) if value(item,'b_deleted')!='1']
    location=timeline.find('location')
    return {'id':value(timeline,'id'),'username':value(timeline,'username'),
            'nickname':value(extra,'nickname'),'ts':int(value(timeline,'createTime') or 0),
            'body':value(timeline,'contentDesc'), 'detail':{'media':media,'likes':people('like_user_list'),
            'comments':people('comment_user_list'),'location':dict(location.attrib) if location is not None else {},
            'title':value(timeline,'ContentObject/title'),'url':value(timeline,'ContentObject/contentUrl')}}


def sync_moments(archive, account, progress, cancel=None, include_media=True):
    verified=find_verified(account['account'],account['dbdir'])
    if not verified:raise ValueError('请先同步当前账号以验证数据库。')
    manifest=read_json(verified)
    rel=next((r for r in manifest.get('keys',{}) if Path(r).name=='sns.db'),None)
    if not rel:raise ValueError('未找到经过验证的朋友圈数据库；请先同步当前账号。')
    entry=manifest['keys'][rel];key=entry if isinstance(entry,str) else entry.get('key')
    with encrypted(inside(Path(account['dbdir']),Path(account['dbdir'])/rel),key) as src:
        rows=src.execute('SELECT tid,content FROM SnsTimeLine').fetchall()
    c=sqlite3.connect(archive.path,timeout=30);ensure_schema(c)
    c.execute('CREATE TABLE IF NOT EXISTS sns_source_digest(tid INTEGER PRIMARY KEY,digest TEXT,moment_id TEXT)')
    changed=skipped=0; changed_ids=[]
    try:
        existing=dict(c.execute('SELECT id,digest FROM moments'))
        cached={tid:(digest,mid) for tid,digest,mid in c.execute('SELECT tid,digest,moment_id FROM sns_source_digest')}
        for i,(tid,raw) in enumerate(rows):
            check_cancel(cancel)
            if not isinstance(raw,str): skipped+=1;continue
            digest=hashlib.sha256(raw.encode()).hexdigest()
            old=cached.get(tid)
            if old and old[0]==digest and existing.get(old[1])==digest:
                continue
            try:item=parse_moment(raw)
            except (ValueError,ET.ParseError,TypeError):skipped+=1;continue
            if not item['id']:skipped+=1;continue
            prior=c.execute('SELECT detail FROM moments WHERE id=?',(item['id'],)).fetchone()
            if existing.get(item['id'])==digest and prior and json.loads(prior[0]).get('_schema')==3:
                c.execute('INSERT OR REPLACE INTO sns_source_digest VALUES(?,?,?)',(tid,digest,item['id']))
                continue
            if prior:
                old={(m.get('id'),m.get('url')):m for m in json.loads(prior[0]).get('media',[])}
                for media in item['detail']['media']:
                    previous=old.get((media.get('id'),media.get('url')), {})
                    for field in ['media_path','media_status','manual']:
                        if previous.get(field):media[field]=previous[field]
            item['detail']['_schema']=3
            c.execute('INSERT OR REPLACE INTO moments VALUES(?,?,?,?,?,?,?,?)',
                      (item['id'],item['username'],item['nickname'],item['ts'],item['body'],json.dumps(item['detail'],ensure_ascii=False),raw,digest))
            c.execute('INSERT OR REPLACE INTO sns_source_digest VALUES(?,?,?)',(tid,digest,item['id']))
            changed+=1
            changed_ids.append((item['ts'],item['id']))
            if i%200==0:progress('moments',10+int(65*i/max(1,len(rows))),f'正在读取朋友圈 · {i+1:,} / {len(rows):,}')
        check_cancel(cancel);c.commit()
        recovered = recover_media(c, archive, account, progress, cancel) if include_media else 0
        count=c.execute('SELECT count(*) FROM moments').fetchone()[0]
    except BaseException:c.rollback();raise
    finally:c.close()
    progress('done',100,f'朋友圈已同步：{count:,} 条，更新 {changed:,} 条，跳过 {skipped} 条无法识别的数据')
    return {'moments':count,'updated':changed,'updated_ids':[mid for _,mid in sorted(changed_ids,reverse=True)],
            'skipped':skipped,'media_recovered':recovered}


def recover_media(c, archive, account, progress, cancel):
    """Link cached media only with exact content or unique matching metadata."""
    from .media import decode_dat, image_parameters, V2
    from .incremental import _image_keys
    root=Path(account['dbdir']).parent
    c.execute('CREATE TABLE IF NOT EXISTS sns_media_cache(path TEXT PRIMARY KEY,signature TEXT,md5 TEXT,asset TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS sns_media_catalog(asset TEXT PRIMARY KEY,width INTEGER,height INTEGER,bytes INTEGER,mtime INTEGER)')
    cached={r[0]:r[1:] for r in c.execute('SELECT * FROM sns_media_cache')}
    needed=set()
    for (detail,) in c.execute('SELECT detail FROM moments'):
        for item in json.loads(detail)['media']:
            needed.update(v for v in [item.get('md5'),item.get('thumb_md5')] if v)
    cache_id=(account['account'],tuple(account.get('pids',[])))
    aes,xor=_image_keys.get(cache_id,(None,None));attempted=bool(aes)
    files=[p for folder in (root/'cache').glob('*/Sns') for p in folder.rglob('*') if p.is_file()]
    assets=archive.directory/'assets'/'moments';assets.mkdir(parents=True,exist_ok=True)
    for i,path in enumerate(files):
        check_cancel(cancel)
        stat=path.stat();signature=f'{stat.st_size}:{stat.st_mtime_ns}';old=cached.get(str(path))
        if old and old[0]==signature and old[2] and c.execute('SELECT 1 FROM sns_media_catalog WHERE asset=?',(old[2],)).fetchone() and _linked_media_ok(archive,old[2]):continue
        if stat.st_size>300*1024*1024:continue
        try:
            raw=path.read_bytes();ext=path.suffix.lstrip('.').lower()
            if raw[:6]==V2 and not attempted:
                attempted=True;aes,xor=image_parameters(root,account.get('pids',[]),cancel,8)
                if aes:_image_keys[cache_id]=(aes,xor)
            if raw[4:8]!=b'ftyp':
                try:raw,ext=decode_dat(raw,aes,xor)
                except ValueError:
                    raw,ext=decode_dat(raw[:-24],aes,xor)
            else:ext='mp4'
            variants=[raw]
            if ext=='jpg' and b'\xff\xd9' in raw:variants.append(raw[:raw.rfind(b'\xff\xd9')+2])
            if ext=='png' and b'IEND' in raw:variants.append(raw[:raw.rfind(b'IEND')+8])
            match=next(((hashlib.md5(v).hexdigest(),v) for v in variants if hashlib.md5(v).hexdigest() in needed),None)
            payload=match[1] if match else variants[-1]
            digest=match[0] if match else hashlib.md5(payload).hexdigest();relative=''
            if ext in {'jpg','png','gif','webp','bmp','mp4'}:
                width=height=0
                if ext!='mp4':
                    width,height=_image_complete(payload)
                destination=assets/(digest+'.'+ext)
                _write_asset(destination,payload)
                relative=destination.relative_to(archive.directory).as_posix()
                c.execute('INSERT OR REPLACE INTO sns_media_catalog VALUES(?,?,?,?,?)',(relative,width,height,len(payload),int(stat.st_mtime)))
            # Keep unmatched files eligible for a later newly cached timeline item.
            c.execute('INSERT OR REPLACE INTO sns_media_cache VALUES(?,?,?,?)',(str(path),signature,digest,relative))
        except (ValueError,OSError):pass
        if i%100==0:progress('moments_media',75+int(23*i/max(1,len(files))),f'精确匹配朋友圈媒体 · {i+1:,} / {len(files):,}')
    valid_assets={asset for (asset,) in c.execute("SELECT asset FROM sns_media_catalog WHERE asset!=''")
                  if _linked_media_ok(archive,asset)}
    mapping={md5:asset for md5,asset in c.execute("SELECT md5,asset FROM sns_media_cache WHERE asset!=''")
             if asset in valid_assets};recovered=0
    dimensions={}
    for asset,width,height,size,_ in c.execute('SELECT * FROM sns_media_catalog WHERE width>0'):
        if asset not in valid_assets:continue
        dimensions.setdefault((width,height,size),set()).add(asset)
    rows=c.execute('SELECT id,detail FROM moments').fetchall()
    owners={}
    for _,detail in rows:
        for item in json.loads(detail)['media']:
            size=item.get('size') or {}
            try:key=(int(float(size.get('width',0))),int(float(size.get('height',0))),int(float(size.get('totalSize',0))))
            except (TypeError,ValueError):continue
            if all(key):owners.setdefault(key,set()).add(item.get('md5') or item.get('id'))
    for mid,detail in rows:
        check_cancel(cancel);data=json.loads(detail);changed=False
        for item in data['media']:
            if item.get('manual'):continue
            rel=mapping.get(item.get('md5')) or mapping.get(item.get('thumb_md5'))
            method='内容校验一致'
            if not rel and item.get('type')=='2':
                size=item.get('size') or {}
                try:key=(int(float(size.get('width',0))),int(float(size.get('height',0))),int(float(size.get('totalSize',0))))
                except (TypeError,ValueError):key=(0,0,0)
                candidates=dimensions.get(key,set()) if all(key) else set()
                if len(candidates)==1 and len(owners.get(key,set()))==1:
                    rel=next(iter(candidates));method='尺寸与文件大小一致'
            if rel:
                if item.get('media_path')!=rel:
                    item['media_path']=rel;item['media_status']='已恢复（'+method+'）';changed=True;recovered+=1
        if changed:c.execute('UPDATE moments SET detail=? WHERE id=?',(json.dumps(data,ensure_ascii=False),mid))
    c.commit();return recovered


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_cdn_opener = build_opener(_NoRedirect)


def _cdn_url(media, field):
    """Use only the media URL and token stored on the same TimelineObject."""
    raw = media.get(field) or ''
    try:parts = urlsplit(raw)
    except ValueError:return ''
    host = (parts.hostname or '').lower()
    if parts.scheme not in {'http','https'} or parts.username or parts.password or not (
        host.endswith('.qpic.cn') or host.endswith('.qq.com')):
        return ''
    query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if k not in {'token','idx'}]
    token=media.get(field+'_token')
    if token:
        query.extend([('token',token),('idx','1')])
    elif 'token=' in parts.query:
        query.extend((k,v) for k,v in parse_qsl(parts.query) if k in {'token','idx'})
    return urlunsplit(('https',parts.netloc,parts.path,urlencode(query),''))


def _fetch_cdn_image(media, field):
    from .media import image_type
    from .sns_crypto import decrypt_image, decrypt_image_full
    import io
    from PIL import Image
    url=_cdn_url(media,field)
    if not url:return None
    try:
        request=Request(url,headers={'User-Agent':'MicroMessenger Client','Accept':'image/*,*/*'})
        with _cdn_opener.open(request,timeout=12) as response:
            if int(response.headers.get('Content-Length') or 0)>25*1024*1024:return None
            data=response.read(25*1024*1024+1)
        if len(data)>25*1024*1024:return None
        variants = [data] if image_type(data) else [
            decrypt_image_full(data,media.get(field+'_key','')),
            decrypt_image(data,media.get(field+'_key',''))]
        for candidate in variants:
            ext=image_type(candidate)
            if ext not in {'jpg','png','gif','webp','bmp'}:continue
            try:
                width,height=_image_complete(candidate)
                return candidate,ext,width,height
            except (OSError,ValueError):continue
        return None
    except (OSError,ValueError,OverflowError):
        return None


def restore_moments_media(archive, ids, progress=None, cancel=None, max_images=90):
    """Fetch a bounded page of images by each post's own authenticated CDN URL."""
    ids=list(dict.fromkeys(str(x) for x in ids if str(x)))[:50]
    if not ids:return {'updated':[],'recovered':0}
    assets=archive.directory/'assets'/'moments';assets.mkdir(parents=True,exist_ok=True)
    from concurrent.futures import ThreadPoolExecutor
    changes=[];attempts=0
    with sqlite3.connect(archive.path,timeout=30) as c:
        c.execute('CREATE TABLE IF NOT EXISTS sns_media_catalog(asset TEXT PRIMARY KEY,width INTEGER,height INTEGER,bytes INTEGER,mtime INTEGER)')
        rows=c.execute('SELECT id,detail FROM moments WHERE id IN ('+','.join('?' for _ in ids)+')',ids).fetchall()
        parsed={mid:json.loads(detail) for mid,detail in rows}
        tasks=[]
        for mid,data in parsed.items():
            for index,item in enumerate(data.get('media',[])):
                if item.get('type')!='2':continue
                if item.get('media_path') and _linked_image_ok(archive,item['media_path']):continue
                if len(tasks)>=max_images:break
                tasks.append((mid,index,item))
        attempts=len(tasks)
        def fetch(task):
            item=task[2]
            relative=item.get('media_path')
            if relative:
                try:
                    from .sns_crypto import repair_partial_image
                    original=inside(archive.directory/'assets',archive.directory/relative).read_bytes()
                    repaired=repair_partial_image(original,item.get('url_key',''))
                    width,height=_image_complete(repaired)
                    return (repaired,'jpg',width,height),'已修复本机旧版半图'
                except (OSError,ValueError):pass
            result=_fetch_cdn_image(item,'url')
            if result is not None:return result,'原动态图片地址'
            return _fetch_cdn_image(item,'thumb'),'原动态缩略图地址'
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(fetch,tasks))
        changed_ids=set()
        for (mid,index,_),(result,source) in zip(tasks,results):
            check_cancel(cancel)
            item=parsed[mid]['media'][index]
            if result is None:
                item['media_path']=''
                item['media_status']='原动态图片地址暂不可用；缓存尚未对应'
                changes.append({'mid':mid,'index':index,'media_path':'','media_status':item['media_status']})
            else:
                payload,ext,width,height=result
                digest=hashlib.sha256(payload).hexdigest()
                destination=assets/(digest+'.'+ext)
                _write_asset(destination,payload)
                relative=destination.relative_to(archive.directory).as_posix()
                item['media_path']=relative;item['media_status']='已恢复（'+source+'）'
                c.execute('INSERT OR REPLACE INTO sns_media_catalog VALUES(?,?,?,?,?)',(relative,width,height,len(payload),0))
                changes.append({'mid':mid,'index':index,'media_path':relative,'media_status':item['media_status']})
            changed_ids.add(mid)
        for row_index,(mid,_) in enumerate(rows):
            if mid in changed_ids:c.execute('UPDATE moments SET detail=? WHERE id=?',(json.dumps(parsed[mid],ensure_ascii=False),mid))
            if progress and row_index%5==0:
                progress('moments_media',int(100*(row_index+1)/max(1,len(rows))),f'正在恢复动态原图 · {row_index+1:,} / {len(rows):,}')
        c.commit()
    return {'updated':changes,'recovered':sum(bool(x['media_path']) for x in changes),'attempted':attempts}


def repair_archived_moments(archive, progress=None, cancel=None):
    """Repair already linked partial JPEGs using their own moment's media key."""
    from .sns_crypto import repair_partial_image
    assets=archive.directory/'assets'/'moments'
    assets.mkdir(parents=True,exist_ok=True)
    repaired=broken=checked=0
    with sqlite3.connect(archive.path,timeout=30) as c:
        rows=c.execute('SELECT id,detail FROM moments').fetchall()
        validity={}
        for index,(mid,raw) in enumerate(rows):
            check_cancel(cancel)
            detail=json.loads(raw);changed=False
            for media in detail.get('media',[]):
                relative=media.get('media_path')
                if not relative or media.get('type')!='2':continue
                checked+=1
                if relative not in validity:
                    validity[relative]=_linked_image_ok(archive,relative)
                if validity[relative]:continue
                try:
                    original=inside(archive.directory/'assets',archive.directory/relative).read_bytes()
                    restored=repair_partial_image(original,media.get('url_key',''))
                    _image_complete(restored)
                    dest=assets/(hashlib.sha256(restored).hexdigest()+'.jpg')
                    _write_asset(dest,restored)
                    media['media_path']=dest.relative_to(archive.directory).as_posix()
                    media['media_status']='已修复完整图片（本机缓存）'
                    changed=True;repaired+=1
                except (OSError,ValueError):
                    broken+=1
            if changed:c.execute('UPDATE moments SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),mid))
            if progress and index%100==0:
                progress('moments_repair',int(100*(index+1)/max(1,len(rows))),f'检查朋友圈图片 · {index+1:,} / {len(rows):,}')
        c.commit()
    return {'checked':checked,'repaired':repaired,'remaining_broken':broken}


def media_candidates(archive, mid, index, offset=0):
    with archive.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='sns_media_catalog'").fetchone():
            return {'items':[],'total':0}
        row=c.execute('SELECT raw FROM moments WHERE id=?',(str(mid),)).fetchone()
        if not row:raise ValueError('动态不存在')
        items=parse_moment(row[0])['detail']['media']
        if not 0<=int(index)<len(items):raise ValueError('图片编号无效')
        size=items[int(index)].get('size',{})
        width,height=int(float(size.get('width',0))),int(float(size.get('height',0)))
        total=c.execute('SELECT count(*) FROM sns_media_catalog WHERE width>0').fetchone()[0]
        rows=c.execute('SELECT * FROM sns_media_catalog WHERE width>0 ORDER BY (width=? AND height=?) DESC,mtime DESC,asset LIMIT 40 OFFSET ?', (width,height,max(0,int(offset)))).fetchall()
        return {'items':[dict(r) for r in rows],'total':total}


def bind_media(archive,mid,index,asset):
    """An explicit user choice, never an automatic dimensions-based association."""
    path=inside(archive.directory,archive.directory/asset)
    if not path.is_file():raise ValueError('图片文件不存在')
    with sqlite3.connect(archive.path,timeout=30) as c:
        if not c.execute('SELECT 1 FROM sns_media_catalog WHERE asset=?',(asset,)).fetchone():raise ValueError('不是本机朋友圈缓存图片')
        row=c.execute('SELECT detail FROM moments WHERE id=?',(str(mid),)).fetchone()
        if not row:raise ValueError('动态不存在')
        data=json.loads(row[0]);index=int(index)
        if not 0<=index<len(data['media']):raise ValueError('图片编号无效')
        item=data['media'][index];item['media_path']=asset;item['media_status']='用户确认的本机图片';item['manual']=True
        c.execute('UPDATE moments SET detail=? WHERE id=?',(json.dumps(data,ensure_ascii=False),str(mid)))
    return {'ok':True}


def list_moments(archive,q='',offset=0,limit=30):
    with archive.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='moments'").fetchone():return {'items':[],'total':0}
        clause='(m.body LIKE ? OR m.nickname LIKE ? OR m.username LIKE ?)';args=['%'+q+'%']*3
        total=c.execute('SELECT count(*) FROM moments m WHERE '+clause,args).fetchone()[0]
        rows=c.execute("SELECT m.*,coalesce(c.avatar,'') avatar,coalesce(nullif(c.remark,''),nullif(m.nickname,''),c.nickname,m.username) display_name FROM moments m LEFT JOIN contacts c ON c.username=m.username WHERE "+clause+' ORDER BY m.ts DESC,m.id DESC LIMIT ? OFFSET ?',args+[max(1,min(10000,int(limit))),max(0,int(offset))]).fetchall()
        items=[]
        for row in rows:
            item=dict(row);item.pop('raw');item.pop('digest');item['detail']=json.loads(item['detail']);items.append(item)
        return {'items':items,'total':total}


def export_moments(archive,options,output_root,progress,cancel=None):
    from .common import STATIC
    from .exporter import csv_cell
    output=Path(output_root).expanduser().resolve()/('朋友圈_'+uuid.uuid4().hex[:8]);output.mkdir(parents=True)
    try:
        items=[];offset=0
        while True:
            check_cancel(cancel);page=list_moments(archive,options.get('q',''),offset,500)
            items.extend(page['items']);offset+=len(page['items'])
            if offset>=page['total'] or not page['items']:break
        if options.get('include_media',True):
            for start in range(0,len(items),10):
                check_cancel(cancel)
                batch=items[start:start+10]
                restored=restore_moments_media(archive,[item['id'] for item in batch],cancel=cancel)
                by_post={}
                for update in restored['updated']:
                    by_post.setdefault(update['mid'],[]).append(update)
                for item in batch:
                    for update in by_post.get(item['id'],[]):
                        item['detail']['media'][update['index']].update(media_path=update['media_path'],media_status=update['media_status'])
                progress('export_media',int(70*(start+len(batch))/max(1,len(items))),f'正在恢复朋友圈图片 · {start+len(batch):,} / {len(items):,}')
        for i,item in enumerate(items):
            check_cancel(cancel)
            for media in [*item['detail']['media'],{'media_path':item['avatar']}]:
                rel=media.get('media_path')
                if rel:
                    source=inside(archive.directory,archive.directory/rel);dest=inside(output,output/rel)
                    if source.is_file():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
                    else:media['media_path']=''
            for media in item['detail']['media']:
                for secret in ('url_key','url_token','thumb_key','thumb_token','video_key'):
                    media.pop(secret,None)
                for field in ('url','thumb'):
                    try:source_url=urlsplit(media.get(field) or '')
                    except ValueError:continue
                    if source_url.query:
                        clean_query=urlencode([(k,v) for k,v in parse_qsl(source_url.query) if k not in {'token','key','enc_idx'}])
                        media[field]=urlunsplit((source_url.scheme,source_url.netloc,source_url.path,clean_query,''))
            if i%100==0:progress('export',70+int(20*i/max(1,len(items))),f'正在导出朋友圈 · {i:,} / {len(items):,}')
        (output/'朋友圈.json').write_text(json.dumps(items,ensure_ascii=False,indent=2),'utf-8')
        with (output/'朋友圈.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['动态编号','作者','时间戳','内容','点赞人数','评论数'])
            for item in items:writer.writerow([csv_cell(x) for x in [item['id'],item['display_name'],item['ts'],item['body'],len(item['detail']['likes']),len(item['detail']['comments'])]])
        data=json.dumps(items,ensure_ascii=False).replace('<','\\u003c').replace('&','\\u0026')
        style=(STATIC/'moments.css').read_text('utf-8');renderer=(STATIC/'moments.js').read_text('utf-8')
        page='<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>朋友圈 · 拾光</title><style>'+style+'</style><body class="moments-export"><main><h1>朋友圈</h1><p>本机缓存的动态；未保存的媒体会注明缺失。搜索文字或作者。</p><input id="filter" placeholder="搜索朋友圈"><div id="feed"></div><button id="more">加载更多</button></main><script id="data" type="application/json">'+data+'</script><script>'+renderer+"\nconst items=JSON.parse(document.getElementById('data').textContent);let limit=30;function render(){const q=document.getElementById('filter').value.toLowerCase(),filtered=items.filter(x=>(x.body+x.display_name).toLowerCase().includes(q));document.getElementById('feed').innerHTML=filtered.slice(0,limit).map(x=>MomentRenderer.card(x,p=>p)).join('');document.getElementById('more').hidden=limit>=filtered.length;}document.getElementById('filter').oninput=()=>{limit=30;render()};document.getElementById('more').onclick=()=>{limit+=30;render()};render();</script>"
        index=output/'朋友圈.html';index.write_text(page,'utf-8')
        return {'path':str(output),'index':str(index),'moments':len(items)}
    except BaseException:shutil.rmtree(output,ignore_errors=True);raise
