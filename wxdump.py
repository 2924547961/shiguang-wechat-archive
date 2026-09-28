# -*- coding: utf-8 -*-
"""
wxdump —— 微信 4.x 本地聊天数据库 通用取钥/解密/导出 工具
=============================================================
目标: 在【你自己的电脑、你自己的微信账号】上,一键完成
  1) 定位微信 4.x 数据目录(xwechat_files / db_storage)
  2) 从正在运行的 Weixin.exe 进程内存里恢复每个 SQLCipher 库的随机 AES 密钥
  3) 用 SQLCipher 逐库离线验证(真正的 PASS 才算数,绝不臆造)
  4) 用 zstd 还原 4.x 压缩容器里的文本,按会话导出为可读 UTF-8 文本

用法:
  python wxdump.py find                       # 找数据目录 / 正在运行的微信
  python wxdump.py keys --data <xwechat_files> [--acct wxid] [--pid N]
  python wxdump.py verify --raw keys.raw.json
  python wxdump.py export --verified keys.verified.json [--out DIR] [--self wxid]
  python wxdump.py all   --data <xwechat_files> [--out DIR]

依赖: pip install -r requirements.txt  (frida, sqlcipher3, zstandard)

【仅限本人设备/本人账号】,本地执行,不适用于他人电脑或远程。
运行到 key 恢复阶段需要你的微信处于登录运行状态;验钥/导出建议在退出微信后进行
(这样 SQLite 会 checkpoint,主库内容最完整)。
"""
import argparse, json, os, sys, re, glob, time, datetime, logging, hashlib, contextlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
logging.disable(logging.CRITICAL)

# sqlcipher3 每次“密钥不对/半写页”都会往 C 层 stderr(fd 2)刷 UTF-16 的
# “ERROR CORE sqlcipher_page_cipher: hmac check failed…”大段日志。验证时错钥试很多把,
# 这些行既吵又让终端乱码。工具自己的信息已足够,默认把它们静音;
# 设 WXDUMP_DEBUG=1 可关闭静音以便排查。
_NULL_FD = os.open(os.devnull, os.O_WRONLY)


