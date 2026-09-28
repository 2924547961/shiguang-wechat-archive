from __future__ import annotations
import base64
import datetime as dt
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from .common import BASE, STATE, STATIC, Cancelled, atomic_json, clean_error, inside, read_json
from .discovery import discover
from .exporter import export_data, export_report
from .emoji import restore_emojis
from .moments import sync_moments, list_moments, export_moments, media_candidates, bind_media, restore_moments_media, repair_archived_moments
from .insights import InsightService
from .importer import sync_account
from .store import Archive, import_legacy


class Application:
    def __init__(self, state=STATE, base=BASE, demo=False):
        self.state, self.base, self.demo = Path(state), Path(base), demo
        self.state.mkdir(parents=True, exist_ok=True)
        self.settings = dict(output_dir=str(self.state / "exports"), data_root="", include_media=True, force_keys=False, auto_sync=True, sync_interval=0.5, auto_emoji=False)
        self.settings.update(read_json(self.state / "settings.json"))
        # Older builds stored the former default of 15 seconds. The live path
        # now performs a light message-only pass, so check once per second.
        if self.settings.get('sync_interval') == 15:
            self.settings['sync_interval'] = 1
        self.lock = threading.RLock()
        self.jobs, self.cancel_events, self.accounts = {}, {}, []
        history = read_json(self.state / 'history.json')
        if isinstance(history, list):
            self.jobs = {j['id']: j for j in history if isinstance(j, dict) and j.get('id') and not j.get('automatic') and j.get('status') in {'done', 'error', 'cancelled'}}
        self.discovery = {"active_count": 0, "process_count": 0, "access_denied": 0}
        self.selected, self.share, self.open_callback = None, None, None
        self.scan()
        self.watcher_stop = threading.Event()
        self.live_status = {'enabled': False, 'interval': 1, 'last_check': None, 'error': '', 'moments_revision': 0, 'chat_revision': 0}
        self._moments_media_lock = threading.Lock()
        self._moments_media_pending = []
        self._moments_media_thread = None
        self.emoji_attempted = {}
        from .automation import Automation
        self.automation = Automation(self)
        from .voice import VoiceSender
        self.voice = VoiceSender(self)
        from .harness import Harness
        from .personal import PersonalService
        self.harness = Harness(self.state)
        self.personal = PersonalService(self)
        self.harness.register('personal.memory', self.personal.run_task)
        self.harness.register('personal.coach', self.personal.run_task)
        self.harness.register('voice.send', self.voice.send, tools=('weixin.voice',))
        self.harness.register('mirror.analyze', lambda ctx, payload: InsightService(self)._run_analysis_task(ctx, payload))
        self.harness.register('mirror.skill', lambda ctx, payload: InsightService(self)._run_skill_task(ctx, payload))
        from .articles import run as article_run
        self.harness.register('article.call', article_run, tools=('article.mcp',))
        from backend.articles.service import ArticleService
        self.article_service = ArticleService(self)
        self.harness.register('article.download', self.article_service.run, tools=('article.download',))
        self.harness.register('article.scan', self.article_service.scan, tools=())
        from .revoke_disk import DiskPatcher
        self.revoke_disk = DiskPatcher(self.state)
        from .multi_instance import launch as launch_weixin
        self.harness.register('weixin.launch', lambda ctx, payload: launch_weixin(ctx, payload, self.revoke_disk),
                              tools=('weixin.launch',))
        if not self.demo:
            self.revoke_disk.start()

    def start_watcher(self):
        if self.demo or hasattr(self, 'watcher'): return
        def watch():
            from .incremental import signature

            seen, sns_seen, in_flight = {}, {}, {}
            last_scan = 0
            scan_thread = None
            while not self.watcher_stop.wait(0.10):
                self.live_status['enabled'] = bool(self.settings.get('auto_sync'))
                if not self.settings.get('auto_sync'): continue
                interval = max(0.10, min(300, float(self.settings.get('sync_interval', 0.5))))
                if interval == 1:
                    interval = 0.5
                self.live_status['interval'] = interval
                if time.time() - (self.live_status['last_check'] or 0) < interval: continue
                self.live_status['last_check'] = time.time()
                try:
                    for kind, item in list(in_flight.items()):
                        job, account_id, stamp = item
                        if job['status'] == 'running': continue
                        del in_flight[kind]
                        if job['status'] != 'done':
                            if job['status'] != 'cancelled':
                                self.live_status['error'] = job['message']
                            continue
                        self.live_status['error'] = ''
                        if kind == 'chat':
                            seen[account_id] = stamp
                            if (job.get('result') or {}).get('new_messages'):
                                self.live_status['chat_revision'] += 1
                                self.automation.wake.set()
                        else:
                            sns_seen[account_id] = stamp
                            if (job.get('result') or {}).get('updated'):
                                self.live_status['moments_revision'] += 1
                                self.automation.wake.set()
                                self._queue_moments_media(account_id, (job.get('result') or {}).get('updated_ids', [])[:50])
                    if time.time() - last_scan > 60 and (scan_thread is None or not scan_thread.is_alive()):
                        last_scan = time.time()
                        scan_thread = threading.Thread(target=self.scan, name='account-discovery', daemon=True)
                        scan_thread.start()
                    account = self.account()
                    if not account.get('active') or not account.get('has_archive'): continue
                    files = sorted(Path(account['dbdir']).rglob('*.db'))
                    stamp = tuple((str(p), signature(p)) for p in files if p.name in {'contact.db', 'head_image.db', 'hardlink.db'} or re.fullmatch(r'(?:biz_)?message_\d+\.db', p.name))
                    if seen.get(account['id']) != stamp and 'chat' not in in_flight:
                        moments_job = in_flight.get('moments')
                        if moments_job and moments_job[0]['status'] == 'running':
                            cancel = self.cancel_events.get(moments_job[0]['id'])
                            if cancel: cancel.set()  # A new chat message takes priority over the timeline scan.
                        try:
                            job = self.start_job('quick_sync', {'account_id': account['id'], 'automatic': True})
                            in_flight['chat'] = (self.jobs[job['id']], account['id'], stamp)
                        except ValueError:
                            pass
                    sns = Path(account['dbdir']) / 'sns' / 'sns.db'
                    if sns.is_file():
                        sns_stamp = signature(sns)
                        if sns_seen.get(account['id']) != sns_stamp and 'moments' not in in_flight and 'chat' not in in_flight:
                            try:
                                job = self.start_job('moments_quick', {'account_id': account['id'], 'automatic': True})
                                in_flight['moments'] = (self.jobs[job['id']], account['id'], sns_stamp)
                            except ValueError:
                                pass
                except Exception as exc:
                    self.live_status['error'] = clean_error(exc)
        self.watcher = threading.Thread(target=watch, name='incremental-watcher', daemon=True)
        self.watcher.start()

    def _queue_moments_media(self, account_id, ids):
        if not ids: return
        with self._moments_media_lock:
            for mid in ids:
                item = (account_id, mid)
                if item not in self._moments_media_pending:
                    self._moments_media_pending.append(item)
            if self._moments_media_thread and self._moments_media_thread.is_alive(): return
            def run():
                while True:
                    with self._moments_media_lock:
                        if not self._moments_media_pending: return
                        account, first = self._moments_media_pending.pop(0)
                        batch = [first]
                        remaining = []
                        for item in self._moments_media_pending:
                            if item[0] == account and len(batch) < 10: batch.append(item[1])
                            else: remaining.append(item)
                        self._moments_media_pending = remaining
                    try:
                        result = restore_moments_media(self.archive(account), batch, max_images=30)
                        if result.get('recovered'):
                            self.live_status['moments_revision'] += 1
                    except Exception as exc:
                        self.live_status['error'] = '朋友圈图片自动恢复失败：' + clean_error(exc)
            self._moments_media_thread = threading.Thread(target=run, name='moments-media', daemon=True)
            self._moments_media_thread.start()

    def scan(self):
        self.discovery = read_json(self.state / "demo.json") if self.demo else discover(self.settings.get("data_root", ""), self.base, self.state)
        with self.lock:
            self.accounts = self.discovery.get("accounts", [])
            active = [a for a in self.accounts if a.get("active")]
            current = next((a for a in self.accounts if a["id"] == self.selected), None)
            if current is None or (active and not current.get("active")):
                self.selected = active[0]["id"] if active else (self.accounts[0]["id"] if self.accounts else None)
        return self.public_state()

    def account(self, account_id=None):
        value = next((a for a in self.accounts if a["id"] == (account_id or self.selected)), None)
        if value is None:
            raise ValueError("尚未识别到账号，请先登录微信并重新检测。")
        return value

    def archive(self, account_id=None):
        a = self.account(account_id)
        return Archive(self.state / "accounts" / a["id"])

    def public_state(self):
        with self.lock:
            return {"accounts": self.accounts, "selected": self.selected, "discovery": {k: v for k, v in self.discovery.items() if k != "accounts"},
                    "settings": self.settings, "jobs": [self.job_public(j) for j in self.jobs.values()][-20:], "version": "2.1", "demo": self.demo,
                    'live': getattr(self, 'live_status', {})}

    def job_public(self, j):
        return {k: v for k, v in j.items() if k != "thread"}

    def start_job(self, kind, options):
        account = dict(self.account(options.get("account_id")))
        if kind not in {"sync", "quick_sync", "legacy", "export", "report", "emoji", "moments_sync", "moments_quick", "moments_export", "moments_repair"}:
            raise ValueError("未知任务。")
        with self.lock:
            readers = {'export', 'report', 'moments_export'}
            active = [j for j in self.jobs.values() if j['status'] == 'running' and j['account_id'] == account['id']]
            if (kind == 'legacy' and active) or any(j['kind'] == 'legacy' for j in active) or \
                    (kind not in readers and any(j['kind'] not in readers for j in active)):
                raise ValueError("当前账号的归档写入任务尚未完成。")
            job_id = secrets.token_hex(10)
            job = {"id": job_id, "kind": kind, "account_id": account["id"], "status": "running", "phase": "prepare", "percent": 0,
                   "message": "正在准备", "logs": [], "started_at": time.time(), "result": None, 'automatic': bool(options.get('automatic'))}
            self.jobs[job_id] = job
            cancel = threading.Event(); self.cancel_events[job_id] = cancel
        def progress(phase, percent, message):
            with self.lock:
                job.update(phase=phase, percent=min(100, max(0, percent)), message=clean_error(message))
                job["logs"] = (job["logs"] + [dt.datetime.now().strftime("%H:%M:%S") + "  " + clean_error(message)])[-180:]
        def run():
            try:
                directory = self.state / "accounts" / account["id"]; directory.mkdir(parents=True, exist_ok=True)
                if kind in {"sync", "quick_sync"}:
                    if self.demo:
                        raise ValueError("设计预览使用虚构数据；请在正式窗口同步微信。")
                    if kind == 'sync':
                        latest = discover(self.settings.get("data_root", ""), self.base, self.state)
                        live = next((a for a in latest["accounts"] if a["id"] == account["id"] and a.get("active")), None)
                        account.update(live if live else dict(active=False, pids=[]))
                    result = sync_account(account, directory, progress, cancel, self.settings.get("include_media", True) if kind=='sync' else False, self.settings.get("force_keys", False) and not options.get("automatic"), fast=kind=='quick_sync')
                    if self.settings.get("force_keys") and not options.get("automatic"):
                        self.settings["force_keys"] = False
                        atomic_json(self.state / "settings.json", self.settings)
                elif kind == "legacy":
                    if not account.get("legacy"):
                        raise ValueError("此账号没有可导入的旧归档。")
                    tmp = directory / (".legacy-" + job_id + ".sqlite")
                    try:
                        import_legacy(account["legacy"], tmp, account["account"], progress, cancel)
                        if cancel.is_set():
                            raise Cancelled("任务已取消")
                        os.replace(tmp, directory / "archive.sqlite")
                        atomic_json(directory / "account.json", {k: account[k] for k in ["account", "dbdir", "root", "display_name", "username"] if k in account})
                    finally:
                        tmp.unlink(missing_ok=True)
                    result = {"messages": Archive(directory).stats()["messages"]}
                elif kind == "export":
                    result = export_data(Archive(directory), options, self.settings["output_dir"], progress, cancel)
                elif kind in {"moments_sync", "moments_quick"}:
                    result = sync_moments(Archive(directory), account, progress, cancel, include_media=kind=='moments_sync')
                elif kind == "moments_export":
                    result = export_moments(Archive(directory), options, self.settings["output_dir"], progress, cancel)
                elif kind == "moments_repair":
                    result = repair_archived_moments(Archive(directory), progress, cancel)
                elif kind == "emoji":
                    result = restore_emojis(Archive(directory), account, progress, cancel,
                                            options.get('message_ids'), bool(options.get('retry_failed')))
                else:
                    result = export_report(Archive(directory), int(options.get("year", dt.datetime.now().year)), options.get("conversation_id") or None, self.settings["output_dir"], progress, cancel)
                with self.lock:
                    job.update(status="done", phase="done", percent=100, message=job['message'] if kind in {'emoji','sync'} else "已完成", result=result, finished_at=time.time())
                if kind in {"sync", "legacy"} and not options.get('automatic'):
                    self.scan()
            except Cancelled as exc:
                with self.lock:
                    job.update(status="cancelled", message=str(exc), finished_at=time.time())
            except Exception as exc:
                with self.lock:
                    job.update(status="error", message=clean_error(exc), finished_at=time.time())
                    job["logs"].append("错误：" + clean_error(exc))
            finally:
                with self.lock:
                    for old_id in [i for i,j in self.jobs.items() if i!=job_id and j.get('automatic') and j['status']!='running']:
                        del self.jobs[old_id]
                        self.cancel_events.pop(old_id,None)
                    history = [self.job_public(j) for j in self.jobs.values() if j['status'] != 'running' and not j.get('automatic')][-20:]
                    try:
                        atomic_json(self.state / 'history.json', history)
                    except OSError:
                        job['logs'].append('本次任务历史未能保存，导出的文件仍在原目录。')
        thread = threading.Thread(target=run, name="archive-" + job_id, daemon=True)
        job["thread"] = thread; thread.start()
        return self.job_public(job)

    def stop_share(self):
        if self.share:
            self.share["server"].shutdown(); self.share["server"].server_close()
            self.share["timer"].cancel(); self.share = None

    def start_share(self, job_id):
        job = self.jobs.get(job_id)
        if not job or job["kind"] != "report" or job["status"] != "done":
            raise ValueError("请先生成年度回顾。")
        self.stop_share()
        import psutil
        choices = [a.address for name, addresses in psutil.net_if_addrs().items() for a in addresses if a.family == socket.AF_INET
                   and not a.address.startswith(("127.", "169.254.")) and psutil.net_if_stats().get(name) and psutil.net_if_stats()[name].isup]
        if not choices:
            raise ValueError("没有找到可用的局域网地址，请连接 Wi-Fi 后重试。")
        host = next((a for a in choices if a.startswith("192.168.")), choices[0])
        token, contents, expiry = secrets.token_urlsafe(24), Path(job["result"]["index"]).read_bytes(), time.time() + 1800
        poster_path=job['result'].get('image')
        poster=Path(poster_path).read_bytes() if poster_path and Path(poster_path).is_file() else None
        class ShareHandler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                requested=unquote(urlsplit(self.path).path)
                allowed={'/'+token+'/':(contents,'text/html; charset=utf-8')}
                if poster:allowed['/'+token+'/年度回顾.png']=(poster,'image/png')
                if requested not in allowed or time.time() >= expiry:
                    self.send_error(404); return
                body,mime=allowed[requested]
                self.send_response(200); self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-store"); self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer((host, 0), ShareHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://{host}:{server.server_port}/{token}/"
        import qrcode
        image = qrcode.make(url); buf = io.BytesIO(); image.save(buf, format="PNG")
        timer = threading.Timer(1800, self.stop_share); timer.daemon = True
        self.share = {"server": server, "timer": timer}; timer.start()
        return {"url": url, "expires_at": expiry, "qr": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}

    def close(self):
        self.watcher_stop.set()
        self.voice.release()
        self.harness.close()
        self.automation.stop()
        self.revoke_disk.stop()
        for event in self.cancel_events.values(): event.set()
        self.stop_share()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, application, port=0):
        self.application, self.token = application, secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
    @property
    def url(self): return self.origin + "/?token=" + self.token


