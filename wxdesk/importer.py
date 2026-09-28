from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .common import BASE, Cancelled, atomic_json, check_cancel, clean_error, inside, read_json, self_username
from .discovery import find_verified
from .media import MediaResolver, image_parameters, image_type
from .messages import parse_message, packed_text, text_content
from .store import create_store, finalize, set_meta, ensure_schema


def command(args, progress, cancel, phase, percent):
    launcher = ([sys.executable, "--wxdump-cli"] if getattr(sys, "frozen", False)
                else [sys.executable, "-u", str(BASE / "wxdump.py")])
    proc = subprocess.Popen([*launcher, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", cwd=str(BASE),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue()
    def reader():
        for line in proc.stdout:
            lines.put(line.rstrip())
    thread = threading.Thread(target=reader, daemon=True); thread.start()
    try:
        while proc.poll() is None or not lines.empty():
            check_cancel(cancel)
            try:
                line = lines.get(timeout=.2)
                if line:
                    if line == '正在验证数据库':
                        phase, percent = 'verify', 16
                    progress(phase, percent, clean_error(line))
            except queue.Empty:
                pass
        thread.join(timeout=2)
        if proc.wait() != 0:
            raise ValueError("读取微信数据库失败，请查看任务日志。确认微信已经登录后重试。")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        proc.stdout.close()


@contextlib.contextmanager
def encrypted(path, key):
    import sqlcipher3
    c = sqlcipher3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError("数据库密钥格式不正确。")
        c.execute('PRAGMA key = "x\'%s\'"' % key)
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        yield c
    finally:
        c.close()


def snapshot(path, key, destination, cancel):
    import sqlcipher3
    destination.parent.mkdir(parents=True, exist_ok=True)
    with encrypted(path, key) as src:
        dst = sqlcipher3.connect(str(destination))
        try:
            dst.execute('PRAGMA key = "x\'%s\'"' % key)
            src.backup(dst, pages=256, progress=lambda *args: check_cancel(cancel), sleep=.05)
            dst.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            dst.close()


def tables(c):
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def qi(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def columns(c, table):
    return {r[1] for r in c.execute("PRAGMA table_info(" + qi(table) + ")")}


def import_contacts(source, dest, account):
    names = {}
    if "contact" not in tables(source):
        return names
    available = columns(source, "contact")
    selected = [name if name in available else "0 AS " + name for name in ["username", "nick_name", "remark", "alias", "verify_flag", "local_type", "flag", "delete_flag"]]
    dest.execute('DELETE FROM friend_list')
    for username, nickname, remark, alias, verify, local_type, flag, deleted in source.execute("SELECT " + ",".join(selected) + " FROM contact"):
        if not username:
            continue
        kind = "group" if username.endswith("@chatroom") else ("official" if username.startswith("gh_") or verify else "direct")
        names[username] = text_content(remark or nickname or username)
        dest.execute("INSERT INTO contacts(username,nickname,remark,alias,kind) VALUES(?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET nickname=excluded.nickname,remark=excluded.remark,alias=excluded.alias,kind=excluded.kind",
                     (username, text_content(nickname), text_content(remark), text_content(alias), kind))
        if (local_type == 1 and int(flag or 0) & 1 and not deleted and kind == 'direct'
                and username not in {self_username(account['account']), 'filehelper', 'fmessage', 'medianote', 'floatbottle', 'newsapp'}):
            dest.execute('INSERT OR IGNORE INTO friend_list VALUES(?)', (username,))
    set_meta(dest, 'friend_filter_ready', 'local_type' in available and 'flag' in available)
    return names


def import_hardlinks(source, dest):
    names = set(tables(source))
    if "dir2id" not in names:
        return
    dirs = dict(source.execute("SELECT rowid,username FROM dir2id"))
    for kind in ["image", "video", "file"]:
        name = next((f"{kind}_hardlink_info_v{v}" for v in [4, 3] if f"{kind}_hardlink_info_v{v}" in names), None)
        if not name:
            continue
        for md5, filename, dir1, dir2, typ, extra in source.execute("SELECT md5,file_name,dir1,dir2,type,extra_buffer FROM " + qi(name)):
            if not filename or not md5:
                continue
            a, b = dirs.get(dir1, ""), dirs.get(dir2, "")
            rec = packed_text(extra or b"", 1)
            if kind == "image":
                path = Path("msg/attach") / a / b
                path = path / "Rec" / rec / "Img" / filename if typ == 4 and rec else path / "Img" / filename
            elif kind == "video":
                path = Path("msg/video") / a
                path = path / b / "Rec" / rec / "V" / filename if typ == 5 and rec else path / filename
            else:
                path = Path("msg/attach") / a / b / rec / filename if typ == 6 and rec else Path("msg/file") / a / filename
            dest.execute("INSERT OR IGNORE INTO media_map VALUES(?,?,?)", (md5, kind, path.as_posix()))


def sync_account(account, directory, progress, cancel=None, include_media=True, force_keys=False, fast=False):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    dbdir = Path(account["dbdir"]).resolve()
    if dbdir.parent.name != account["account"]:
        raise ValueError("账号与数据库目录不一致。")
    account_root = dbdir.parent
    verified = None if force_keys else find_verified(account["account"], str(dbdir))
    required = {p.relative_to(dbdir).as_posix() for p in dbdir.rglob("*.db") if re.fullmatch(r"(?:biz_)?message_\d+\.db", p.name)}
    if verified and not required.issubset(set(read_json(verified).get("keys", {}))):
        verified = None
    if verified and not fast:
        cached = read_json(verified).get('keys', {})
        for rel in sorted(required | ({'contact/contact.db'} & set(cached))):
            check_cancel(cancel)
            try:
                with encrypted(dbdir / rel, cached[rel]['key']):
                    pass
            except Exception:
                progress('keys', 3, '已有密钥与当前数据库不匹配，正在重新获取。')
                verified = None
                break
    if not verified:
        if not account.get("active"):
            raise ValueError("还没有此账号的可用密钥，请先登录这个微信账号。")
        raw = directory / "keys.raw.json"
        verified = str(directory / "keys.verified.json")
        progress("keys", 5, "正在读取当前账号的数据库访问密钥")
        command(['prepare', '--data', str(dbdir.parent.parent), '--acct', account['account'],
                 '--raw', str(raw), '--out', verified], progress, cancel, 'keys', 8)
    manifest = read_json(verified)
    if manifest.get("account") != account["account"] or Path(manifest.get("dbdir", "")).resolve() != dbdir:
        raise ValueError("密钥清单不属于当前账号。")
    keys = manifest.get("keys", {})
    missing = required.difference(keys)
    if missing:
        raise ValueError(f"有 {len(missing)} 个聊天数据库未通过验证，已停止同步以保留原有归档。请在微信中打开相关会话后重试。")
    if (directory / 'archive.sqlite').is_file():
        from .incremental import incremental_sync
        return incremental_sync(account, directory, keys, progress, cancel, include_media)
    run = uuid.uuid4().hex
    work = directory / (".build-" + run)
    work.mkdir()
    target = work / "archive.sqlite"
    assets = directory / "assets"
    snapshots, names = {}, {}
    c = None
    try:
        selected = {rel: data for rel, data in keys.items() if Path(rel).name in {"contact.db", "hardlink.db", "head_image.db"}
                    or re.fullmatch(r"(?:(?:biz_)?message|media)_\d+\.db", Path(rel).name)}
        if not any(re.fullmatch(r"(?:biz_)?message_\d+\.db", Path(r).name) for r in selected):
            raise ValueError("此账号没有可读取的聊天数据库。")
        for i, (rel, data) in enumerate(selected.items()):
            check_cancel(cancel)
            source = inside(dbdir, dbdir / rel)
            dest = inside(work, work / "snapshots" / rel)
            progress("snapshot", 20 + int(15 * i / max(1, len(selected))), f"正在创建一致副本 · {i + 1} / {len(selected)}")
            if not source.is_file():
                raise ValueError("数据库文件已移动，重新识别账号后再试。")
            try:
                snapshot(source, data["key"], dest, cancel)
            except Cancelled:
                raise
            except Exception as exc:
                raise ValueError("数据库副本创建失败。可退出微信后使用已保存密钥重试；若微信已升级，请在设置中启用重新取钥。") from exc
            snapshots[rel] = dest
        c = create_store(target)
        set_meta(c, "account", account["account"])
        set_meta(c, "source", "wechat_database")
        set_meta(c, "dbdir", str(dbdir))
        set_meta(c, "source_databases", sorted(selected))
        for rel, path in snapshots.items():
            check_cancel(cancel)
            base = Path(rel).name
            with encrypted(path, keys[rel]["key"]) as src:
                if base == "contact.db":
                    names.update(import_contacts(src, c, account))
                elif base == "hardlink.db":
                    import_hardlinks(src, c)
                elif base.startswith("media_") and "VoiceInfo" in tables(src):
                    for sid, data in src.execute("SELECT svr_id,voice_data FROM VoiceInfo"):
                        if data:
                            c.execute("INSERT OR IGNORE INTO voice VALUES(?,?)", (str(sid), data))
                elif base == "head_image.db" and "head_image" in tables(src):
                    assets.mkdir(exist_ok=True)
                    for username, data in src.execute("SELECT username,image_buffer FROM head_image"):
                        if data and image_type(bytes(data)):
                            filename = "avatar_" + hashlib.sha256(bytes(data)).hexdigest()[:24] + "." + image_type(bytes(data))
                            (assets / filename).write_bytes(bytes(data))
                            c.execute("UPDATE contacts SET avatar=? WHERE username=?", ("assets/" + filename, username))
        image_key, xor_key = None, None
        if include_media:
            progress("media", 37, "正在识别此设备的图片格式")
            image_key, xor_key = image_parameters(account_root, account.get("pids", []), cancel)
        resolver = MediaResolver(account_root, assets, c, image_key, xor_key, cancel)
        chats = [(r, p) for r, p in snapshots.items() if re.fullmatch(r"(?:biz_)?message_\d+\.db", Path(r).name)]
        total_messages = 0
        for shard_index, (rel, path) in enumerate(chats):
            with encrypted(path, keys[rel]["key"]) as src:
                n2i = dict(src.execute("SELECT rowid,user_name FROM Name2Id"))
                reverse = {hashlib.md5(username.encode()).hexdigest(): username for username in n2i.values() if username}
                msg_tables = [t for t in tables(src) if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", t)]
                for ti, table in enumerate(msg_tables):
                    check_cancel(cancel)
                    cid = reverse.get(table[4:]) or "unknown:" + table[4:]
                    kind = "group" if cid.endswith("@chatroom") else ("official" if Path(rel).name.startswith("biz_") else "direct")
                    title = names.get(cid, cid)
                    c.execute("INSERT OR IGNORE INTO conversations(id,title,kind) VALUES(?,?,?)", (cid, title, kind))
                    wanted = ["local_id", "server_id", "local_type", "create_time", "real_sender_id", "message_content", "packed_info_data"]
                    actual = columns(src, table)
                    if not set(wanted[:-1]).issubset(actual):
                        raise ValueError("检测到新的微信消息表结构，请更新适配后重试。")
                    query = "SELECT " + ",".join(qi(x) if x in actual else "NULL" for x in wanted) + " FROM " + qi(table) + " ORDER BY local_id"
                    for lid, sid, ltype, ts, sender_id, content, packed in src.execute(query):
                        if total_messages % 100 == 0:
                            check_cancel(cancel)
                        sender = n2i.get(sender_id, "")
                        parsed = parse_message(ltype, content, sender, bytes(packed or b""))
                        me = sender in {self_username(account["account"]), account["account"]}
                        origin = hashlib.sha256((cid + ":" + str(sid) if sid and str(sid) != "0" else rel + ":" + table + ":" + str(lid)).encode()).hexdigest()
                        message = dict(parsed, conversation_id=cid, local_id=lid, server_id=str(sid or ""), ts=int(ts or 0))
                        if include_media:
                            media_path, media_status = resolver.resolve(message)
                            if message["kind"] == "forward":
                                resolver.forward_media(message)
                        else:
                            media_path, media_status = "", "未提取媒体" if parsed["kind"] in {"image", "video", "audio", "emoji", "file"} else ""
                        c.execute("INSERT OR IGNORE INTO messages(origin,conversation_id,sender,sender_name,is_self,ts,local_type,kind,body,detail,raw,source_db,source_table,local_id,server_id,media_path,media_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (origin, cid, sender, "我" if me else names.get(sender, sender or title), int(me), int(ts or 0), ltype, parsed["kind"], parsed["body"],
                                   json.dumps(parsed["detail"], ensure_ascii=False), parsed["raw"], rel, table, lid, str(sid or ""), media_path, media_status))
                        total_messages += 1
                        if total_messages % 1000 == 0:
                            pc = 40 + int(49 * (shard_index + ti / max(1, len(msg_tables))) / len(chats))
                            progress("import", pc, f"正在整理聊天 · 已处理 {total_messages:,} 条消息")
                    if ti % 5 == 0:
                        pc = 40 + int(49 * (shard_index + (ti + 1) / max(1, len(msg_tables))) / len(chats))
                        progress("import", pc, f"正在整理聊天 · 已处理 {total_messages:,} 条消息")
                        c.commit()
        if not total_messages:
            raise ValueError("没有读到聊天消息，原有归档已保留。")
        c.execute("DROP TABLE voice")  # Converted audio files are kept; avoid duplicating large binary payloads.
        finalize(c, progress)
        set_meta(c, "media_included", include_media)
        c.commit(); c.close(); c = None
        check_cancel(cancel)
        os.replace(target, directory / "archive.sqlite")
        meta = {k: account[k] for k in ["account", "dbdir", "root", "username"] if k in account}
        meta["display_name"] = names.get(self_username(account["account"]), account.get("display_name", account["account"]))
        atomic_json(directory / "account.json", meta)
        progress("done", 100, f"归档完成 · {total_messages:,} 条消息已整理")
        return {"messages": total_messages, "display_name": meta["display_name"]}
    finally:
        if c is not None:
            c.close()
        checked = inside(directory, work)
        shutil.rmtree(checked, ignore_errors=True)