@contextlib.contextmanager
def quiet_sqlcipher():
    if os.environ.get("WXDUMP_DEBUG"):
        yield
        return
    saved = os.dup(2)
    try:
        os.dup2(_NULL_FD, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


BANNER = r"""
  _      __      __            _
 | | /| / /_ __ / /  _ ___  __| |___
 | |/ |/ / // // _ \/ _ \ \/ / -_|_-<
 |__/|__/\_,_//_//_/\___/\_/\_\___/__/   WeChat 4.x local DB tool
"""
APP_NAME = "Weixin.exe"
DATA_DIR_NAME = "xwechat_files"
DB_DIR_NAME = "db_storage"

try:
    import zstandard as _zstd
    DCTX = _zstd.ZstdDecompressor()
    ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
except Exception:
    DCTX, ZSTD_MAGIC = None, b"\x28\xb5\x2f\xfd"


# ----------------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------------
def out(*a):
    print(*a, flush=True)


def file_salt_hex(path):
    """Standard SQLCipher stores its salt in the first 16 bytes, replacing SQLite's header."""
    with open(path, "rb") as fh:
        head = fh.read(48)
    if len(head) < 32:
        return None
    if head[:16] == b"SQLite format 3\x00" or not any(head[:16]):
        return None  # plaintext SQLite or an empty/uninitialized file
    return head[:16].hex()


def md5u(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# 发现: 数据目录 / 账号 / 运行中的微信
# ----------------------------------------------------------------------------
def running_wechat_pids():
    pids = []
    try:
        import psutil  # optional
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info["name"] or "").lower() == APP_NAME.lower():
                    pids.append(p.info["pid"])
            except Exception:
                pass
        return pids
    except Exception:
        pass
    try:  # 兜底: tasklist
        raw = os.popen('tasklist /FI "IMAGENAME eq %s"' % APP_NAME).read()
        for line in raw.splitlines():
            m = re.search(r"^\s*Weixin\.exe\s+(\d+)", line)
            if m:
                pids.append(int(m.group(1)))
    except Exception:
        pass
    return sorted(set(pids))


def candidate_drive_roots():
    roots = []
    for L in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        d = "%s:\\" % L
        if os.path.exists(d):
            roots.append(d)
    roots.append(os.path.join(os.path.expanduser("~"), "Documents"))
    roots.append(os.path.expanduser("~"))
    return roots


def find_data_roots(explicit=None):
    """返回所有 xwechat_files 根目录(每个含至少一个带 db_storage 的账号目录)。"""
    hits = []
    for root in (explicit or []):
        if os.path.isdir(root):
            hits.append(os.path.abspath(root))
    if not explicit:
        for base in candidate_drive_roots():
            for dirpath in (os.path.join(base, DATA_DIR_NAME),):
                if os.path.isdir(dirpath):
                    hits.append(dirpath)
            # 常见: 微信数据在 自定义盘/某目录/xwechat_files (浅层 2 级扫)
            try:
                for sub in os.listdir(base):
                    p = os.path.join(base, sub)
                    if os.path.isdir(p) and os.path.isdir(os.path.join(p, DATA_DIR_NAME)):
                        hits.append(os.path.join(p, DATA_DIR_NAME))
            except Exception:
                pass
    seen, res = set(), []
    for h in hits:
        h = os.path.normpath(h)
        if h not in seen and os.path.isdir(h):
            seen.add(h)
            res.append(h)
    return res


def accounts_of_root(root):
    """root 下每个含 db_storage 的账号子目录 -> (目录名, db_storage路径)"""
    accts = []
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return accts
    for name in names:
        dbd = os.path.join(root, name, DB_DIR_NAME)
        if os.path.isdir(dbd):
            accts.append((name, dbd))
    return accts


def served_account_dirs(root):
    """通过“运行中微信进程打开的 DB 句柄”反查它当前在服务哪个账号。

    微信只把**正在登录**的账号目录下的库保持打开(句柄形如
    <root>\\<账号目录>\\db_storage\\...\\*.db/-wal/-shm)。扫一遍 Weixin.exe
    进程的 open_files(),命中的账号目录名即是它当前服务的账号。
    返回账号目录名列表;空列表 = 无法判定(微信未运行 / 权限不足 / 库瞬时全关)。
    """
    known = [n for n, _ in accounts_of_root(root)]
    if not known:
        return []
    longest = sorted(known, key=len, reverse=True)  # 长名优先,防前缀误判
    served = []
    pids = running_wechat_pids()
    try:
        import psutil
        pids = sorted(pids, key=lambda p: psutil.Process(p).create_time())
    except Exception:
        pass
    for pid in pids:
        try:
            paths = [f.path for f in psutil.Process(pid).open_files()]
        except Exception:
            continue  # 个别进程句柄读不到,跳过;其它进程还能补上
        for acc in longest:
            if acc in served:
                continue
            marker = acc + "\\"  # 账号目录段后须跟子项,避免误中同名前缀
            if any(marker in pa.replace("/", "\\") for pa in paths):
                served.append(acc)
    return served


def cmd_find(args):
    out(BANNER)
    pids = running_wechat_pids()
    if pids:
        out("运行中的微信进程 PID:", ", ".join(map(str, pids)))
    else:
        out("未发现正在运行的微信(取钥阶段需要先登录运行微信)。")
    roots = find_data_roots([args.data] if args.data else None)
    if not roots:
        out("未找到 %s 目录。可用 --data <xwechat_files绝对路径> 指定。" % DATA_DIR_NAME)
        return 1
    for root in roots:
        accts = accounts_of_root(root)
        out("数据根目录: %s" % root)
        for name, dbd in accts:
            n = len(glob.glob(os.path.join(dbd, "**", "*.db"), recursive=True))
            out("  账号: %-32s 数据库目录: %s  (%d 个 .db)" % (name, dbd, n))
        if not accts:
            out("  (该根目录下暂无含 %s 的账号目录)" % DB_DIR_NAME)
        served = served_account_dirs(root)
        if served:
            out("  >> 微信当前正打开的账号: %s(取钥只能取到这个账号;"
                "要取别的账号请先用微信登录它)" % "、".join(served))
        elif pids:
            out("  >> 未能判定微信正打开的账号(库句柄暂不可读);取钥前请确认目标账号已登录。")
    return 0


# ----------------------------------------------------------------------------
# 阶段 1: 从运行进程内存恢复密钥 (Frida)
# ----------------------------------------------------------------------------
def list_db_files(dbdir):
    return sorted(glob.glob(os.path.join(dbdir, "**", "*.db"), recursive=True))


def dump_keys_js(salt_to_db):
    return r"""
var salt_to_db = %s;
// Run after load(): a synchronous whole-process scan can hit Frida's
// script.load() RPC timeout before Python's own deadline even starts.
setImmediate(function () {
var needle = '1000000020000000100000001000000000100000';  // codec_ctx sizes @ +0x0c..+0x20
var ranges = Process.enumerateRanges({protection:'rw-', coalesce:true})
  .concat(Process.enumerateRanges({protection:'rwx', coalesce:true}))
  .filter(function (r) { return r.size <= 0x40000000 && r.size > 0; });
function rd(p,n){try{return Array.prototype.slice.call(new Uint8Array(ptr(p).readByteArray(n)));}catch(e){return null;}}
function u64(b,o){return b[o]+b[o+1]*256+b[o+2]*65536+b[o+3]*16777216+(b[o+4]+b[o+5]*256+b[o+6]*65536+b[o+7]*16777216)*4294967296;}
function u32(b,o){return (b[o]+b[o+1]*256+b[o+2]*65536+b[o+3]*16777216)>>>0;}
function b2h(b){var s='';for(var i=0;i<b.length;i++)s+=('0'+b[i].toString(16)).slice(-2);return s;}
function asc(b){if(!b)return null;var s='';for(var i=0;i<b.length;i++){var c=b[i];s+=(c>=32&&c<127)?String.fromCharCode(c):'.';}return s;}

var codecs=[], seen={}, globalKeys={};
// WCDB Config.Cipher keeps a separate encoded key literal. Some WeChat 4.x
// builds mask codec_ctx keys, so decode the configuration independently.
// Layout reference: fanyuantaier/wechatauto-replica (Apache-2.0).
var configMap={}, configCount=0;
var configName='com.Tencent.WCDB.Config.Cipher';
var configMask=[0xd2,0xc7,0x44,0x24,0x58,0x02,0,0,0,0x48,0x89,0x44,0x24,0x50,0x48,0x8b,0x45,0,0x48,0x84,0x4c,0x24,0x48,0x48,0x89,0x44,0x25,0x40,0x48,0x58,0x4c,0x24];
function pattern(b){return b.map(function(v){return ('0'+v.toString(16)).slice(-2);}).join(' ');}
function little64(n){var b=[];for(var i=0;i<8;i++){b.push(n%%256);n=Math.floor(n/256);}return b;}
var names={},objects={};
var readable=Process.enumerateRanges({protection:'r--',coalesce:true}).concat(ranges);
var namePattern=pattern(configName.split('').map(function(c){return c.charCodeAt(0);}));
for(var ni=0;ni<readable.length;ni++){
 var nr=readable[ni];if(nr.size>0x40000000)continue;
 try{Memory.scanSync(nr.base,nr.size,namePattern).forEach(function(x){names[x.address.toString()]=true;});}catch(e){}
}
Object.keys(names).slice(0,8).forEach(function(address){
 var pair=pattern(little64(parseInt(address,16)).concat(little64(configName.length)));
 for(var ri=0;ri<ranges.length;ri++){
  try{Memory.scanSync(ranges[ri].base,ranges[ri].size,pair).forEach(function(x){
   var node=rd(x.address,0x28);if(!node)return;
   var object=u64(node,0x18);if(object<0x10000||object>0x800000000000||objects[object])return;objects[object]=true;
   var offsets=[0x88];for(var off=0x40;off<=0x140;off+=8)if(off!==0x88)offsets.push(off);
   offsets.forEach(function(off){
    var obj=rd(object+off,0x18);if(!obj)return;
    var dataPtr=u64(obj,8),length=u64(obj,16);if(dataPtr<0x10000||dataPtr>0x800000000000||length<64||length>1024)return;
    var blob=rd(dataPtr,length);if(!blob)return;
    var decoded='';for(var i=0;i<blob.length;i++)decoded+=String.fromCharCode(blob[i]^configMask[i%%32]);
    var re=/[xX]'([0-9a-fA-F]{64,192})'/g,m;
    while((m=re.exec(decoded))!==null){
     var run=m[1].toLowerCase(),kh=run.slice(0,64),salt=run.length>=96?run.slice(64,96):null;
     if(!globalKeys[kh]){globalKeys[kh]={derive_key:0,hmac32:null,source:'config_cipher'};configCount++;}
     var rel=salt?salt_to_db[salt]:null;
     if(rel){configMap[rel]=configMap[rel]||{salt:salt,keys:{}};configMap[rel].keys[kh]=globalKeys[kh];}
    }
   });
  });}catch(e){}
 }
});
// A valid Config.Cipher literal already contains the database key and salt.
// The codec sweep is substantially more expensive and only needed as fallback.
if (configCount === 0) for (var ri=0;ri<ranges.length;ri++){
  var r=ranges[ri], h;
  try { h = Memory.scanSync(r.base, r.size, needle); } catch(e){ continue; }
  for (var hi=0;hi<h.length;hi++){
    var base = h[hi].address.sub(0x0c).toString();
    if (seen[base]) continue; seen[base]=true;
    var C=parseInt(base,16);
    var hdr=rd(C,0x80); if(!hdr) continue;
    // 内存里的 codec 盐(若能读到,用于和磁盘库做映射;读不到不阻止收钥)
    var salt=null;
    var saltPtr=u64(hdr,0x48);
    if (saltPtr>0x100000000 && saltPtr<0x800000000000) salt=rd(saltPtr,16);
    if (!salt) salt=rd(C+0x48,16);
    var saltHex=salt?b2h(salt):null;
    var rel=(saltHex&&salt_to_db[saltHex])?salt_to_db[saltHex]:null;
    var read_ctx=u64(hdr,0x68), write_ctx=u64(hdr,0x70);
    var o={rel:rel, salt:saltHex, cc:base, keys:[]};
    var cs={};
    [read_ctx,write_ctx].forEach(function(cc){
      if(!cc||cs['0x'+cc.toString(16)]) return; cs['0x'+cc.toString(16)]=true;
      var ch=rd(cc,0x30); if(!ch) return;
      var derive_key=u32(ch,0), pass_sz=u32(ch,4);
      var key=rd(u64(ch,8),32); if(!key) return;
      var hmac=rd(u64(ch,16),32);
      var kh=b2h(key);
      o.keys.push({derive_key:derive_key, pass_sz:pass_sz, key:kh,
                   hmac32: hmac?b2h(hmac):null});
      if(!globalKeys[kh]) globalKeys[kh]={derive_key:derive_key,hmac32:hmac?b2h(hmac):null};
    });
    if(o.keys.length) codecs.push(o);
  }
}
var map=configMap;
codecs.forEach(function(c){ if(!c.rel) return;
  map[c.rel]=map[c.rel]||{salt:c.salt,keys:{}};
  c.keys.forEach(function(k){ map[c.rel].keys[k.key]={derive_key:k.derive_key,hmac32:k.hmac32}; }); });
send({codecs:codecs, map:map, globalKeys:globalKeys,configCount:configCount});
});
""" % json.dumps(salt_to_db)


def _scan_codecs(pid, dbs, timeout):
    """附加一个微信进程并注入扫描脚本,返回 {codecs,map,globalKeys} 或 None。"""
    try:
        import frida
    except Exception as e:
        out("frida 不可用: %s(请先 pip install -r requirements.txt)" % str(e)[:80])
        return None
    sess = None
    got = []
    deadline = time.monotonic() + max(1, int(timeout))
    try:
        dev = frida.get_local_device()
        sess = dev.attach(pid)
        script = sess.create_script(dump_keys_js(dbs))
        script.on("message", lambda msg, data: got.append(msg))
        script.load()
        while time.monotonic() < deadline and not any(
                isinstance(m.get("payload"), dict) and "map" in m["payload"]
                for m in got):
            time.sleep(0.2)
    except Exception as e:
        out("  注入 PID %d 失败: %s" % (pid, str(e)[:110]))
        try:
            if sess is not None:
                sess.detach()
        except Exception:
            pass
        return None
    if not any(isinstance(m.get("payload"), dict) and "map" in m["payload"]
               for m in got):
        out("  PID %d 内存扫描超过 %d 秒，已停止该进程。" % (pid, timeout))
    try:
        sess.detach()
    except Exception:
        pass
    for m in got:
        p = m.get("payload")
        if isinstance(p, dict) and "map" in p:
            return p
    return None


def _probe_payload(payload, dbdir, rels):
    """Accept a process only when one of its candidate keys opens this account's DB."""
    candidates = payload.get("globalKeys") or {}
    samples = _verification_order(rels)[:2]
    for rel in samples:
        path = os.path.join(dbdir, rel.replace("/", os.sep))
        precise = (payload.get('map', {}).get(rel) or {}).get('keys', {})
        for key in dict.fromkeys([*precise, *candidates]):
            if verify_one(path, key)[0]:
                return rel, key
    return None


def _verification_order(rels):
    """Probe a chat DB and a contact DB before less essential databases."""
    paths = sorted(rels)
    chat = next((r for r in paths if CHAT_DB_RE.search(r)), None)
    contact = next((r for r in paths if os.path.basename(r).lower() == 'contact.db'), None)
    first = [r for r in (chat, contact) if r]
    return first + [r for r in paths if r not in first]


def cmd_keys(args):
    out(BANNER)
    roots = find_data_roots([args.data] if args.data else None)
    if not roots:
        out("找不到数据目录,请 --data 指定 xwechat_files 所在目录。")
        return 1
    root = roots[0]
    accts = accounts_of_root(root)
    if args.acct:
        accts = [a for a in accts if args.acct.lower() in a[0].lower()]
    if not accts:
        out("该根目录下无匹配账号。可用: " + " ".join(a[0] for a in accounts_of_root(root)))
        return 1
    name, dbdir = accts[0]
    if len(accts) > 1:
        out("发现多个账号,默认取第一个: %s (可用 --acct 指定)" % name)
    # —— 账号自检: 取钥只能取到“微信当前登录”的账号;若目标账号与正在运行的
    #    微信不一致,密钥拿到也是别人的,verify 必全 FAIL。这里在扫描前就拦住。
    served = served_account_dirs(root)
    if served and name not in served:
        if not args.acct:
            # 命令行没指定账号而默认选的恰好不对: 直接自动对齐到正在登录的账号。
            sv_in_root = [s for s in served if os.path.isdir(os.path.join(root, s, DB_DIR_NAME))]
            if sv_in_root and name not in sv_in_root:
                out("未指定账号;微信当前登录并打开的账号是 %s —— 自动改用该账号。"
                    % sv_in_root[0])
                name, dbdir = sv_in_root[0], os.path.join(root, sv_in_root[0], DB_DIR_NAME)
                accts = [(name, dbdir)]
        elif not getattr(args, "force", False):
            out("")
            out("!! 账号不匹配: 你要取的是 【%s】,但微信当前登录并服务的是 【%s】。"
                % (name, "、".join(served)))
            out("!! 微信只把“正在登录的账号”的库密钥加载进内存;%s 的库现在没有密钥可取,"
                % name)
            out("!! 用 %s 的密钥去验 %s 的库必然全部 FAIL —— 那不是工具坏了,是账号选错了。"
                % ("/".join(served), name))
            out("!!")
            out("!! 两种做法任选:")
            out("!!   1) 你其实想要 %s 的数据  ->  换 --acct %s 重跑(或 GUI 账号下拉选它)。"
                % (served[0], served[0]))
            out("!!   2) 你确实要 %s 的数据  ->  先在微信里登录 %s,再重跑取钥。"
                % (name, name))
            out("!! 若你明白风险、坚持用正在登录账号的密钥继续: 加 --force 跳过本检查。")
            out("")
            return 1
    # 候选进程: 手动 --pid 则只用它;否则微信多进程按启动先后排(主进程最早,最可能
    # 持有 DB codec),逐一尝试,直到某个进程真的扫到 codec。
    if args.pid:
        cand_pids = [args.pid]
    else:
        cand_pids = running_wechat_pids()
        if len(cand_pids) > 1:
            try:
                import psutil
                cand_pids = sorted(cand_pids, key=lambda p: (
                    any(arg.startswith('--type=') for arg in psutil.Process(p).cmdline()),
                    psutil.Process(p).create_time()))
            except Exception:
                pass
    if not cand_pids:
        out("未找到正在运行的微信,请先登录运行微信后再取钥。")
        return 1
    if len(cand_pids) > 1 and not args.pid:
        out("多个微信进程,优先主进程并逐一验证: %s" % cand_pids)
    pid = cand_pids[0]
    rels = sorted(os.path.relpath(f, dbdir).replace("\\", "/")
                  for f in list_db_files(dbdir))
    if not rels:
        out("该账号 db_storage 下没有任何 .db 文件?"); return 1
    dbs = {}
    for f in list_db_files(dbdir):
        s = file_salt_hex(f)
        if s:
            dbs[s] = os.path.relpath(f, dbdir).replace("\\", "/")
    out("账号 %s | PID %d | 磁盘库 %d 个(已读取库头盐 %d 个)"
        % (name, pid, len(rels), len(dbs)))
    if not dbs:
        out("未识别到标准 SQLCipher 库头盐，将通过数据库读取验证归属。")
    payload = None
    pid_used = None
    codec_seen = False
    for p in cand_pids:
        possible = _scan_codecs(p, dbs, getattr(args, "timeout", 40))
        if possible is not None and possible.get('globalKeys'):
            codec_seen = True
            matched = _probe_payload(possible, dbdir, rels)
            if not matched:
                out("PID %d 扫描到密钥候选，但未通过当前账号数据库验证，继续检查其它微信进程。" % p)
                continue
            payload = possible
            pid_used = p
            out("PID %d 的密钥已通过 %s 验证，使用该进程。" % (p, matched[0]))
            break
        else:
            out("PID %d 未找到可用密钥候选。" % p)
    if payload is None:
        if codec_seen:
            out("所有微信进程均未找到能打开当前账号数据库的密钥。请核对登录账号；若账号正确，当前微信版本的取钥或数据库结构可能已改变。")
        else:
            out("未能取得可用密钥候选。请确认微信已登录且账号正在使用数据库；若扫描超时，可在命令行提高 --timeout。")
        return 1
    mapping = payload["map"]
    globalk = payload.get("globalKeys") or {}
    nctx = len(payload.get("codecs") or [])
    out("WCDB 配置候选 %d 把" % payload.get("configCount", 0))
    out("扫描到 codec_ctx=%d 个,内存不同密钥 %d 把"
        % (nctx, len(globalk)))
    # 每个磁盘库的候选密钥 = 该库盐命中的精确密钥(若有) + 全部内存密钥兜底。
    # 兜底可让“库头无明文盐 / 盐扫描不到”的版本仍靠 sqlcipher3 真 PASS 归属到库。
    cand = {}
    for rel in rels:
        e = mapping.get(rel) or {}
        precise = e.get("keys", {}) if e else {}
        salt = e.get("salt") if e else None
        outkeys, seen = [], set()
        for k, v in precise.items():
            outkeys.append({"key": k, "derive_key": v.get("derive_key"),
                            "hmac32": v.get("hmac32")})
            seen.add(k)
        for k, v in globalk.items():
            if k in seen:
                continue
            outkeys.append({"key": k, "derive_key": v.get("derive_key"),
                            "hmac32": v.get("hmac32")})
            seen.add(k)
        if outkeys:
            cand[rel] = {"salt": salt, "keys": outkeys}
    if not cand:
        out("内存里没扫到任何可用密钥(codec_ctx 结构可能随微信版本变化,如实报告失败)。")
        return 1
    n_fallback = sum(1 for rel, e in cand.items() if e["salt"] is None)
    raw = {
        "tool": "wxdump",
        "account": name,
        "dbdir": dbdir,
        "source": "live memory codec_ctx (Weixin.exe PID %d)" % pid_used,
        "recovered_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "candidates": cand,
    }
    if served:
        raw["served_accounts"] = served
        if name not in served:
            raw["account_mismatch"] = True
            if getattr(args, "force", False):
                raw["forced_override"] = True  # 审计: 明知不一致仍强制继续
    out("已登记 %d 个磁盘库的候选密钥(%d 个靠全局内存密钥兜底)"
        % (len(cand), n_fallback))
    outpath = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "keys_%s.raw.json" % name)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    out("候选密钥已写入: %s" % outpath)
    out("下一步: python wxdump.py verify --raw %s" % outpath)
    args.out = outpath  # 回填实际写入路径,供 cmd_all 衔接
    return 0