class Handler(BaseHTTPRequestHandler):
    server_version = "ShiguangLocal/2"
    def log_message(self, *args): pass
    def authenticated(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.server.token): return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return "sg_session" in cookie and hmac.compare_digest(cookie["sg_session"].value, self.server.token)
        except Exception: return False
    def headers_common(self):
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer"); self.send_header("X-Frame-Options", "DENY")
    def json_response(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.headers_common(); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        if self.command != "HEAD": self.wfile.write(data)
    def send_file(self, path, media=False):
        if not path.is_file(): self.send_error(404); return
        size = path.stat().st_size; start, end, partial = 0, size - 1, False
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()): self.send_error(416); return
            if not match[1]: start = max(0, size - int(match[2]))
            else:
                start = int(match[1]); end = min(size - 1, int(match[2])) if match[2] else end
            if start > end or start >= size: self.send_error(416); return
            partial = True
        self.send_response(206 if partial else 200); self.headers_common()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if media and not mime.startswith(("image/", "audio/", "video/")):
            mime = "application/octet-stream"; self.send_header("Content-Disposition", 'attachment; filename="attachment' + path.suffix + '"')
        self.send_header("Content-Type", mime); self.send_header("Accept-Ranges", "bytes")
        if partial: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if path.suffix == ".html":
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' qrc:; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command == "HEAD": return
        with path.open("rb") as f:
            f.seek(start); remaining = end - start + 1
            while remaining > 0:
                block = f.read(min(256 * 1024, remaining))
                if not block: break
                self.wfile.write(block); remaining -= len(block)
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        try:
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}": self.send_error(403); return
            parsed = urlsplit(self.path); path = unquote(parsed.path); params = parse_qs(parsed.query)
            if path == "/" and "token" in params and hmac.compare_digest(params["token"][0], self.server.token):
                self.send_response(303); self.send_header("Set-Cookie", "sg_session=" + self.server.token + "; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if not self.authenticated(): self.json_response({"error": "请从桌面程序打开此页面。"}, 401); return
            app = self.server.application; get = lambda key, default="": params.get(key, [default])[0]
            if path == "/api/state": self.json_response(app.public_state())
            elif path == "/api/stats": self.json_response(app.archive(get("account_id") or None).stats())
            elif path == "/api/conversations": self.json_response(app.archive().conversations(get("q"), get("kind", "all"), int(get("offset", 0)), int(get("limit", 100))))
            elif path == '/api/moments/candidates': self.json_response(media_candidates(app.archive(),get('mid'),int(get('index',0)),int(get('offset',0))))
            elif path == "/api/moments": self.json_response(list_moments(app.archive(),get("q"),int(get("offset",0)),int(get("limit",30))))
            elif path == "/api/contacts": self.json_response(app.archive().contacts(get("q"), int(get("offset", 0)), int(get("limit", 100))))
            elif path == '/api/automation': self.json_response(app.automation.status())
            elif path == '/api/insights/skills': self.json_response(InsightService(app).skills(get('cid')))
            elif path == '/api/insights/saved': self.json_response(InsightService(app).saved(get('cid')))
            elif path == '/api/insights/range': self.json_response(InsightService(app).date_range(get('cid')))
            elif path == '/api/insights/practice': self.json_response(InsightService(app).practice(get('cid')))
            elif path == '/api/personal/state': self.json_response(app.personal.state(get('cid')))
            elif path == '/api/articles/status':
                self.json_response(app.article_service.status(app.selected or 'local-articles'))
            elif path == '/api/storage/location':
                from .storage_location import public_location
                self.json_response(public_location(app.state))
            elif path == '/api/articles/mcp-status':
                from .articles import status
                self.json_response(status())
            elif path == '/api/weixin/multi': self.json_response(app.revoke_disk.multi_status())
            elif path.startswith('/api/harness/task/'):
                self.json_response(app.harness.get(app.selected or 'local-articles', path.rsplit('/', 1)[-1], get('after', 0)))
            elif path == '/api/harness/tasks': self.json_response({'items': app.harness.recent(app.selected or 'local-articles')})
            elif path.startswith('/api/insights/analysis/'):
                self.json_response(InsightService(app).analysis_status(path.rsplit('/', 1)[-1]))
            elif path.startswith('/api/insights/skill/'):
                self.json_response(InsightService(app).skill_status(path.rsplit('/', 1)[-1]))
            elif path == '/api/media/health': self.json_response(app.archive().media_health())
            elif path.startswith('/api/automation/send/'):
                self.json_response(app.automation.direct_status(path.rsplit('/', 1)[-1]))
            elif path == '/api/automation/moments-live':
                if app.demo: raise ValueError('演示模式无法读取微信界面。')
                self.json_response(app.automation.live_moments())
            elif path == "/api/messages": self.json_response(app.archive().messages(get("cid"), get("q"), int(get("offset", 0)), int(get("limit", 80)), get("until") or None, get("server_id"), get("kind")))
            elif path == '/api/emoji/status':
                ids=[int(v) for v in get('ids').split(',') if v][:100]
                with app.archive().connect() as c:
                    rows=c.execute('SELECT id,media_path,media_status FROM messages WHERE kind=\'emoji\' AND id IN ('+','.join('?' for _ in ids)+')',ids).fetchall() if ids else []
                self.json_response({'items':[dict(r) for r in rows]})
            elif path.startswith("/api/jobs/"):
                job = app.jobs.get(path.rsplit("/", 1)[-1]); self.json_response(app.job_public(job) if job else {"error": "任务不存在"}, 200 if job else 404)
            elif path.startswith("/media/"):
                parts = path.split("/", 3)
                if len(parts) != 4: raise ValueError("媒体路径无效")
                directory = app.archive(parts[2]).directory
                self.send_file(inside(directory / "assets", directory / parts[3]), media=True)
            elif path == "/" or path.startswith("/static/"):
                file = STATIC / "index.html" if path == "/" else inside(STATIC, STATIC / path[len("/static/"):])
                self.send_file(file)
            else: self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        except Exception as exc: self.json_response({"error": clean_error(exc)}, 400)
    def do_POST(self):
        try:
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}" or not self.authenticated(): self.json_response({"error": "访问未授权"}, 403); return
            if self.headers.get("Origin", self.server.origin) != self.server.origin or self.headers.get("Sec-Fetch-Site") == "cross-site": self.json_response({"error": "来源无效"}, 403); return
            if "application/json" not in self.headers.get("Content-Type", ""): raise ValueError("请求格式应为 JSON。")
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 1024 * 1024: raise ValueError("请求过大。")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict): raise ValueError("请求格式无效。")
            app = self.server.application; path = urlsplit(self.path).path
            if path == "/api/detect": result = app.scan()
            elif path == '/api/antirevoke':
                if app.demo: raise ValueError('演示模式无法修改微信进程。')
                action = data.get('action', 'status')
                if action == 'status': result = app.revoke_disk.status()
                elif action == 'enable': result = app.revoke_disk.queue('patched')
                elif action == 'disable': result = app.revoke_disk.queue('original')
                else: raise ValueError('防撤回操作无效。')
            elif path == "/api/select":
                selected = app.account(data.get("id")); app.selected = selected["id"]; result = app.public_state()
            elif path == "/api/settings":
                for k in ["output_dir", "data_root"]:
                    if k in data and isinstance(data[k], str):
                        if k == "output_dir" and not data[k].strip(): raise ValueError("导出目录不能为空。")
                        app.settings[k] = data[k].strip()
                for k in ["include_media", "force_keys", 'auto_sync', 'auto_emoji']:
                    if k in data: app.settings[k] = bool(data[k])
                if 'sync_interval' in data: app.settings['sync_interval'] = max(1,min(300,int(data['sync_interval'])))
                atomic_json(app.state / "settings.json", app.settings); result = app.settings
            elif path == '/api/storage/location':
                from .storage_location import public_location, save_archive_choice
                save_archive_choice(data.get('path'))
                result = {**public_location(app.state), 'restart_required': True}
            elif path == '/api/moments/bind': result=bind_media(app.archive(),data.get('mid'),int(data.get('index',0)),data.get('asset',''))
            elif path == '/api/moments/restore': result=restore_moments_media(app.archive(),data.get('ids',[]) if isinstance(data.get('ids'),list) else [])
            elif path == '/api/automation':
                if app.demo: raise ValueError('演示模式不能启用自动回复。')
                result = app.automation.update(data)
            elif path == '/api/automation/models':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = app.automation.models(data)
            elif path == '/api/automation/test-model':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = app.automation.test_model(data)
            elif path == '/api/automation/send':
                if app.demo: raise ValueError('演示模式不能发送消息。')
                result = app.automation.queue_direct(data.get('cid'),data.get('message'))
            elif path == '/api/voice/send':
                if app.demo: raise ValueError('演示模式不能录制语音。')
                aid = app.account()['id']
                result = app.harness.submit(aid, data.get('cid') or '', 'voice.send',
                                            {'account_id': aid, 'cid': data.get('cid'),
                                             'duration': data.get('duration')})
            elif path == '/api/automation/prepare-multi':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = app.automation.prepare_multi(data.get('contacts'),data.get('mode'),data.get('text'))
            elif path == '/api/automation/send-multi':
                if app.demo: raise ValueError('演示模式不能发送消息。')
                result = app.automation.queue_multi(data.get('drafts'))
            elif path == '/api/insights/analyze':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = InsightService(app).analyze(data.get('cid'),data.get('mode','both'),data.get('upload'),
                                                     start=data.get('start'),end=data.get('end'))
            elif path == '/api/insights/analysis/start':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = InsightService(app).start_analysis(data.get('cid'),data.get('mode','both'),data.get('upload'),
                                                            data.get('start'),data.get('end'))
            elif path == '/api/insights/preview-upload':
                result = InsightService.preview_upload(data.get('text'), data.get('self_label'))
            elif path == '/api/personal/start':
                result = app.personal.start(data)
            elif path == '/api/personal/memory':
                result = app.personal.configure_memory(data.get('cid'), data.get('enabled'))
            elif path == '/api/articles/start':
                if app.demo: raise ValueError('演示模式不能调用公众号工具。')
                from .articles import TOOLS
                if data.get('name') not in TOOLS: raise ValueError('不支持此公众号工具。')
                result = app.harness.submit(app.account()['id'], '', 'article.call',
                                             {'name': data['name'], 'url': data.get('url', '')})
            elif path == '/api/articles/download':
                if app.demo: raise ValueError('演示模式不能下载真实公众号文章。')
                result = app.article_service.start(app.selected or 'local-articles', data)
            elif path == '/api/articles/scan':
                if app.demo: raise ValueError('演示模式不能扫描本机微信缓存。')
                result = app.article_service.start_scan(app.selected or 'local-articles', data.get('root'))
            elif path == '/api/articles/clear-session':
                result = app.article_service.clear_session(app.selected or 'local-articles')
            elif path == '/api/weixin/multi':
                if app.demo: raise ValueError('演示模式不能修改微信。')
                action = data.get('action')
                if action == 'enable': result = app.revoke_disk.queue_multi('patched')
                elif action == 'disable': result = app.revoke_disk.queue_multi('original')
                elif action == 'launch': result = app.harness.submit(app.account()['id'], '', 'weixin.launch', {})
                else: raise ValueError('多开操作无效。')
            elif path == '/api/harness/cancel':
                result = app.harness.cancel(app.selected or 'local-articles', data.get('id'))
            elif path == '/api/harness/resume':
                result = app.harness.resume(app.selected or 'local-articles', data.get('id'))
            elif path == '/api/insights/practice':
                result = InsightService(app).record_practice(data.get('cid'),data.get('report_id'),data.get('day'),
                    data.get('note',''),data.get('old_response',''),data.get('new_response',''),data.get('outcome',''),
                    data.get('emotion_before',''),data.get('emotion_after',''))
            elif path == '/api/insights/preflight':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = InsightService(app).preflight(data.get('cid'),data.get('text'),data.get('need',''))
            elif path == '/api/insights/create-skill':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = InsightService(app).make_skill(data.get('kind'),data.get('cid'),data.get('report',''))
            elif path == '/api/insights/skill/start':
                if app.demo: raise ValueError('演示模式不能连接模型服务。')
                result = InsightService(app).start_skill(data.get('kind'), data.get('cid'))
            elif path == '/api/insights/save-skill':
                result = InsightService(app).save_skill(data.get('kind'),data.get('cid'),data.get('content'))
            elif path == '/api/insights/apply-skill':
                if app.demo: raise ValueError('演示模式不能配置自动回复。')
                result = InsightService(app).apply_skill(data.get('cid'),data.get('kind'))
            elif path == '/api/insights/export':
                report=data.get('report')
                if not isinstance(report,dict) or len(json.dumps(report,ensure_ascii=False))>500000:
                    raise ValueError('报告内容无效。')
                cid = report.get('conversation', {}).get('id')
                saved = InsightService(app).saved(cid)
                if not saved or saved.get('id') != report.get('id'):
                    raise ValueError('报告已更新，请重新打开后导出。')
                folder=Path(app.settings['output_dir'])/'聊天洞察'
                folder.mkdir(parents=True,exist_ok=True)
                target=folder/('聊天洞察-'+time.strftime('%Y%m%d-%H%M%S')+'.html')
                target.write_text(InsightService.export_html(saved),encoding='utf-8')
                result={'path':str(target)}
            elif path == '/api/insights/export-skill':
                result=InsightService(app).export_skill(data.get('cid'),data.get('kind'))
            elif path == '/api/automation/schedule':
                if app.demo: raise ValueError('演示模式不能发送消息。')
                result = app.automation.schedule_message(data.get('cid'),data.get('message'),data.get('due_at'))
            elif path == '/api/automation/schedule-multi':
                if app.demo: raise ValueError('演示模式不能发送消息。')
                result = app.automation.schedule_multi(data.get('drafts'),data.get('due_at'))
            elif path == '/api/automation/schedule/cancel':
                if app.demo: raise ValueError('演示模式不能发送消息。')
                result = app.automation.cancel_scheduled(data.get('id'))
            elif path == '/api/automation/moment':
                if app.demo: raise ValueError('演示模式不能操作朋友圈。')
                result = app.automation.moment_action(data.get('action'),data.get('mid'),data.get('content',''))
            elif path == '/api/automation/moment-live':
                if app.demo: raise ValueError('演示模式不能操作朋友圈。')
                result = app.automation.moment_action_live(data.get('action'),data.get('index'),data.get('fingerprint'),data.get('content',''))
            elif path == "/api/jobs": result = app.start_job(data.get("kind"), data.get("options", {}))
            elif path == '/api/emoji/ensure':
                ids=[int(v) for v in data.get('ids',[])][:30]
                aid=app.selected
                ids=[v for v in ids if time.time()-app.emoji_attempted.get((aid,v),0)>3600]
                if ids:
                    with app.archive().connect() as c:
                        allowed={row[0] for row in c.execute(
                            "SELECT id FROM messages WHERE id IN ("+','.join('?' for _ in ids)+") "
                            "AND kind='emoji' AND media_path='' AND media_status NOT IN "
                            "('消息中没有可用的表情下载地址','下载地址失效或数据暂时无法解码')",ids)}
                    ids=[v for v in ids if v in allowed]
                if not app.settings.get('auto_emoji') or not ids or any(j['status']=='running' for j in app.jobs.values()):result={'skipped':True}
                else:
                    result=app.start_job('emoji',{'automatic':True,'message_ids':ids,'account_id':aid})
                    for mid in ids:app.emoji_attempted[(aid,mid)]=time.time()
            elif path == "/api/cancel":
                event = app.cancel_events.get(data.get("id"))
                if event: event.set()
                result = {"ok": True}
            elif path == "/api/open":
                job = app.jobs.get(data.get("job_id"))
                if not job or job["status"] != "done" or not job.get("result"): raise ValueError("尚无可打开的导出结果。")
                target = job["result"].get("image" if data.get("image") else "index" if data.get("file") else "path")
                if not target: raise ValueError("此任务没有导出文件。")
                if app.open_callback: app.open_callback(target)
                elif os.name == "nt": os.startfile(target)
                result = {"ok": True}
            elif path == "/api/share": result = app.start_share(data.get("job_id"))
            elif path == "/api/share/stop": app.stop_share(); result = {"ok": True}
            else: self.send_error(404); return
            self.json_response(result)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        except Exception as exc: self.json_response({"error": clean_error(exc)}, 400)
