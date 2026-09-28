"""Small, durable task harness for user-started analysis and coaching.

The archive and provider remain owned by the application. This runtime stores
task state and progress, never API credentials or hidden model reasoning.
"""
from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class Interrupted(Exception):
    pass


class Context:
    def __init__(self, harness, task_id, cancel, action):
        self.harness, self.task_id, self.cancel, self.action = harness, task_id, cancel, action

    def check(self):
        if self.cancel.is_set(): raise Interrupted('任务已取消。')

    def progress(self, percent, stage):
        self.check()
        self.harness._update(self.task_id, percent=max(0, min(99, int(percent))), stage=str(stage)[:180])
        self.harness._event(self.task_id, 'progress', {'percent': percent, 'stage': stage})

    def checkpoint(self, name, producer):
        self.check()
        with self.harness.connect() as db:
            row = db.execute('SELECT value FROM checkpoints WHERE task_id=? AND name=?', (self.task_id, name)).fetchone()
        if row: return json.loads(row[0])
        value = producer()
        self.check()
        with self.harness.connect() as db:
            db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)',
                       (self.task_id, name, json.dumps(value, ensure_ascii=False)))
        self.harness._event(self.task_id, 'checkpoint', {'name': name})
        return value

    def tool(self, name, inputs, producer):
        """Run a named tool once per stable input, recording its lifecycle.

        Inputs may contain private chats, so the event log stores only a digest.
        The result is checkpointed for a retry after interruption.
        """
        if name not in self.harness.allowed_tools.get(self.action, ()):
            raise ValueError('当前任务不允许调用此工具。')
        digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        checkpoint_name = 'tool:' + name + ':' + digest
        with self.harness.connect() as db:
            old = db.execute('SELECT value FROM checkpoints WHERE task_id=? AND name=?',
                             (self.task_id, checkpoint_name)).fetchone()
        if old:
            self.harness._event(self.task_id, 'tool_reused', {'name': name, 'digest': digest})
            return json.loads(old[0])
        self.harness._event(self.task_id, 'tool_started', {'name': name, 'digest': digest})
        try:
            value = self.checkpoint(checkpoint_name, producer)
        except Exception:
            self.harness._event(self.task_id, 'tool_failed', {'name': name, 'digest': digest})
            raise
        self.harness._event(self.task_id, 'tool_completed', {'name': name, 'digest': digest})
        return value

    def external(self, name, inputs, producer):
        """Record a side-effecting tool without replaying or checkpointing it."""
        if name not in self.harness.allowed_tools.get(self.action, ()):
            raise ValueError('当前任务不允许调用此工具。')
        self.check()
        digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.harness._event(self.task_id, 'external_started', {'name': name, 'digest': digest})
        try:
            result = producer()
        except Exception:
            self.harness._event(self.task_id, 'external_failed', {'name': name, 'digest': digest})
            raise
        self.harness._event(self.task_id, 'external_completed', {'name': name, 'digest': digest})
        return result