# ----------------------------------------------------------------------------
# 阶段 2: 逐库 SQLCipher 离线验证 (真正的 PASS)
# ----------------------------------------------------------------------------
def open_sqlcipher(path, key):
    import sqlcipher3 as sqlite3
    with quiet_sqlcipher():
        from pathlib import Path
        con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            con.execute("PRAGMA key = \"x'%s'\"" % key)
            n = con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        except Exception:
            con.close()
            raise
    return con, n


def verify_one(path, key, want_tables=False):
    """返回 (True, 信息) 或 (False, 错误)。只接受真 PASS。"""
    try:
        con, n = open_sqlcipher(path, key)
        try:
            if want_tables:
                tabs = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
                return True, {"tables": len(tabs)}
            return True, {"sqlite_master_rows": n}
        finally:
            con.close()
    except Exception as e:
        return False, str(e)[:120]


def verify_one_retry(path, key, tries=4):
    """离线验证一把密钥;若正赶上微信 checkpoint 读到半写页(瞬时失败),自动重试。
    只有重试耗尽仍失败,才算这个(库,密钥)真失败。"""
    last = (False, "?")
    for i in range(tries):
        last = verify_one(path, key)
        if last[0]:
            return last
        time.sleep(0.5)
    return last


CHAT_DB_RE = re.compile(r"(?:biz_)?message_\d+\.db$", re.I)
WAL_SUFFIXES = ("", "-wal", "-shm")


