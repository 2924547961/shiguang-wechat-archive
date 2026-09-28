from __future__ import annotations

import collections
import contextlib
import csv
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .common import check_cancel, inside
from .messages import KIND_NAMES

SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE contacts(username TEXT PRIMARY KEY,nickname TEXT,remark TEXT,alias TEXT,kind TEXT,avatar TEXT DEFAULT '');
CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,total INTEGER DEFAULT 0,
 last_ts INTEGER DEFAULT 0,preview TEXT DEFAULT '',avatar TEXT DEFAULT '');
CREATE TABLE messages(id INTEGER PRIMARY KEY,origin TEXT UNIQUE,conversation_id TEXT NOT NULL,sender TEXT,
 sender_name TEXT,is_self INTEGER NOT NULL,ts INTEGER NOT NULL,local_type INTEGER,kind TEXT,body TEXT,
 detail TEXT NOT NULL DEFAULT '{}',raw TEXT,source_db TEXT,source_table TEXT,local_id INTEGER,server_id TEXT,
 media_path TEXT DEFAULT '',media_status TEXT DEFAULT '');
CREATE TABLE media_map(md5 TEXT,kind TEXT,path TEXT,PRIMARY KEY(md5,kind,path));
CREATE TABLE voice(server_id TEXT PRIMARY KEY,data BLOB);
CREATE INDEX messages_conversation_time ON messages(conversation_id,ts,id);
CREATE INDEX messages_time ON messages(ts);
CREATE INDEX messages_server ON messages(conversation_id,server_id);
CREATE INDEX messages_kind ON messages(kind);
"""


def create_store(path):
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    ensure_schema(c)
    return c


def ensure_schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS friend_list(username TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS sync_state(source_db TEXT,source_table TEXT,max_id INTEGER,PRIMARY KEY(source_db,source_table));
    CREATE TABLE IF NOT EXISTS source_versions(source_db TEXT PRIMARY KEY,signature TEXT);
    CREATE INDEX IF NOT EXISTS messages_source_local ON messages(source_db,source_table,local_id);
    CREATE TABLE IF NOT EXISTS moments(id TEXT PRIMARY KEY,username TEXT,nickname TEXT,ts INTEGER,body TEXT,detail TEXT,raw TEXT,digest TEXT);
    CREATE INDEX IF NOT EXISTS moments_time ON moments(ts DESC);
    ''')


def friends_clause(c, prefix=''):
    ready = c.execute("SELECT value FROM metadata WHERE key='friend_filter_ready'").fetchone()
    return prefix + "username IN (SELECT username FROM friend_list)" if ready and json.loads(ready[0]) else prefix + "kind='direct'"


def set_meta(c, key, value):
    c.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def finalize(c, progress=lambda *a: None):
    progress("index", 92, "正在整理会话与搜索索引")
    c.execute("UPDATE conversations SET total=(SELECT count(*) FROM messages m WHERE m.conversation_id=conversations.id),"
              "last_ts=coalesce((SELECT max(ts) FROM messages m WHERE m.conversation_id=conversations.id),0),"
              "preview=coalesce((SELECT substr(body,1,100) FROM messages m WHERE m.conversation_id=conversations.id ORDER BY ts DESC,id DESC LIMIT 1),'')")
    c.execute("DELETE FROM conversations WHERE total=0")
    c.execute("UPDATE conversations SET avatar=coalesce((SELECT avatar FROM contacts WHERE username=conversations.id),'')")
    c.execute("ANALYZE")
    set_meta(c, "updated_at", dt.datetime.now().isoformat(timespec="seconds"))
    c.commit()


def sql_filter(conversations=None, start=None, end=None, prefix=""):
    conditions, args = [], []
    if conversations is not None:
        if not conversations:
            conditions.append("0")
        else:
            conditions.append(prefix + "conversation_id IN (" + ",".join("?" for _ in conversations) + ")")
            args.extend(conversations)
    if start:
        conditions.append(prefix + "ts>=?"); args.append(int(start))
    if end:
        conditions.append(prefix + "ts<?"); args.append(int(end))
    return (" AND ".join(conditions) or "1"), args


def unpack_message(row):
    r = dict(row)
    try:
        r["detail"] = json.loads(r.get("detail") or "{}")
    except ValueError:
        r["detail"] = {}
    r.pop("raw", None)
    r["time"] = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S") if r["ts"] else "时间未知"
    r["label"] = KIND_NAMES.get(r["kind"], "其他消息")
    return r


class Archive:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "archive.sqlite"

    @contextlib.contextmanager
    def connect(self):
        if not self.path.is_file():
            raise ValueError("请先同步聊天记录，或导入已有归档。")
        c = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def media_health(self):
        with self.connect() as c:
            rows = c.execute("SELECT kind,media_status,count(*) count,sum(CASE WHEN media_path<>'' THEN 1 ELSE 0 END) linked FROM messages WHERE kind IN ('image','emoji') GROUP BY kind,media_status").fetchall()
            recent = c.execute("SELECT media_path FROM messages WHERE kind IN ('image','emoji') AND media_path<>'' ORDER BY id DESC LIMIT 300").fetchall()
        counts = {'image': 0, 'emoji': 0, 'linked': 0, 'unavailable': 0, 'thumbnail': 0, 'first_frame': 0}
        for row in rows:
            amount = int(row['count'])
            counts[row['kind']] += amount
            status = row['media_status'] or ''
            if status == '缩略图': counts['thumbnail'] += amount
            if status == 'WXGF 首帧': counts['first_frame'] += amount
            counts['linked'] += int(row['linked'] or 0)
            counts['unavailable'] += amount - int(row['linked'] or 0)
        broken = 0
        for row in recent:
            try:
                if not inside(self.directory / 'assets', self.directory / row['media_path']).is_file():
                    broken += 1
            except ValueError:
                broken += 1
        counts['recent_checked'] = len(recent)
        counts['recent_broken'] = broken
        return counts

    def metadata(self):
        with self.connect() as c:
            return {r[0]: json.loads(r[1]) for r in c.execute("SELECT key,value FROM metadata")}

    def stats(self):
        with self.connect() as c:
            stats = dict(c.execute("SELECT count(*) messages,min(ts) first_ts,max(ts) last_ts,"
                                   "count(DISTINCT date(ts,'unixepoch','localtime')) days FROM messages").fetchone())
            stats["conversations"] = c.execute("SELECT count(*) FROM conversations").fetchone()[0]
            stats["contacts"] = c.execute("SELECT count(*) FROM contacts WHERE " + friends_clause(c)).fetchone()[0]
            stats["years"] = [int(r[0]) for r in c.execute("SELECT DISTINCT strftime('%Y',ts,'unixepoch','localtime') y FROM messages WHERE ts>0 ORDER BY y DESC")]
            stats["media"] = dict(c.execute("SELECT CASE WHEN media_status='' THEN '未解析' ELSE media_status END,count(*) FROM messages WHERE kind IN ('image','video','audio','emoji','file') GROUP BY media_status"))
            stats["metadata"] = {r[0]: json.loads(r[1]) for r in c.execute("SELECT key,value FROM metadata")}
            return stats

    def conversations(self, query="", kind="all", offset=0, limit=100):
        where, args = ["1"], []
        if query:
            where.append("(title LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args += ["%" + escaped + "%"] * 2
        if kind in {"direct", "group", "official"}:
            where.append("kind=?"); args.append(kind)
        clause = " AND ".join(where)
        with self.connect() as c:
            total = c.execute("SELECT count(*) FROM conversations WHERE " + clause, args).fetchone()[0]
            rows = c.execute("SELECT * FROM conversations WHERE " + clause + " ORDER BY last_ts DESC,id LIMIT ? OFFSET ?", [*args, min(5000, max(1, limit)), max(0, offset)]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total}

    def contacts(self, query="", offset=0, limit=100):
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        terms = ["%" + escaped + "%"] * 4
        clause = "nickname LIKE ? ESCAPE '\\' OR remark LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR alias LIKE ? ESCAPE '\\'"
        with self.connect() as c:
            clause = '(' + clause + ') AND ' + friends_clause(c)
            total = c.execute("SELECT count(*) FROM contacts WHERE " + clause, terms).fetchone()[0]
            rows = c.execute("SELECT * FROM contacts WHERE " + clause + " ORDER BY coalesce(nullif(remark,''),nickname,username) LIMIT ? OFFSET ?", [*terms, min(5000, max(1, limit)), max(0, offset)]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total}

    def messages(self, cid, query="", offset=0, limit=80, until=None, server_id="", kind=""):
        where, args = ["conversation_id=?"], [cid]
        with self.connect() as c:
            if server_id:
                target = c.execute("SELECT id,ts FROM messages WHERE conversation_id=? AND server_id=? ORDER BY id LIMIT 1", (cid, server_id)).fetchone()
                if target is None:
                    return {"items": [], "total": 0, "has_more": False, "not_found": True}
                # Place the referenced message near the middle of the loaded page.
                offset = max(0, c.execute("SELECT count(*) FROM messages WHERE conversation_id=? AND (ts>? OR (ts=? AND id>?))", (cid, target["ts"], target["ts"], target["id"])).fetchone()[0] - limit // 2)
                query, until, kind = "", None, ""
            if query:
                where.append("body LIKE ? ESCAPE '\\'")
                args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
            if until:
                where.append("ts<?"); args.append(int(until))
            if kind in KIND_NAMES:
                where.append("kind=?"); args.append(kind)
            clause = " AND ".join(where)
            total = c.execute("SELECT count(*) FROM messages WHERE " + clause, args).fetchone()[0]
            rows = c.execute("SELECT messages.*,coalesce((SELECT avatar FROM contacts WHERE username=messages.sender),'') sender_avatar FROM messages WHERE " + clause + " ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?", [*args, min(limit, 200), max(0, offset)]).fetchall()
        return {"items": [unpack_message(r) for r in reversed(rows)], "total": total,
                "has_more": offset + len(rows) < total, "offset": offset + len(rows), "target": server_id}

    def iterate(self, conversations=None, start=None, end=None):
        clause, args = sql_filter(conversations, start, end)
        with self.connect() as c:
            for r in c.execute("SELECT messages.*,coalesce((SELECT avatar FROM contacts WHERE username=messages.sender),'') sender_avatar FROM messages WHERE " + clause + " ORDER BY ts,id", args):
                yield dict(r)

    def annual(self, year, cid=None, cancel=None):
        year = int(year)
        if not 1970 <= year <= 2100:
            raise ValueError("年份不正确。")
        start, end = int(dt.datetime(year, 1, 1).timestamp()), int(dt.datetime(year + 1, 1, 1).timestamp())
        clause, args = sql_filter([cid] if cid else None, start, end)
        with self.connect() as c:
            row = c.execute("SELECT count(*) total,coalesce(sum(is_self),0) sent,min(ts) first_ts,max(ts) last_ts,"
                            "count(DISTINCT date(ts,'unixepoch','localtime')) days FROM messages WHERE " + clause, args).fetchone()
            result = dict(row)
            result.update(year=year, conversation_id=cid, title="我的年度回顾")
            if cid:
                chat = c.execute("SELECT title,kind FROM conversations WHERE id=?", (cid,)).fetchone()
                if not chat or chat["kind"] != "direct":
                    raise ValueError("双人年报请选择一个单聊联系人。")
                result["title"] = "我和" + chat["title"]
            result["received"] = result["total"] - result["sent"]
            result["months"] = [0] * 12
            for month, n in c.execute("SELECT strftime('%m',ts,'unixepoch','localtime'),count(*) FROM messages WHERE " + clause + " GROUP BY 1", args):
                result["months"][int(month) - 1] = n
            result["hours"] = [0] * 24
            for hour, n in c.execute("SELECT strftime('%H',ts,'unixepoch','localtime'),count(*) FROM messages WHERE " + clause + " GROUP BY 1", args):
                result["hours"][int(hour)] = n
            result["calendar"] = dict(c.execute("SELECT date(ts,'unixepoch','localtime'),count(*) FROM messages WHERE " + clause + " GROUP BY 1", args))
            result["types"] = [{"kind": k, "name": KIND_NAMES.get(k, k), "count": n} for k, n in c.execute("SELECT kind,count(*) FROM messages WHERE " + clause + " GROUP BY kind ORDER BY count(*) DESC", args)]
            result["top_contacts"] = [dict(r) for r in c.execute("SELECT v.title,count(*) count FROM messages m JOIN conversations v ON v.id=m.conversation_id WHERE " + sql_filter([cid] if cid else None, start, end, "m.")[0] + " AND v.kind='direct' GROUP BY v.id ORDER BY count DESC LIMIT 8", args)]
            result['characters'] = result['sent_characters'] = 0
            for body, mine in c.execute("SELECT body,is_self FROM messages WHERE " + clause + " AND kind IN ('text','quote')", args):
                check_cancel(cancel)
                count = len(re.sub(r'\s', '', body or ''))
                result['characters'] += count
                result['sent_characters'] += count if mine else 0
            result['received_characters'] = result['characters'] - result['sent_characters']
            streak = longest = 0; previous = None
            for date in sorted(result['calendar']):
                date = dt.date.fromisoformat(date)
                streak = streak + 1 if previous and (date-previous).days == 1 else 1
                longest = max(longest, streak); previous = date
            result['longest_streak'] = longest
            result['night_messages'] = sum(result['hours'][:6])
            result['daily_average'] = round(result['total'] / max(1,result['days']), 1)
            result['weekdays'] = [0] * 7
            for date, count in result['calendar'].items(): result['weekdays'][dt.date.fromisoformat(date).weekday()] += count
            words = collections.Counter()
            try:
                import jieba
                jieba.setLogLevel(50)
                tokenizer = jieba.Tokenizer()
                stop = {"这个", "那个", "我们", "你们", "他们", "什么", "可以", "没有", "就是", "一个", "还是", "然后", "不是", "自己", "知道", "现在", "怎么", "已经", "一下", "这样", "真的", "因为", "所以", "但是", "感觉"}
                for i, (body,) in enumerate(c.execute("SELECT body FROM messages WHERE " + clause + " AND kind='text'", args)):
                    if i % 500 == 0:
                        check_cancel(cancel)
                    for word in tokenizer.cut((body or "")[:4000]):
                        if 2 <= len(word) <= 10 and word not in stop and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]+", word):
                            words[word] += 1
            except ImportError:
                pass
            result["words"] = [{"word": k, "count": n} for k, n in words.most_common(48)]
            result["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            return result


def import_legacy(directory, target, account, progress, cancel=None):
    """Import old TXT exports, preserving their explicit media-loss boundary."""
    directory = Path(directory)
    summary = (directory / "_summary.txt").read_text("utf-8-sig")
    match = re.search(r"^账号:\s*(.+)$", summary, re.M)
    if not match or match[1].strip() != account:
        raise ValueError("归档中的账号与所选账号不一致。")
    c = create_store(target)
    indexes = []
    for p in sorted(directory.glob("index_*.csv")):
        with p.open(encoding="utf-8-sig", newline="") as f:
            indexes.extend(csv.DictReader(f, delimiter="\t"))
    line_re = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*?)\[([^\]]+)\]\s?(.*)$")
    inverse = {v: k for k, v in KIND_NAMES.items()}
    inverse.update({"表情": "emoji", "链接/文件": "link", "语音通话": "call", "视频通话": "call"})
    try:
        set_meta(c, "source", "legacy_txt")
        set_meta(c, "account", account)
        set_meta(c, "limitations", "旧 TXT 未保存媒体、原始消息编号或完整 XML。同步数据库后可恢复这些内容。")
        for pos, item in enumerate(indexes):
            check_cancel(cancel)
            filename = item.get("file", "")
            path = inside(directory, directory / filename)
            if not path.is_file():
                continue
            cid = item.get("username") or "unknown:" + hashlib.md5(filename.encode()).hexdigest()
            if cid == "?":
                cid = "unknown:" + hashlib.md5(filename.encode()).hexdigest()
            title = item.get("title") or cid
            kind = "group" if cid.endswith("@chatroom") or item.get("kind") == "群聊" else ("official" if filename.startswith("biz_") else "direct")
            c.execute("INSERT OR IGNORE INTO conversations(id,title,kind) VALUES(?,?,?)", (cid, title, kind))
            c.execute("INSERT OR IGNORE INTO contacts(username,nickname,remark,alias,kind) VALUES(?,?,?,'',?)", (cid, title, "", kind))
            pending = None
            def insert():
                if pending is None:
                    return
                ts, sender, mkind, body, line_index = pending
                origin = "legacy:" + hashlib.sha256((filename + ":" + str(line_index)).encode()).hexdigest()
                c.execute("INSERT OR IGNORE INTO messages(origin,conversation_id,sender,sender_name,is_self,ts,local_type,kind,body,raw,source_db,source_table,local_id,server_id,media_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (origin, cid, sender, sender, int(sender == "我"), ts, 0, mkind, body, body, filename, "", line_index, "", "旧归档不含原文件" if mkind in {"image", "video", "audio", "emoji", "file"} else ""))
            with path.open(encoding="utf-8-sig", errors="replace") as f:
                for line_index, line in enumerate(f):
                    m = line_re.match(line.rstrip("\n"))
                    if m:
                        insert()
                        pending = [int(dt.datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").timestamp()), m[2], inverse.get(m[3], "unknown"), m[4], line_index]
                    elif pending is not None:
                        pending[3] += "\n" + line.rstrip("\n")
                insert()
            if pos % 10 == 0:
                progress("import", 5 + int(82 * (pos + 1) / max(1, len(indexes))), f"正在导入历史归档 · {pos + 1} / {len(indexes)}")
                c.commit()
        finalize(c, progress)
    finally:
        c.close()
