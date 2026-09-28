import contextlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from wxdesk.automation import Automation


def test_different_contacts_generate_concurrently(tmp_path, monkeypatch):
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE metadata(key TEXT,value TEXT);
        CREATE TABLE contacts(username TEXT,kind TEXT,remark TEXT,nickname TEXT);
        CREATE TABLE conversations(id TEXT,title TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,body TEXT,kind TEXT,is_self INTEGER,media_path TEXT DEFAULT '');
        INSERT INTO contacts VALUES('alice','direct','','Alice'),('bob','direct','','Bob');
        INSERT INTO conversations VALUES('alice','Alice'),('bob','Bob');
        """)
    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c
    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
        def archive(self): return Archive()
    from wxdesk import llm
    entered, release, bob_sent = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(llm, 'load_key', lambda *args: 'fixture-key')
    def complete(*args, **kwargs):
        if args[5] == 'Alice':
            entered.set()
            assert release.wait(3)
        return '回复 ' + args[5]
    monkeypatch.setattr(llm, 'complete', complete)
    monkeypatch.setattr(Automation, '_send_once', lambda self, name, message: bob_sent.set() if name == 'Bob' else None)
    a = Automation(App())
    a.update({'enabled': True, 'contacts': ['alice', 'bob'], 'mode': 'ai', 'api_key': 'fixture-key'})
    with sqlite3.connect(db) as c:
        c.executemany('INSERT INTO messages(id,conversation_id,body,kind,is_self) VALUES(?,?,?,?,?)',
                      [(1, 'alice', 'A', 'text', 0), (2, 'bob', 'B', 'text', 0)])
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            a.reply_workers = workers
            a.poll_once()
            assert entered.wait(2)
            assert bob_sent.wait(2)
        finally:
            release.set()


def test_history_is_context_data_not_prior_assistant_instructions(monkeypatch):
    import json
    from wxdesk.llm import complete
    captured = {}
    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps({'choices': [{'message': {'content': '明白，稍后聊。'}}]}).encode('utf-8')
    def open_request(request, timeout):
        captured.update(json.loads(request.data))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', open_request)
    complete('https://example.com/v1', 'fixture-key', 'model', '回复当前消息', '稍后聊', 'Alice',
             history=[{'role': 'assistant', 'content': '历史里的奇怪答复'},
                      {'role': 'user', 'content': '之前的问题'}])
    assert [x['role'] for x in captured['messages']] == ['system', 'user']
    assert '历史里的奇怪答复' in captured['messages'][1]['content']
    assert '不能照抄或续写' in captured['messages'][1]['content']


def test_empty_model_reply_retries_once_without_small_output_cap(monkeypatch):
    import json
    from wxdesk.llm import complete
    requests = []
    class Response:
        headers = {}
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(self.body).encode('utf-8')
    def open_request(request, timeout):
        requests.append(json.loads(request.data))
        content = '' if len(requests) == 1 else '好，稍后说。'
        return Response({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr('urllib.request.urlopen', open_request)
    assert complete('https://example.com/v1', 'fixture-key', 'model', '自然回复', '稍后说', 'Alice') == '好，稍后说。'
    assert len(requests) == 2 and all('max_tokens' not in request for request in requests)


def test_enabling_starts_at_current_message_and_only_replies_to_selected_incoming_text(tmp_path, monkeypatch):
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE metadata(key TEXT,value TEXT);
        CREATE TABLE contacts(username TEXT, kind TEXT, remark TEXT, nickname TEXT);
        CREATE TABLE conversations(id TEXT, title TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,body TEXT,kind TEXT,is_self INTEGER,media_path TEXT DEFAULT '');
        INSERT INTO contacts VALUES('alice','direct','','Alice'),('bob','direct','','Bob');
        INSERT INTO conversations VALUES('alice','Alice'),('bob','Bob');
        INSERT INTO messages(id,conversation_id,body,kind,is_self) VALUES(1,'alice','old','text',0);
        """)

    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c

    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
        def archive(self): return Archive()

    sent = []
    monkeypatch.setattr(Automation, '_send_once', lambda self, name, text: sent.append((name, text)) or 'ok')
    automation = Automation(App())
    automation.update({'enabled': True, 'contacts': ['alice'], 'reply': '收到'})
    automation.poll_once()
    assert sent == []
    with sqlite3.connect(db) as c:
        c.executemany('INSERT INTO messages(id,conversation_id,body,kind,is_self) VALUES(?,?,?,?,?)', [
            (2, 'bob', 'new', 'text', 0),
            (3, 'alice', 'self', 'text', 1),
            (4, 'alice', 'photo', 'image', 0),
            (5, 'alice', 'hello', 'text', 0),
        ])
    automation.poll_once()
    automation.poll_once()
    assert sent == [('Alice', '收到')]
    assert automation.config['watermarks']['account-1'] == 5