def _wipe_dst(base_dst):
    for s in WAL_SUFFIXES:
        p = base_dst + s
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def snapshot_one(path, key, snapmain, tries=14):
    """把一个(可能正被运行中微信使用的)库做成**一致快照**。

    不能直接文件拷贝: 微信在 WAL 模式下会把 checkpoint 直接写进主库,若拷贝撞上
    checkpoint 写到一半,主库页 1 就是坏的(hmac 校验失败)——重试也会一直撞。
    正确做法是用 SQLite 在线备份 API: 在活库上开一个读事务,由 pager 协调 WAL,
    逐页复制到(同样用该密钥加密的)新库,永远得到一致快照。
    返回 (snapmain, sqlite_master行数) 或 (None, None)。"""
    import sqlcipher3 as sqlite3
    base = os.path.basename(path)
    os.makedirs(os.path.dirname(snapmain), exist_ok=True)  # 目的目录必须先建好
    _wipe_dst(snapmain)
    for i in range(tries):
        src = dst = None
        try:
            with quiet_sqlcipher():
                src = sqlite3.connect(path)
                src.execute("PRAGMA key = \"x'%s'\"" % key)
                src.execute("SELECT count(*) FROM sqlite_master").fetchone()  # 就绪,开读快照
                _wipe_dst(snapmain)
                dst = sqlite3.connect(snapmain)
                dst.execute("PRAGMA key = \"x'%s'\"" % key)
                src.backup(dst)
                n = dst.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
                dst.close(); dst = None
                src.close(); src = None
            return snapmain
        except Exception as e:
            try:
                if dst is not None:
                    dst.close()
            except Exception:
                pass
            try:
                if src is not None:
                    src.close()
            except Exception:
                pass
            out("  快照 %s 失败(第 %d 次): %s —— 重试" % (base, i + 1, str(e)[:80]))
            if os.environ.get("WXDUMP_DEBUG") and i == tries - 2:
                import traceback
                traceback.print_exc()
            time.sleep(min(1.0 + 1.6 * i, 7))  # 指数退避;微信写盘期可能持续数秒
    return None


