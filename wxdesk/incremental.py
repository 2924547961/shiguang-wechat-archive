"""Incremental, read-only source snapshots with transactional local checkpoints."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from .common import atomic_json, check_cancel, inside, self_username
from .media import MediaResolver, image_parameters, image_type
from .messages import parse_message, packed_text
from .store import ensure_schema, set_meta

_image_keys = {}


def signature(path):
    values = []
    for p in [Path(path), Path(str(path) + '-wal')]:
        try:
            s = p.stat(); values.append((s.st_size, s.st_mtime_ns))
        except FileNotFoundError:
            values.append(None)
    return json.dumps(values)


@contextlib.contextmanager
def account_lock(directory):
    p = Path(directory) / 'sync.lock'
    with p.open('a+b') as f:
        f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError('此账号正在另一个窗口同步，请等待完成。') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def incremental_sync(account, directory, keys, progress, cancel=None, include_media=True):
    from .importer import encrypted, tables, qi, columns, import_contacts
    started = time.monotonic()
    directory, dbdir = Path(directory), Path(account['dbdir'])
    assets = directory / 'assets'; assets.mkdir(exist_ok=True)
    with account_lock(directory), contextlib.ExitStack() as stack:
        c = sqlite3.connect(str(directory / 'archive.sqlite'), timeout=30)
        stack.callback(c.close)
        c.row_factory = sqlite3.Row
        stored = c.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
        if not stored or json.loads(stored[0]) != account['account']:
            raise ValueError('归档与所选微信账号不一致。')
        c.execute('PRAGMA journal_mode=WAL')
        ensure_schema(c)
        if not c.execute('SELECT 1 FROM sync_state LIMIT 1').fetchone():
            c.execute('INSERT OR IGNORE INTO sync_state SELECT source_db,source_table,max(local_id) FROM messages GROUP BY source_db,source_table')
            c.commit()
        versions = dict(c.execute('SELECT source_db,signature FROM source_versions'))
        before = {rel: signature(inside(dbdir, dbdir / rel)) for rel in keys}
        sources = {}
        def source(rel):
            if rel not in sources:
                src = stack.enter_context(encrypted(inside(dbdir, dbdir / rel), keys[rel]['key']))
                src.execute('BEGIN')
                sources[rel] = src
            return sources[rel]
        c.execute('BEGIN')
        added = updated = checked = 0; touched = set()
        try:
            progress('incremental', 10, '检查新增消息，复用已有聊天与媒体')
            contact_changed = False
            for rel in keys:
                check_cancel(cancel)
                base = Path(rel).name
                if base not in {'contact.db', 'head_image.db', 'hardlink.db'} or before[rel] == versions.get(rel):
                    continue
                src = source(rel)
                if base == 'contact.db':
                    import_contacts(src, c, account); contact_changed = True
                elif base == 'head_image.db' and 'head_image' in tables(src):
                    for username, data in src.execute('SELECT username,image_buffer FROM head_image'):
                        if data and (ext := image_type(bytes(data))):
                            filename = 'avatar_' + hashlib.sha256(bytes(data)).hexdigest()[:24] + '.' + ext
                            if not (assets / filename).exists():
                                (assets / filename).write_bytes(bytes(data))
                            c.execute('UPDATE contacts SET avatar=? WHERE username=? AND avatar<>?', ('assets/' + filename, username, 'assets/' + filename))
                    contact_changed = True
                else:
                    dirs = dict(src.execute('SELECT rowid,username FROM dir2id')) if 'dir2id' in tables(src) else {}
                    for kind in ['image', 'video', 'file']:
                        table = next((f'{kind}_hardlink_info_v{v}' for v in [4,3] if f'{kind}_hardlink_info_v{v}' in tables(src)), None)
                        if not table: continue
                        mark = c.execute('SELECT max_id FROM sync_state WHERE source_db=? AND source_table=?', (rel, table)).fetchone()
                        last = mark[0] if mark else 0
                        for rid, md5, filename, dir1, dir2, typ, extra in src.execute('SELECT rowid,md5,file_name,dir1,dir2,type,extra_buffer FROM '+qi(table)+' WHERE rowid>?', (last,)):
                            check_cancel(cancel); last = max(last, rid)
                            if not md5 or not filename: continue
                            a,b=dirs.get(dir1,''),dirs.get(dir2,'');rec=packed_text(extra or b'',1)
                            if kind=='image':
                                path=Path('msg/attach')/a/b
                                path=path/'Rec'/rec/'Img'/filename if typ==4 and rec else path/'Img'/filename
                            elif kind=='video':
                                path=Path('msg/video')/a
                                path=path/b/'Rec'/rec/'V'/filename if typ==5 and rec else path/filename
                            else:path=Path('msg/attach')/a/b/rec/filename if typ==6 and rec else Path('msg/file')/a/filename
                            c.execute('INSERT OR IGNORE INTO media_map VALUES(?,?,?)',(md5,kind,path.as_posix()))
                        c.execute('INSERT OR REPLACE INTO sync_state VALUES(?,?,?)',(rel,table,last))
                c.execute('INSERT OR REPLACE INTO source_versions VALUES(?,?)', (rel,before[rel]))
            names = {r['username']:r['remark'] or r['nickname'] or r['username'] for r in c.execute('SELECT * FROM contacts')}
            class MediaLookup:
                def execute(self, sql, args=()):
                    if sql.startswith('SELECT data FROM voice'):
                        for rel in keys:
                            if re.fullmatch(r'media_\d+\.db', Path(rel).name):
                                row=source(rel).execute('SELECT voice_data FROM VoiceInfo WHERE svr_id=? LIMIT 1',args).fetchone()
                                if row:
                                    class Result:
                                        def fetchone(self): return row
                                    return Result()
                        class Empty:
                            def fetchone(self): return None
                        return Empty()
                    return c.execute(sql,args)
            cache_id = (account['account'],tuple(account.get('pids',[])))
            image_key,xor_key=_image_keys.get(cache_id,(None,None))
            resolver=MediaResolver(dbdir.parent,assets,MediaLookup(),image_key,xor_key,cancel)
            key_tried=bool(image_key)
            def resolve(message):
                nonlocal key_tried
                if message['kind']=='image' and not key_tried:
                    key_tried=True
                    resolver.aes_key,resolver.xor_key=image_parameters(dbdir.parent,account.get('pids',[]),cancel,8)
                    if resolver.aes_key:_image_keys[cache_id]=(resolver.aes_key,resolver.xor_key)
                return resolver.resolve(message)
            chats=[rel for rel in keys if re.fullmatch(r'(?:biz_)?message_\d+\.db',Path(rel).name)]
            for index,rel in enumerate(chats):
                check_cancel(cancel)
                if before[rel] == versions.get(rel):continue
                src=source(rel);n2i=dict(src.execute('SELECT rowid,user_name FROM Name2Id'))
                reverse={hashlib.md5(name.encode()).hexdigest():name for name in n2i.values() if name}
                for table in tables(src):
                    if not re.fullmatch(r'Msg_[0-9a-fA-F]{32}',table):continue
                    check_cancel(cancel)
                    mark=c.execute('SELECT max_id FROM sync_state WHERE source_db=? AND source_table=?',(rel,table)).fetchone()
                    high=src.execute('SELECT max(local_id) FROM '+qi(table)).fetchone()[0] or 0
                    last=mark[0] if mark and high>=mark[0] else 0
                    cid=reverse.get(table[4:]) or 'unknown:'+table[4:]
                    kind='group' if cid.endswith('@chatroom') else 'official' if Path(rel).name.startswith('biz_') else 'direct'
                    fields=['local_id','server_id','local_type','create_time','real_sender_id','message_content','packed_info_data']
                    available=columns(src,table)
                    if not set(fields[:-1]).issubset(available):raise ValueError('消息表结构已变化，请更新适配。')
                    query='SELECT '+','.join(qi(k) if k in available else 'NULL' for k in fields)+' FROM '+qi(table)+' WHERE local_id>? ORDER BY local_id'
                    for lid,sid,ltype,ts,sender_id,content,packed in src.execute(query,(max(0,last-8),)):
                        check_cancel(cancel);checked+=1
                        sender=n2i.get(sender_id,'');parsed=parse_message(ltype,content,sender,bytes(packed or b''))
                        origin=hashlib.sha256((cid+':'+str(sid) if sid and str(sid)!='0' else rel+':'+table+':'+str(lid)).encode()).hexdigest()
                        old=c.execute('SELECT id,raw,local_type,server_id,media_path,media_status,detail,kind FROM messages WHERE origin=?',(origin,)).fetchone()
                        if old is None:
                            old=c.execute('SELECT id,raw,local_type,server_id,media_path,media_status,detail,kind FROM messages WHERE source_db=? AND source_table=? AND local_id=?',(rel,table,lid)).fetchone()
                        if old and old['server_id'] not in ('', '0', str(sid or '')) and not (parsed['detail'].get('revoke') and old['kind']!='system'):
                            old=None
                        if old and old['raw']==parsed['raw'] and old['local_type']==ltype and old['server_id']==str(sid or ''):continue
                        # A recall may overwrite the source row. Keep the previously
                        # observed message in this local archive and mark it recalled.
                        if old and parsed['detail'].get('revoke') and old['kind']!='system':
                            detail=json.loads(old['detail']);detail['recalled']=True
                            c.execute('UPDATE messages SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),old['id']))
                            updated+=1;touched.add(cid)
                            continue
                        c.execute('INSERT OR IGNORE INTO conversations(id,title,kind) VALUES(?,?,?)',(cid,names.get(cid,cid),kind))
                        message=dict(parsed,conversation_id=cid,local_id=lid,server_id=str(sid or ''),ts=int(ts or 0))
                        media,status=(old['media_path'],old['media_status']) if old else ('','未提取媒体' if message['kind'] in {'image','emoji','audio','video','file'} else '')
                        if include_media and not media:
                            media,status=resolve(message)
                            if message['kind']=='forward':resolver.forward_media(message)
                        me=sender in {self_username(account['account']),account['account']}
                        values=(origin,cid,sender,'我' if me else names.get(sender,sender or names.get(cid,cid)),int(me),int(ts or 0),ltype,message['kind'],message['body'],json.dumps(message['detail'],ensure_ascii=False),message['raw'],rel,table,lid,str(sid or ''),media,status)
                        if old:
                            c.execute('UPDATE messages SET origin=?,conversation_id=?,sender=?,sender_name=?,is_self=?,ts=?,local_type=?,kind=?,body=?,detail=?,raw=?,source_db=?,source_table=?,local_id=?,server_id=?,media_path=?,media_status=? WHERE id=?',(*values,old['id']));updated+=1
                        else:
                            c.execute('INSERT INTO messages(origin,conversation_id,sender,sender_name,is_self,ts,local_type,kind,body,detail,raw,source_db,source_table,local_id,server_id,media_path,media_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values);added+=1
                        if parsed['detail'].get('revoked_server_id'):
                            target=c.execute('SELECT id,detail FROM messages WHERE conversation_id=? AND server_id=? AND kind<>\'system\' ORDER BY id DESC LIMIT 1',(cid,parsed['detail']['revoked_server_id'])).fetchone()
                            if target:
                                detail=json.loads(target['detail']);detail['recalled']=True
                                c.execute('UPDATE messages SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),target['id']))
                        touched.add(cid)
                    c.execute('INSERT OR REPLACE INTO sync_state VALUES(?,?,?)',(rel,table,high))
                c.execute('INSERT OR REPLACE INTO source_versions VALUES(?,?)',(rel,before[rel]))
                progress('incremental',25+int(55*(index+1)/max(1,len(chats))),f'已新增 {added:,} 条，更新 {updated:,} 条；历史媒体已复用')
            # Retry recent pending attachments: WeChat may download them after the message arrives.
            if include_media:
                for r in c.execute("SELECT * FROM messages WHERE media_path='' AND ((kind IN ('image','video','audio','file') AND ts>?) OR (kind='image' AND media_status=?)) ORDER BY ts DESC LIMIT 80",(int(time.time())-7*86400,'缺少当前版本的图片密钥')).fetchall():
                    check_cancel(cancel);message=dict(r);message['detail']=json.loads(r['detail'])
                    media,status=resolve(message)
                    if media:c.execute('UPDATE messages SET media_path=?,media_status=? WHERE id=?',(media,status,r['id']));updated+=1
            for cid in touched:
                c.execute("UPDATE conversations SET total=(SELECT count(*) FROM messages WHERE conversation_id=?),last_ts=(SELECT max(ts) FROM messages WHERE conversation_id=?),preview=(SELECT substr(body,1,100) FROM messages WHERE conversation_id=? ORDER BY ts DESC,id DESC LIMIT 1) WHERE id=?",(cid,cid,cid,cid))
            if contact_changed:
                c.execute("UPDATE conversations SET title=coalesce((SELECT coalesce(nullif(remark,''),nullif(nickname,''),username) FROM contacts WHERE username=conversations.id),title),avatar=coalesce((SELECT avatar FROM contacts WHERE username=conversations.id),'')")
            check_cancel(cancel)
            set_meta(c,'updated_at',dt.datetime.now().isoformat(timespec='seconds'))
            set_meta(c,'sync_mode','incremental');set_meta(c,'media_included',include_media)
            c.commit()
            total=c.execute('SELECT count(*) FROM messages').fetchone()[0]
            seconds=round(time.monotonic()-started,2)
            progress('done',100,f'增量同步完成：新增 {added:,} 条，更新 {updated:,} 条，用时 {seconds:g} 秒')
            return {'messages':total,'new_messages':added,'updated_messages':updated,'checked':checked,'elapsed':seconds,'incremental':True}
        except BaseException:
            c.rollback();raise