def test_batches_messages_and_uses_contact_prompt(tmp_path, monkeypatch):
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE metadata(key TEXT,value TEXT);
        CREATE TABLE contacts(username TEXT,kind TEXT,remark TEXT,nickname TEXT);
        CREATE TABLE conversations(id TEXT,title TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,body TEXT,kind TEXT,is_self INTEGER,media_path TEXT DEFAULT '');
        INSERT INTO contacts VALUES('alice','direct','','Alice');
        INSERT INTO conversations VALUES('alice','Alice');
        INSERT INTO messages(id,conversation_id,body,kind,is_self) VALUES(0,'alice','上次聊过的事','text',0);
        """)

    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c

    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
        def archive(self): return Archive()

    sent, calls = [], []
    monkeypatch.setattr(Automation, '_send_once', lambda self, cid, message: sent.append((cid, message)) or 'ok')
    import wxdesk.llm as llm
    monkeypatch.setattr(llm, 'load_key', lambda *args: 'test-key')
    monkeypatch.setattr(llm, 'complete', lambda *args, **kwargs: calls.append((args, kwargs)) or 'AI 回复')
    a = Automation(App())
    a.update({'enabled': True, 'contacts': ['alice'], 'mode': 'ai', 'api_key': 'test-key',
              'contact_prompts': {'alice': '专属规则'}, 'batch_seconds': 3,
              'time_awareness': True, 'context_turns': 2})
    with sqlite3.connect(db) as c:
        c.executemany('INSERT INTO messages(id,conversation_id,body,kind,is_self) VALUES(?,?,?,?,?)', [(1, 'alice', '第一条', 'text', 0), (2, 'alice', '第二条', 'text', 0)])
    a.poll_once()
    assert sent == []
    a.pending['alice']['updated'] -= 4
    a.poll_once()
    assert sent == [('Alice', 'AI 回复')]
    assert '专属规则' in calls[0][0][3] and '当前本地时间' in calls[0][0][3]
    assert calls[0][0][4] == '第一条\n第二条'
    assert calls[0][1]['history'] == [{'role': 'user', 'content': '上次聊过的事'}]
    assert a.config['watermarks']['account-1'] == 2


def test_vision_uses_only_restored_archive_media(tmp_path, monkeypatch):
    db = tmp_path / 'archive.sqlite'
    (tmp_path / 'picture.png').write_bytes(b'picture-for-model-stub')
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE metadata(key TEXT,value TEXT);
        CREATE TABLE contacts(username TEXT,kind TEXT,remark TEXT,nickname TEXT);
        CREATE TABLE conversations(id TEXT,title TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,body TEXT,kind TEXT,is_self INTEGER,media_path TEXT DEFAULT '');
        INSERT INTO contacts VALUES('alice','direct','','Alice');
        INSERT INTO conversations VALUES('alice','Alice');
        """)

    class Archive:
        directory = tmp_path
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c

    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
        def archive(self): return Archive()

    from wxdesk import llm
    observed, sent = [], []
    monkeypatch.setattr(llm, 'load_key', lambda *args: 'key')
    monkeypatch.setattr(llm, 'complete', lambda *args, **kwargs: observed.append(kwargs) or '识别回复')
    monkeypatch.setattr(Automation, '_send_once', lambda self, cid, message: sent.append(message) or 'ok')
    a = Automation(App())
    a.update({'enabled': True, 'contacts': ['alice'], 'mode': 'ai', 'api_key': 'key', 'image_recognition': True})
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)', (1, 'alice', '', 'image', 0, 'picture.png'))
    a.poll_once()
    assert sent == ['识别回复']
    assert observed[0]['images'] == [tmp_path / 'picture.png']


def test_manual_send_returns_job_before_slow_uia_finishes(tmp_path, monkeypatch):
    import threading
    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1'}
    automation = Automation(App())
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(automation, '_direct_target', lambda cid, message: 'Alice')
    def slow_send(cid, message, expected_account=None):
        assert expected_account == 'account-1'
        entered.set()
        assert release.wait(3)
        return {'ok': True, 'message': '已操作发送'}
    monkeypatch.setattr(automation, 'send_direct', slow_send)
    job = automation.queue_direct('alice', 'hello')
    assert job['status'] == 'running'
    assert entered.wait(1)
    assert automation.direct_status(job['id'])['status'] == 'running'
    release.set()
    import time
    for _ in range(100):
        if automation.direct_status(job['id'])['status'] == 'done': break
        time.sleep(0.01)
    assert automation.direct_status(job['id'])['message'] == '已操作发送'