def cmd_verify(args):
    out(BANNER)
    raw = json.load(open(args.raw, encoding="utf-8"))
    dbdir = raw.get("dbdir")
    cand = raw.get("candidates", {})
    out("验证账号 %s (%d 个库的候选密钥)" % (raw.get("account"), len(cand)))
    passed, failed, rows = 0, 0, []
    # Probe a chat database early. On new WeChat builds every DB may reject the
    # same global candidates; avoid repeating a full key search for every file.
    ordered = [(rel, cand[rel]) for rel in _verification_order(cand)]
    failed_fingerprints = set()
    for rel, e in ordered:
        path = os.path.join(dbdir, rel.replace("/", os.sep))
        if not os.path.exists(path):
            out("%-34s 文件不存在: %s" % (rel, path)); failed += 1; continue
        keys = [k["key"] for k in e["keys"]]
        ok, info = False, None
        # First pass: each candidate once. A failed key is normally simply wrong.
        for key in keys:
            ok, info = verify_one(path, key)
            if ok:
                break
        if not ok:
            # Retry the whole candidate set once after a short pause for a
            # possible live checkpoint. Sleeping after every wrong key made
            # 23 candidates cost 35 seconds per database.
            time.sleep(0.5)
            for key in keys:
                ok, info = verify_one(path, key)
                if ok:
                    break
        size_mb = os.path.getsize(path) / 1e6
        if ok:
            passed += 1
            failed_fingerprints.clear()
            # 存纯 hex 密钥字符串(导出/PRAGMA 按字符串消费;不要把整条 entry 塞进来)
            rows.append({"rel": rel, "salt": e.get("salt"), "key": key})
            out("%-34s %6.1fMB  PASS  %s" % (rel, size_mb, info))
        else:
            failed += 1
            out("%-34s %6.1fMB  FAIL  %s" % (rel, size_mb, (info or "")[:90]))
            fingerprint = tuple(sorted(keys))
            if not passed and fingerprint in failed_fingerprints:
                out("连续数据库使用同一批候选密钥均未通过，已停止重复验证。")
                out("请确认当前微信登录的就是该账号；若账号一致，可能是当前微信版本的数据库结构或取钥规则已变化。")
                break
            failed_fingerprints.add(fingerprint)
    out("\n通过=%d 失败=%d" % (passed, failed))
    if not rows:
        out("没有库验证通过 —— 不产出任何密钥(可能是微信版本结构差异)。")
        return 1
    ver = {"tool": "wxdump", "account": raw.get("account"), "dbdir": dbdir,
           "verified_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "verification": "sqlcipher3 offline: PRAGMA key = x'<key>' -> sqlite_master readable",
           "keys": {r["rel"]: {"salt": r["salt"], "key": r["key"]} for r in rows}}
    outpath = args.out or re.sub(r"\.raw\.json$", ".verified.json", args.raw)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(ver, f, ensure_ascii=False, indent=1)
    out("已通过验证的密钥清单写入: %s" % outpath)
    return 0