class Harness:
    def __init__(self, state: Path):
        self.path = Path(state) / 'harness.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.handlers = {}
        self.allowed_tools = {}
        self.cancels = {}
        self.limiter = threading.BoundedSemaphore(4)
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?,stage=?,updated=? WHERE status=?',
                       ('interrupted', '程序关闭，点击继续以恢复', time.time(), 'running'))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,account_id TEXT,cid TEXT,action TEXT,'
                       'payload TEXT,status TEXT,percent INTEGER,stage TEXT,result TEXT,error TEXT,updated REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,'
                       'kind TEXT,value TEXT,at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS checkpoints(task_id TEXT,name TEXT,value TEXT,PRIMARY KEY(task_id,name))')
            with db:
                yield db
        finally:
            db.close()

    def register(self, action, handler, tools=('model.complete',)):
        self.handlers[action] = handler
        self.allowed_tools[action] = frozenset(tools)

    def submit(self, account_id, cid, action, payload, resume=None):
        if action not in self.handlers: raise ValueError('任务类型不受支持。')
        with self.lock, self.connect() as db:
            if len(self.cancels) >= 40: raise ValueError('后台任务过多，请等待或取消已有任务。')
            if resume:
                row = db.execute('SELECT * FROM tasks WHERE id=? AND account_id=?', (resume, account_id)).fetchone()
                if not row or row['status'] not in {'interrupted', 'error', 'cancelled'}:
                    raise ValueError('任务无法继续。')
                if row['action'] != action or row['cid'] != cid: raise ValueError('任务账号或联系人不匹配。')
                task_id, payload = resume, json.loads(row['payload'])
            else:
                task_id = secrets.token_hex(12)
            if task_id in self.cancels: raise ValueError('任务已在运行。')
            db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, account_id, cid, action, json.dumps(payload, ensure_ascii=False),
                        'running', 0, '准备材料', None, '', time.time()))
            cancel = threading.Event()
            self.cancels[task_id] = cancel
        self._event(task_id, 'started', {'action': action})
        threading.Thread(target=self._run_limited, args=(task_id, action, payload, cancel),
                         name='shiguang-task-' + task_id[:8], daemon=True).start()
        return self.get(account_id, task_id)

    def _run_limited(self, task_id, action, payload, cancel):
        with self.limiter:
            self._run(task_id, action, payload, cancel)

    def _run(self, task_id, action, payload, cancel):
        context = Context(self, task_id, cancel, action)
        try:
            context.check()
            value = self.handlers[action](context, payload)
            context.check()
            self._update(task_id, status='done', percent=100, stage='已完成', result=json.dumps(value, ensure_ascii=False))
            self._event(task_id, 'completed', {})
        except Interrupted as exc:
            self._update(task_id, status='cancelled', stage=str(exc))
            self._event(task_id, 'cancelled', {})
        except Exception as exc:
            self._update(task_id, status='error', stage='任务未完成，可点击继续', error=str(exc)[:300])
            self._event(task_id, 'error', {'message': str(exc)[:300]})
        finally:
            with self.lock: self.cancels.pop(task_id, None)

    def _update(self, task_id, **changes):
        if not changes: return
        allowed = {'status', 'percent', 'stage', 'result', 'error'}
        if set(changes) - allowed: raise ValueError('任务字段无效。')
        with self.connect() as db:
            db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in changes) + ',updated=? WHERE id=?',
                       (*changes.values(), time.time(), task_id))

    def _event(self, task_id, kind, value):
        with self.connect() as db:
            db.execute('INSERT INTO events(task_id,kind,value,at) VALUES(?,?,?,?)',
                       (task_id, kind, json.dumps(value, ensure_ascii=False), time.time()))

    def get(self, account_id, task_id, after=0):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=? AND account_id=?', (task_id, account_id)).fetchone()
            if not row: raise ValueError('任务不存在或账号已切换。')
            events = [dict(x) for x in db.execute('SELECT seq,kind,value,at FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 100',
                                                 (task_id, int(after)))]
        result = {k: row[k] for k in ('id', 'cid', 'action', 'status', 'percent', 'stage', 'error', 'updated')}
        result['result'] = json.loads(row['result']) if row['result'] else None
        result['events'] = [{'seq': e['seq'], 'kind': e['kind'], 'value': json.loads(e['value']), 'at': e['at']} for e in events]
        return result

    def list(self, account_id, cid=None):
        with self.connect() as db:
            if cid:
                rows = db.execute('SELECT id FROM tasks WHERE account_id=? AND cid=? ORDER BY updated DESC LIMIT 25',
                                  (account_id, cid))
            else:
                rows = db.execute('SELECT id FROM tasks WHERE account_id=? ORDER BY updated DESC LIMIT 25', (account_id,))
            ids = [r[0] for r in rows]
        return [self.get(account_id, task_id) for task_id in ids]

    def recent(self, account_id):
        with self.connect() as db:
            rows = db.execute('SELECT id,cid,action,status,percent,stage,error,updated FROM tasks '
                              'WHERE account_id=? ORDER BY updated DESC LIMIT 30', (account_id,)).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, account_id, task_id):
        task = self.get(account_id, task_id)
        if task['action'] == 'voice.send' and task['status'] == 'running':
            raise ValueError('语音正在录制；请等待自动结束。')
        with self.lock:
            event = self.cancels.get(task_id)
            if event: event.set()
        return {'ok': bool(event)}

    def resume(self, account_id, task_id):
        with self.connect() as db:
            row = db.execute('SELECT cid,action,payload FROM tasks WHERE id=? AND account_id=?',
                             (task_id, account_id)).fetchone()
        if not row: raise ValueError('任务不存在或账号已切换。')
        if row['action'] in {'article.call', 'article.download', 'weixin.launch', 'voice.send'}:
            raise ValueError('外部操作可能已经执行，请先查看实际状态；需要时重新发起。')
        return self.submit(account_id, row['cid'], row['action'], json.loads(row['payload']), resume=task_id)

    def close(self):
        with self.lock:
            for event in self.cancels.values(): event.set()