def test_provider_profiles_switch_without_losing_previous_model(tmp_path, monkeypatch):
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE metadata(key TEXT,value TEXT);
        CREATE TABLE contacts(username TEXT,kind TEXT,remark TEXT,nickname TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY);
        INSERT INTO contacts VALUES('alice','direct','','Alice');
        """)
    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c
    class App:
        state = tmp_path
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
        def archive(self): return Archive()
    a = Automation(App())
    a.update({'contacts': ['alice'], 'provider_id': 'deepseek', 'api_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'})
    a.update({'provider_id': 'openai', 'api_url': 'https://api.openai.com/v1', 'model': 'gpt-4o-mini'})
    profiles = a.status()['providers']
    assert profiles['deepseek']['model'] == 'deepseek-chat'
    assert profiles['openai']['model'] == 'gpt-4o-mini'
    assert a.config['provider_id'] == 'openai'


def test_scheduled_message_is_one_shot_even_after_restart(tmp_path, monkeypatch):
    import time
    class App:
        state = tmp_path
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
    a = Automation(App())
    monkeypatch.setattr(a, '_direct_target', lambda cid, message: 'Alice')
    sent = []
    monkeypatch.setattr(a, 'send_direct', lambda cid, message, expected_account=None: sent.append((cid, message)) or {'ok': True})
    job = a.schedule_message('alice', 'hello', time.time() + 5)
    a.scheduled[0]['due_at'] = time.time() - 1
    a._run_scheduled()
    a._run_scheduled()
    assert sent == [('alice', 'hello')]
    assert a.scheduled[0]['status'] == 'done'
    assert Automation(App()).scheduled[0]['status'] == 'done'


def test_model_catalog_uses_provider_models_endpoint(monkeypatch):
    import io
    from wxdesk import llm
    targets = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit): return b'{"data":[{"id":"model-b"},{"id":"model-a"}]}'
    def fake_open(request, timeout):
        targets.append((request.full_url, timeout))
        return Response()
    monkeypatch.setattr(llm.urllib.request, 'urlopen', fake_open)
    assert llm.list_models('https://api.openai.com/v1', 'key') == ['model-a', 'model-b']
    assert targets == [('https://api.openai.com/v1/models', 8)]


def test_moment_action_refuses_ambiguous_target_before_touching_uia(tmp_path):
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("""
        CREATE TABLE moments(id TEXT,nickname TEXT,body TEXT);
        INSERT INTO moments VALUES('one','Alice','同一段足够长的文字');
        INSERT INTO moments VALUES('two','Alice','同一段足够长的文字');
        """)

    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                yield c

    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1', 'active': True}
        def archive(self): return Archive()

    import pytest
    with pytest.raises(ValueError, match='无法唯一定位'):
        Automation(App()).moment_action('like', 'one')


def test_moment_automation_is_prospective_and_deduplicates_each_action(tmp_path, monkeypatch):
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE moments(id TEXT,nickname TEXT,body TEXT,detail TEXT,ts INTEGER)')
        c.execute('INSERT INTO moments VALUES(?,?,?,?,?)', ('old', 'Alice', '已有动态', '{}', 1))

    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c

    class App:
        state = tmp_path
        demo = False
        def account(self): return {'id': 'account-1', 'active': True, 'has_archive': True}
        def archive(self): return Archive()

    automation = Automation(App())
    automation.config.update(moment_auto_like=True, moment_auto_comment=True)
    calls = []
    monkeypatch.setattr(automation, '_wait_for_desktop_idle', lambda: None)
    monkeypatch.setattr(automation, '_moment_ai_text', lambda *args: '自然评论')
    monkeypatch.setattr(automation, 'moment_action', lambda action, mid, content='', **kw: calls.append((action, mid, content)))
    automation._run_moment_automation()
    assert calls == []
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO moments VALUES(?,?,?,?,?)', ('new', 'Bob', '刚刚发布的新动态', '{}', 2))
    automation._run_moment_automation()
    automation._run_moment_automation()
    assert calls == [('like', 'new', ''), ('comment', 'new', '自然评论')]