# ----------------------------------------------------------------------------
# 阶段 3: 导出聊天记录 (zstd 容器还原 + 按会话写文本)
# ----------------------------------------------------------------------------
def sanitize(s):
    if s is None:
        return ""
    o2 = []
    for ch in s:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            continue
        if o < 0x20 and ch not in "\n\t\r":
            continue
        o2.append(ch)
    return "".join(o2)


def strip_pua(s):
    return re.sub(r"[\ue000-\uf8ff\U000e0000-\U000e01ef]", "", s)


def deblob(b):
    if isinstance(b, (bytes, bytearray)) and b[:4] == ZSTD_MAGIC and DCTX is not None:
        try:
            return DCTX.decompress(b)
        except Exception:
            try:
                return b"".join(DCTX.decompressobj().decompress(b))
            except Exception:
                return bytes(b)
    return bytes(b) if b is not None else b""


USERNAME_RE = re.compile(r"^[0-9A-Za-z_\-@]{1,80}:(\n|$)")


def strip_sender_bytes(b, sid, n2i, self_id):
    if sid == self_id:
        return b
    name = n2i.get(sid)
    if name:
        enc = name.encode("utf-8")
        if b[:len(enc) + 1] == enc + b":":
            b = b[len(enc) + 1:]
            if b[:1] == b"\n":
                b = b[1:]
            return b
    nl = b.find(b"\n")
    if 0 < nl <= 96:
        line = b[:nl].decode("utf-8", "ignore")
        if USERNAME_RE.match(line) and len(b) > nl + 1:
            return b[nl + 1:]
    return b


def strip_sender_str(s, sid, n2i, self_id):
    if s is None:
        return s
    if sid == self_id:
        return s
    name = n2i.get(sid)
    if name:
        pre = name + ":"
        if s.startswith(pre):
            s = s[len(pre):]
            if s.startswith("\n"):
                s = s[1:]
            return s
    m = re.match(r"^[0-9A-Za-z_\-@]{1,80}:\n", s)
    if m and len(s) > m.end():
        s = s[m.end():]
    return s


TYPE_LABEL = {1: "[文本]", 3: "[图片]", 34: "[语音]", 36: "[?]", 42: "[名片]",
              43: "[视频]", 47: "[表情]", 48: "[位置]", 49: "[链接/文件]",
              50: "[语音通话]", 51: "[视频通话]", 52: "[?]", 10000: "[系统消息]"}


def typelabel(local_type):
    mt = local_type & 0xFFFFFFFF if local_type and local_type > 0xFFFFFFFF else local_type
    if mt is None:
        mt = local_type
    return TYPE_LABEL.get(mt, "[类型%d]" % mt), mt


def body_of(content, sid, n2i, self_id):
    """type=1 文本正文(可读、去掉发送者前缀)。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return sanitize(strip_pua(strip_sender_str(content, sid, n2i, self_id))).strip("\n")
    b = strip_sender_bytes(deblob(content), sid, n2i, self_id)
    txt = strip_pua(sanitize(b.decode("utf-8", "ignore"))).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", txt) if txt else ""


def sysmsg_render(content, sid, n2i, self_id):
    """type=10000 系统消息转可读。"""
    if content is None:
        return ""
    if isinstance(content, bytes):
        txt = strip_sender_bytes(deblob(content), sid, n2i, self_id).decode("utf-8", "ignore")
    else:
        txt = strip_sender_str(content, sid, n2i, self_id)
    m = re.search(r"<content>(.*?)</content>", txt, re.S)
    if m and m.group(1).strip():
        return re.sub(r"\s+", " ", sanitize(m.group(1))).strip()[:300]
    tmpl_m = re.search(r"<template><!\[CDATA\[(.*?)\]\]></template>", txt, re.S)
    if not tmpl_m:
        t = re.search(r'<sysmsg type="([^"]+)"', txt)
        return ("[%s]" % t.group(1)) if t else ""

    def link_value(name):
        lm = re.search(r'<link name="%s".*?</link>' % re.escape(name), txt, re.S)
        if not lm:
            return None
        seg = lm.group(0)
        pm = re.search(r"<plain><!\[CDATA\[(.*?)\]\]></plain>", seg, re.S)
        if pm and pm.group(1).strip():
            names = pm.group(1).strip()
        else:
            nicks = re.findall(r"<nickname><!\[CDATA\[(.*?)\]\]></nickname>", seg, re.S)
            names = "、".join(nicks)
        parts = [x for x in names.split("、") if x.strip()]
        if len(parts) > 40:
            names = "、".join(parts[:40]) + "…等%d人" % len(parts)
        return names

    outt = re.sub(r"\$([0-9A-Za-z_]+)\$",
                  lambda mo: link_value(mo.group(1)) if link_value(mo.group(1)) is not None else mo.group(0),
                  tmpl_m.group(1))
    return re.sub(r"\s+", " ", outt).strip()[:300]


def summary_of(content, sid, n2i, self_id):
    """非文本媒体行的短说明(标题/链接卡/位置)。"""
    if content is None or isinstance(content, str):
        return ""
    b = strip_sender_bytes(deblob(content), sid, n2i, self_id)
    txt = b.decode("utf-8", "ignore")

    def plain(v):
        return re.sub(r"<!\[CDATA\[|\]\]>", "", sanitize(v)).strip()
    t = re.search(r"<title>(.*?)</title>", txt, re.S)
    if t:
        d = re.search(r"<des>(.*?)</des>", txt, re.S)
        o2 = plain(t.group(1))
        if d:
            dv = plain(d.group(1))
            if dv:
                o2 += " — " + dv
        return o2[:300]
    m = re.search(r"<location .*?label=\"([^\"]*)\"", txt)
    if m:
        return sanitize(m.group(1))[:300]
    return ""


def one_line(s):
    return re.sub(r"[\r\n\t]+", " ", s) if s else s


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s).strip(" .")
    return s[:80] or "untitled"


def fmt_time(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def export_one_db(con, dbfile, n2i, self_id, disp, rooms, outdir, index_rows):
    """导出单个含 Msg_ 表的库 -> 返回导出会话数。"""
    tabs = [t[0] for t in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
    ownmap = {md5u(u): u for u in set(n2i.values()) if u}
    tag = os.path.basename(dbfile).replace(".db", "")
    manifest = []
    for idx, tb in enumerate(tabs, 1):
        h = tb[4:]
        user = ownmap.get(h)
        title = (disp.get(user) if user else None) or user or "(未知)"
        n = con.execute('SELECT count(*) FROM "%s"' % tb).fetchone()[0]
        if not n:
            continue
        is_room = bool(user and (user in rooms or user.endswith("@chatroom")))
        fname = "%s_%04d_%s.txt" % (tag, idx, safe_name(title))
        f = open(os.path.join(outdir, fname), "w", encoding="utf-8")
        f.write("=" * 70 + "\n")
        f.write("文件        : %s\n" % os.path.basename(dbfile))
        f.write("会话        : %s\n" % one_line(title))
        f.write("对象        : %s\n" % (user or "?"))
        f.write("类型        : %s\n" % ("群聊" if is_room else "单聊"))
        f.write("消息数      : %d\n" % n)
        f.write("=" * 70 + "\n\n")
        last_local = 0
        while True:
            batch = con.execute(
                'SELECT local_id, local_type, create_time, real_sender_id, message_content '
                'FROM "%s" WHERE local_id > ? ORDER BY local_id LIMIT 4000' % tb,
                (last_local,)).fetchall()
            if not batch:
                break
            for lid, lt, ts, sid, content in batch:
                label, mt = typelabel(lt)
                who = ("我" if sid == self_id
                       else (disp.get(n2i.get(sid, ""), "") or n2i.get(sid, "") or ""))
                if mt == 1:
                    body = body_of(content, sid, n2i, self_id)
                elif mt == 10000:
                    body = sysmsg_render(content, sid, n2i, self_id)
                else:
                    body = summary_of(content, sid, n2i, self_id)
                if body:
                    f.write("[%s] %s%s %s\n" % (fmt_time(ts), who, label, body))
                else:
                    f.write("[%s] %s%s\n" % (fmt_time(ts), who, label))
                last_local = lid
        f.close()
        manifest.append((idx, one_line(title), user, n, "群聊" if is_room else "单聊", fname))
        out("   %-34s n=%-6d %s" % (title, n, "群聊" if is_room else "单聊"))
    if manifest:
        with open(os.path.join(outdir, "index_%s.csv" % tag), "w", encoding="utf-8") as mf:
            mf.write("idx\ttitle\tusername\trows\tkind\tfile\n")
            for idx, title, user, n, kind, fn in manifest:
                mf.write("%d\t%s\t%s\t%d\t%s\t%s\n" % (idx, title, user or "?", n, kind, fn))
        index_rows.append((tag, manifest))
    return len(manifest)


def resolve_self(n2i, account, args_self):
    """自己 wxid: 命令行 > 账号目录前缀 > 兜底。返回 self rowid 或 None。"""
    if args_self:
        cand = [args_self]
    else:
        # 账号目录形如 wxid_<id>_<suffix> (xwechat 4.x),去掉尾部 4 位 hex 后缀即自己的 wxid
        base = re.sub(r"_([0-9a-f]{4})$", "", account, flags=re.I)
        cand = []
        for c in (base, account):
            if c.startswith("wxid_") or "@" in c:
                cand.append(c)
    for c in dict.fromkeys(cand):
        for rid, u in n2i.items():
            if u == c:
                return rid
    return None


def cmd_export(args):
    out(BANNER)
    ver = json.load(open(args.verified, encoding="utf-8"))
    dbdir = ver.get("dbdir")
    keys = ver.get("keys", {})
    account = ver.get("account", "wechat")
    outdir = args.out or os.path.join(os.path.dirname(os.path.abspath(args.verified)),
                                      "export_" + re.sub(r"[^\w.-]", "_", account))
    os.makedirs(outdir, exist_ok=True)
    snapdir = os.path.join(outdir, "_snapshot")
    live = bool(running_wechat_pids())
    if not args.no_snap:
        if live:
            out("注意: 检测到微信正在运行。运行中微信的数据库处于 WAL 模式、会被持续改写,")
            out("      直接读活文件可能撞上 checkpoint 读到半写页。因此导出前会对每个库做")
            out("      SQLite 在线备份式的**一致快照**(在活库上开读事务、由 pager 协调 WAL),")
            out("      导出读快照 —— 全程不干扰微信。若某库正赶上微信写盘高峰而反复失败,")
            out("      请关闭微信后重跑(关闭后文件 checkpoint 完整、导出最全也最稳)。\n")
        else:
            out("微信未运行,数据库文件稳定,将对每个库做一致快照后导出(也可 --no-snap 直读)。\n")
    # 决定要用的库: 联系人库(显示名/群) + 真正的聊天分片 (message_N / biz_message_N)
    need = {}
    for rel, k in keys.items():
        base = os.path.basename(rel).lower()
        if base == "contact.db":
            need[rel] = ("contact", k)
        elif CHAT_DB_RE.search(base):
            need[rel] = ("chat", k)
    if not any(v[0] == "chat" for v in need.values()):
        out("通过验证的密钥清单里没有找到聊天分片(message_N.db / biz_message_N.db)。")
        out("可用 --verified 指向正确的 keys_*.verified.json,或重新 verify。")
        return 1
    # 快照
    snaps = {}
    ok = True
    for rel, (kind, k) in sorted(need.items()):
        p = os.path.join(dbdir, rel.replace("/", os.sep))
        if not os.path.exists(p):
            out("跳过(文件不存在): %s" % rel); continue
        if args.no_snap:
            snaps[rel] = (kind, k["key"], p)
            out("%-10s %s" % ("(直读)", rel))
            continue
        out("快照 %-10s %s" % ("(聊天)" if kind == "chat" else "(联系人)", rel))
        sp = os.path.join(snapdir, rel.replace("/", os.sep))
        snap = snapshot_one(p, k["key"], sp)
        if snap is None:
            out("!! 快照 %s 反复失败 —— 微信可能正忙于写盘。可稍后重试,或关闭微信后再导。" % rel)
            ok = False
            continue
        snaps[rel] = (kind, k["key"], snap)
    if not ok:
        out("存在未能获得稳定快照的库,导出中止(避免输出不完整/撕页数据)。")
        out("可先关闭微信让它把 WAL checkpoint 进主库,再运行 export;或加 --no-snap 直读活库(不推荐)。")
        return 1
    # 联系人显示名 & 群集合 (来自快照)
    disp, rooms = {}, set()
    for rel, (kind, key, path) in snaps.items():
        if kind != "contact":
            continue
        try:
            con, _ = open_sqlcipher(path, key)
            for u, remark, nick in con.execute(
                    "SELECT username, remark, nick_name FROM contact"):
                d = sanitize(strip_pua(remark or nick or u)) or u
                disp[u] = d
            for (u,) in con.execute("SELECT username FROM chat_room"):
                rooms.add(u)
            con.close()
        except Exception as e:
            out("警告: 联系人库打不开(仍会导出,显示为 wxid): %s" % str(e)[:80])
    out("联系人显示名=%d 群=%d\n" % (len(disp), len(rooms)))
    # 导出每个聊天快照
    index_rows = []
    total_files = 0
    for rel, (kind, key, path) in sorted(snaps.items()):
        if kind != "chat":
            continue
        try:
            con, _ = open_sqlcipher(path, key)
        except Exception as e:
            out("打开快照失败: %s  %s" % (rel, str(e)[:80])); continue
        n2i = {r: u for r, u in con.execute("SELECT rowid, user_name FROM Name2Id")}
        self_id = resolve_self(n2i, account, args.self)
        if self_id is None:
            out("警告: 未能在此库确定自己的 wxid(账号目录名 %s)。可用 --self wxid 指定。" % account)
        out("导出消息库: %s" % rel)
        start = time.time()
        nf = export_one_db(con, rel, n2i, self_id, disp, rooms, outdir, index_rows)
        total_files += nf
        con.close()
        out("   (%d 个会话, 用时 %.1fs)\n" % (nf, time.time() - start))
    # 汇总
    with open(os.path.join(outdir, "_summary.txt"), "w", encoding="utf-8") as f:
        f.write("wxchat 导出汇总\n\n")
        f.write("账号: %s\n数据库目录: %s\n" % (account, dbdir))
        for tag, manifest in index_rows:
            f.write("%s: %d 个会话\n" % (tag, len(manifest)))
    out("完成。导出 %d 个会话到: %s" % (total_files, outdir))
    if not args.keep_snap and os.path.isdir(snapdir):
        import shutil
        shutil.rmtree(snapdir, ignore_errors=True)
        out("(稳定快照目录已清理;如需保留请加 --keep-snap)")
    return 0


def cmd_all(args):
    out(BANNER)
    # 三个阶段的 --out 语义不同,必须按阶段隔离:
    #   keys/verify 的 --out 是“密钥 json 输出文件”;export 的 --out 是“导出目录”。
    # 一键流程里 keys/verify 都用默认文件名,export 用用户的 --out 目录。
    save_out = args.out
    args.out = None              # keys/verify 各自用默认 .raw/.verified.json
    rc = cmd_keys(args)
    if rc:
        args.out = save_out
        return rc
    rawpath = args.out           # cmd_keys 成功时会回填实际写入的 raw 路径
    args.raw = rawpath
    args.out = None              # keys 已把 out 回填成 raw 路径,须清掉,verify 才写默认 .verified.json
    rc = cmd_verify(args)
    if rc:
        args.out = save_out
        return rc
    args.out = save_out          # 恢复用户的导出目录
    args.verified = re.sub(r"\.raw\.json$", ".verified.json", rawpath)
    return cmd_export(args)


def cmd_prepare(args):
    """Recover and validate in one helper process (one onefile extraction)."""
    import sqlcipher3
    con = sqlcipher3.connect(':memory:')
    try:
        version = con.execute('PRAGMA cipher_version').fetchone()
        if not version:
            out('SQLCipher 驱动未正确加载，已停止取钥。')
            return 1
        out('SQLCipher ' + str(version[0]))
    finally:
        con.close()
    key_args = argparse.Namespace(**vars(args))
    key_args.out = args.raw
    rc = cmd_keys(key_args)
    if rc:
        return rc
    out('正在验证数据库')
    return cmd_verify(argparse.Namespace(raw=args.raw, out=args.out))


def main():
    ap = argparse.ArgumentParser(description="微信 4.x 本地数据库取钥/解密/导出(仅限本人电脑)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("find", help="发现数据目录与运行中的微信")
    p.add_argument("--data", help="xwechat_files 目录(可选)")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("keys", help="从运行中的微信恢复密钥")
    p.add_argument("--data", help="xwechat_files 目录(可选,可自动查找)")
    p.add_argument("--acct", help="账号名过滤(如 wxid_xxx)")
    p.add_argument("--pid", type=int, help="微信进程 PID(可选,自动找)")
    p.add_argument("--out", help="原始密钥 json 输出路径")
    p.add_argument("--timeout", type=int, default=40, help="Frida 扫描秒数")
    p.add_argument("--force", action="store_true",
                   help="目标账号与微信当前登录账号不一致时仍继续(不推荐)")
    p.set_defaults(fn=cmd_keys)

    p = sub.add_parser('prepare', help='一次完成当前账号取钥与验证')
    p.add_argument('--data')
    p.add_argument('--acct')
    p.add_argument('--pid', type=int)
    p.add_argument('--timeout', type=int, default=60)
    p.add_argument('--force', action='store_true')
    p.add_argument('--raw', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("verify", help="离线逐库 SQLCipher 验证密钥")
    p.add_argument("--raw", required=True, help="keys_*.raw.json")
    p.add_argument("--out", help="验证通过清单输出路径")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("export", help="导出聊天记录")
    p.add_argument("--verified", required=True, help="keys_*.verified.json")
    p.add_argument("--out", help="导出目录")
    p.add_argument("--self", help="自己的 wxid(一般自动,必要时指定)")
    p.add_argument("--no-snap", action="store_true",
                   help="不做稳定快照、直接读活库(不推荐;微信运行时可能读到半写页)")
    p.add_argument("--keep-snap", action="store_true", help="导出后保留稳定快照目录")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("all", help="keys -> verify -> export 一键")
    p.add_argument("--data", help="xwechat_files 目录")
    p.add_argument("--acct", help="账号名过滤")
    p.add_argument("--pid", type=int)
    p.add_argument("--timeout", type=int, default=40, help="Frida 扫描秒数")
    p.add_argument("--force", action="store_true",
                   help="目标账号与微信当前登录账号不一致时仍继续(不推荐)")
    p.add_argument("--out", help="输出目录")
    p.add_argument("--self", help="自己的 wxid")
    p.add_argument("--no-snap", action="store_true")
    p.add_argument("--keep-snap", action="store_true")
    p.set_defaults(fn=cmd_all)

    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
