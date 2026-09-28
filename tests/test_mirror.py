import json
import io

import pytest

from wxdesk.insights import InsightService, _upload, _validated_report
from wxdesk.llm import complete_long, list_models, model_capacity, save_key, load_key, write_protected_key


def test_skill_job_reports_actual_stage_and_finishes(tmp_path, monkeypatch):
    import time
    from wxdesk.insights import InsightService
    class App:
        state = tmp_path
        def account(self): return {'id': 'account-a'}
    service = InsightService(App())
    def generate(self, kind, cid, report='', progress=None):
        progress(45, '观察语言习惯 2/4')
        return {'kind': kind, 'content': '已生成规则'}
    monkeypatch.setattr(InsightService, 'make_skill', generate)
    started = service.start_skill('my_style', 'friend')
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = service.skill_status(started['id'])
        if job['status'] == 'done': break
        time.sleep(.01)
    assert job['status'] == 'done' and job['percent'] == 100
    assert job['result']['kind'] == 'my_style'


def test_deep_report_keeps_only_valid_citations(tmp_path, monkeypatch):
    from wxdesk.insights import InsightService
    class App:
        state = tmp_path
        automation = type('AutomationStub', (), {'config': {'provider_id': 'ollama', 'api_url': 'http://127.0.0.1:11434/v1', 'model': 'fixture'},
                                                  '_secret_for': lambda self, provider: tmp_path / 'key.json'})()
        def account(self): return {'id': 'account-a'}
    service = InsightService(App())
    def model(instruction, material, *_):
        if '指定主题：' in instruction:
            titles = instruction.rsplit('指定主题：', 1)[1].split('、')
            return json.dumps({'items': [{'title': title, 'text': '只描述已核对的互动',
                                           'refs': ['U1', 'invented'], 'limits': '场景有限'} for title in titles]}, ensure_ascii=False)
        return json.dumps(_raw(['U1'], sparse=True), ensure_ascii=False)
    service._model = model
    report = service.analyze(mode='self', upload={'text': 'A：一\nB：二\nA：三\nB：四', 'self_label': 'A'}, deep=True)
    assert len(report['deep_sections']) == 16
    assert all(item['refs'] == ['U1'] for item in report['deep_sections'])
    exported = service.export_html(report)
    assert '深度观察' in exported and '数据质量与适用范围' in exported
from wxdesk.automation import Automation


def _person(refs):
    return {'strengths': ['会说明自己的请求'], 'summary_refs': refs,
            'tendencies': [{'dimension': '表达方式', 'observation': '在片段中直接提问',
                            'confidence': '高', 'refs': refs, 'alternate': '可能只是当时着急'}],
            'emotion': '情绪无法确认', 'communication': '问句较多'}


def _raw(refs, sparse=False):
    return {'self': _person(refs), 'other': _person(refs),
            'events': [] if sparse else [{'trigger': '询问回复', 'a_behavior': '发问',
                                         'b_behavior': '解释', 'possible_needs': '可能希望澄清', 'refs': refs}],
            'cycle': {} if sparse else {'name': '追问与解释', 'steps': ['发问', '解释'],
                                        'refs': refs, 'alternate': '也可能只是普通时间安排冲突'},
            'changes': [] if sparse else [{'behavior': '先问具体事件', 'refs': refs,
                                          'trigger': '回复较慢', 'old_behavior': '连续追问',
                                          'new_behavior': '确认是否方便', 'example': '你方便聊吗？',
                                          'card': {'automatic_thought': '可能被忽略', 'emotion': '留给本人确认',
                                                   'supporting_evidence': '没有及时回复',
                                                   'counter_evidence': '尚不知道当时情况',
                                                   'new_feeling': '留给本人记录'}}]}


def test_upload_preview_keeps_speakers_and_rejects_third_person():
    text = 'A：你好\nB：你好\nA：今天想聊聊\n继续这一句\nB：好'
    preview = InsightService.preview_upload(text)
    assert [x['side'] for x in preview['evidence']] == ['A', 'B', 'A', 'B']
    assert '继续这一句' in preview['evidence'][2]['text']
    assert preview['from_time'] == '时间未知'
    assert any('时间' in x for x in preview['warnings'])
    chosen = _upload(text, 'B')
    assert [x['side'] for x in chosen['sample']] == ['对方', '我', '对方', '我']
    with pytest.raises(ValueError, match='A/B 之外'):
        _upload(text + '\nC：第三人', 'A')


def test_mirror_rejects_fake_reference_and_reduces_confidence():
    evidence = [{'id': 'U1'}, {'id': 'U2'}, {'id': 'U3'}, {'id': 'U4'}]
    raw = _raw(['U1'], sparse=True)
    result = _validated_report(raw, evidence, 'self')
    assert result['other'] is None
    assert result['self']['tendencies'][0]['confidence'] == '低'
    assert result['plan'] == []
    raw['self']['tendencies'][0]['refs'] = ['U999']
    cleaned = _validated_report(raw, evidence, 'self')
    assert cleaned['self']['tendencies'] == []
    assert cleaned['evidence_warning']


def test_long_cordial_conversation_needs_no_fabricated_conflict():
    evidence = [{'id': f'U{i}'} for i in range(1, 16)]
    raw = _raw(['U1', 'U2', 'U3'])
    raw['events'] = []
    raw['cycle'] = {}
    raw['changes'] = []
    sections = _validated_report(raw, evidence, 'both')
    assert sections['cycle']['name'] == ''
    assert sections['changes'] == sections['plan'] == []
    raw['events'] = ['unexpected value']
    assert _validated_report(raw, evidence, 'both')['events'] == []


def test_plan_uses_evidence_based_focus_and_varied_daily_tasks():
    evidence = [{'id': f'U{i}'} for i in range(1, 16)]
    sections = _validated_report(_raw(['U1', 'U2', 'U3']), evidence, 'both')
    assert len(sections['plan']) == 30
    assert '回复较慢' in sections['plan'][0]['task']
    assert '确认是否方便' in sections['plan'][14]['task']
    assert len({x['task'] for x in sections['plan']}) == 30


def test_report_and_practice_bound_to_account_and_report_version(tmp_path):
    current = {'id': 'account-a'}
    class App:
        state = tmp_path
        def account(self): return current
        automation = type('AutomationStub', (), {'config': {'provider_id': 'ollama', 'api_url': 'http://127.0.0.1:11434/v1', 'model': 'fixture'},
                                                  '_secret_for': lambda self, provider: tmp_path / 'key.json'})()
    service = InsightService(App())
    service._model = lambda *args: json.dumps(_raw(['U1'], sparse=True), ensure_ascii=False)
    upload = {'text': 'A：一\nB：二\nA：三\nB：四', 'self_label': 'A'}
    report = service.analyze(mode='self', upload=upload)
    cid = report['conversation']['id']
    assert service.saved(cid)['id'] == report['id']
    exported = service.export_html(report)
    assert 'id="msg-U1"' in exported and '30 天' in exported
    result = service.record_practice(cid, report['id'], 1, note='先记录，不评价', emotion_before='紧张')
    assert result['items'][0]['emotion_before'] == '紧张'
    service.analyze(mode='self', upload=upload)
    with pytest.raises(ValueError, match='报告已更新'):
        service.record_practice(cid, report['id'], 2, note='旧报告')
    assert service.practice(cid)['items'] == []
    current['id'] = 'account-b'
    assert service.saved(cid) == {}
    assert service.practice(cid)['items'] == []


def test_preflight_only_returns_editable_draft_and_checks_quotes(tmp_path):
    current = {'id': 'account-a'}
    class App:
        state = tmp_path
        def account(self): return current
    service = InsightService(App())
    draft = '你为什么总这样？'
    response = {'risk': '中', 'signals': [{'label': '泛化', 'quote': '总这样'}],
                'possible_reading': '可能被理解为指责', 'need_question': '你最想表达什么？',
                'rewrite': '这件事让我难过，方便聊聊吗？', 'limitations': '无法预测对方反应'}
    service._model = lambda *args: json.dumps(response, ensure_ascii=False)
    result = service.preflight('alice', draft, '希望被认真听见')
    assert result['rewrite'].startswith('这件事')
    assert not service.mirror_path.exists()
    response['signals'][0]['quote'] = '草稿里没有'
    with pytest.raises(ValueError, match='草稿以外'):
        service.preflight('alice', draft)


def test_account_switch_during_model_request_cannot_save_report(tmp_path):
    current = {'id': 'account-a'}
    class App:
        state = tmp_path
        def account(self): return current
        automation = type('AutomationStub', (), {'config': {'provider_id': 'ollama', 'api_url': 'http://127.0.0.1:11434/v1', 'model': 'fixture'},
                                                  '_secret_for': lambda self, provider: tmp_path / 'key.json'})()
    service = InsightService(App())
    def switched(*args):
        current['id'] = 'account-b'
        return json.dumps(_raw(['U1'], sparse=True), ensure_ascii=False)
    service._model = switched
    with pytest.raises(ValueError, match='账号已变化'):
        service.analyze(mode='self', upload={'text': 'A：一\nB：二\nA：三\nB：四', 'self_label': 'A'})
    assert not service.mirror_path.exists()


def test_model_length_finish_is_not_accepted_as_complete(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps({'choices': [{'finish_reason': 'length',
                                     'message': {'content': '{"partial": true}'}}]}).encode()
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(ValueError):
        complete_long('https://example.com/v1', 'test', 'mock', 'instruction', 'material')


def test_long_mirror_splits_and_reports_progress_without_losing_references(tmp_path, monkeypatch):
    monkeypatch.setattr('wxdesk.insights.model_capacity', lambda *args: (12000, 9000))
    class App:
        state = tmp_path
        automation = type('AutomationStub', (), {'config': {'provider_id': 'ollama', 'api_url': 'http://127.0.0.1:11434/v1', 'model': 'fixture'},
                                                  '_secret_for': lambda self, provider: tmp_path / 'key.json'})()
        def account(self): return {'id': 'account-a'}
    service = InsightService(App())
    calls = []
    def model(instruction, material, *args):
        calls.append(instruction)
        if instruction.startswith('逐条阅读'):
            chunk = json.loads(material)
            return json.dumps({'observations': [{'fact': '双方讨论安排', 'refs': [chunk[0]['id']],
                                                  'alternative': '只是当时排程'}]}, ensure_ascii=False)
        return json.dumps(_raw(['U1'], sparse=True), ensure_ascii=False)
    service._model = model
    upload = {'text': '\n'.join(('A' if i % 2 else 'B') + '：' + ('讨论周末时间。' * 18)
                                for i in range(1, 81)), 'self_label': 'A'}
    progress = []
    report = service.analyze(mode='self', upload=upload, progress=lambda value, stage: progress.append((value, stage)))
    assert len([c for c in calls if c.startswith('逐条阅读')]) > 1
    assert report['evidence'][0]['id'] == 'U1'
    assert any('提取证据' in stage for _, stage in progress)
    assert progress[-1][0] == 98
    assert service.saved(report['conversation']['id'])['id'] == report['id']


def test_short_output_model_uses_section_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr('wxdesk.insights.model_capacity', lambda *args: (12000, 900))
    class App:
        state = tmp_path
        automation = type('AutomationStub', (), {'config': {'provider_id': 'ollama', 'api_url': 'http://127.0.0.1:11434/v1', 'model': 'fixture'},
                                                  '_secret_for': lambda self, provider: tmp_path / 'key.json'})()
        def account(self): return {'id': 'account-a'}
    service = InsightService(App())
    whole = _raw(['U1'], sparse=True)
    calls = []
    def model(instruction, *_):
        calls.append(instruction)
        if len(calls) <= 2:
            raise ValueError('模型输出达到当前额度')
        if '返回 self 和 other' in instruction:
            return json.dumps({'self': whole['self'], 'other': None}, ensure_ascii=False)
        if '返回 events 和 cycle' in instruction:
            return json.dumps({'events': [], 'cycle': {}}, ensure_ascii=False)
        return json.dumps({'changes': []}, ensure_ascii=False)
    service._model = model
    report = service.analyze(mode='self', upload={'text': 'A：一\nB：二\nA：三\nB：四', 'self_label': 'A'})
    assert len(calls) == 5
    assert report['sections']['other'] is None
    assert report['sections']['changes'] == []


def test_model_catalog_keeps_every_id_and_exposes_capacity(monkeypatch):
    catalog = {'data': [{'id': f'model-{i}', 'context_length': 128000,
                         'max_output_tokens': 16384} for i in range(600)]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(catalog).encode()
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: Response())
    models = list_models('https://example.com/v1', 'test')
    assert len(models) == 600
    assert model_capacity('https://example.com/v1', 'test', 'model-599') == (128000, 16384)


def test_long_model_uses_provider_maximum_or_omits_local_ceiling(monkeypatch):
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return b'{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}'
    def open_request(request, timeout):
        requests.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', open_request)
    assert complete_long('https://example.com/v1', 'fixture', 'model', 'reply', 'ping', 393216) == 'OK'
    assert complete_long('https://example.com/v1', 'fixture', 'model', 'reply', 'ping') == 'OK'
    assert requests[0]['max_tokens'] == 393216
    assert 'max_tokens' not in requests[1]


def test_model_connection_checks_current_form_without_chat_or_saving(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('wxdesk.llm.complete_long', lambda *args: calls.append(args) or 'OK')
    class Stub:
        config = {'provider_id': 'custom'}
        app = type('AppStub', (), {'account': lambda self: {'id': 'account-a'}})()
        def _secret_for(self, provider): return tmp_path / 'key.json'
    result = Automation.test_model(Stub(), {'provider_id': 'custom', 'api_url': 'https://example.com/v1',
                                             'model': 'chosen-model', 'api_key': 'test-key'})
    assert result['ok'] is True and result['model'] == 'chosen-model'
    assert calls[0][4] == 'ping' and calls[0][5] == 512
    assert not (tmp_path / 'key.json').exists()


def test_api_key_is_dpapi_protected_at_rest(tmp_path):
    path = tmp_path / 'model-secret.json'
    secret = 'SG-NOT-A-REAL-KEY-12345'
    save_key(path, 'account-a', secret)
    stored = path.read_text('utf-8')
    assert secret not in stored
    assert load_key(path, 'account-a') == secret
    assert load_key(path, 'account-b') == ''


def test_old_encrypted_key_moves_out_of_shareable_app_folder(tmp_path):
    old_dir, private_dir = tmp_path / 'app', tmp_path / 'profile' / 'secrets'
    old_dir.mkdir()
    private_dir.mkdir(parents=True)
    old = old_dir / 'automation_secret.json'
    save_key(old, 'account-a', 'SG-NOT-A-REAL-KEY-12345')
    stub = type('SecretMigrationStub', (), {'secret_dir': private_dir})()
    Automation._migrate_legacy_secrets(stub, old_dir)
    assert not old.exists()
    assert load_key(private_dir / old.name, 'account-a') == 'SG-NOT-A-REAL-KEY-12345'


def test_encrypted_folder_rename_failure_preserves_protected_key(tmp_path, monkeypatch):
    path = tmp_path / 'model-secret.json'
    def reject_rename(*args):
        error = OSError('Windows encrypted folder rejected rename')
        error.winerror = 17
        raise error
    monkeypatch.setattr('wxdesk.llm.atomic_json', reject_rename)
    save_key(path, 'account-a', 'SG-NOT-A-REAL-KEY-12345')
    assert load_key(path, 'account-a') == 'SG-NOT-A-REAL-KEY-12345'
    assert 'SG-NOT-A-REAL-KEY-12345' not in path.read_text('utf-8')


def test_style_generation_recovers_from_output_cap_by_sections(tmp_path, monkeypatch):
    rows = [{'id': str(i), 'time': '2026-09-27 12:00', 'side': '我',
             'text': '今天有空的话再商量一下', 'segment': 1} for i in range(12)]
    monkeypatch.setattr('wxdesk.insights._sample', lambda *args, **kwargs: {
        'conversation': {'id': 'alice', 'title': 'Alice'}, 'total': 12,
        'sampled': 12, 'sample': rows, 'from_time': rows[0]['time'], 'to_time': rows[-1]['time']})
    class App:
        state = tmp_path
        automation = type('AutomationStub', (), {'config': {}})()
        def account(self): return {'id': 'account-a'}
        def archive(self): return None
    service = InsightService(App())
    calls = []
    def model(instruction, *_):
        calls.append(instruction)
        if '上次达到输出额度' not in instruction:
            raise ValueError('模型输出达到当前额度')
        return '以自然语气回应，先回应对方当前问题，再根据语境提出一个具体问题。'
    service._model = model
    result = service.make_skill('my_style', 'alice')
    assert len(calls) == 8
    assert result['content'].count('## ') == 4


def test_full_period_reads_every_text_message_without_sampling(tmp_path):
    import contextlib
    import sqlite3
    from wxdesk.insights import _sample
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE conversations(id TEXT,title TEXT,kind TEXT);"
                        "CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,ts INTEGER,"
                        "is_self INTEGER,body TEXT,kind TEXT);"
                        "INSERT INTO conversations VALUES('alice','Alice','direct');")
        c.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                      [(i, 'alice', 1000+i, i%2, f'message {i}', 'text') for i in range(1, 2501)])
    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c
    all_rows = _sample(Archive(), 'alice', limit=None)
    assert all_rows['total'] == all_rows['sampled'] == 2500
    assert [x['id'] for x in all_rows['sample'][::2499]] == ['1', '2500']
    period = _sample(Archive(), 'alice', limit=None, start=1200, end=1300)
    assert period['total'] == period['sampled'] == 100
    assert period['sample'][0]['id'] == '200' and period['sample'][-1]['id'] == '299'
    service = InsightService(type('App', (), {'state': tmp_path, 'archive': lambda self: Archive()})())
    bounds = service.date_range('alice')
    assert bounds['count'] == 2500
    assert bounds['first'] and bounds['last']


def test_large_full_archive_is_chunked_and_report_keeps_cited_evidence(tmp_path, monkeypatch):
    import contextlib
    import sqlite3
    db = tmp_path / 'archive.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE conversations(id TEXT,title TEXT,kind TEXT);"
                        "CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,ts INTEGER,"
                        "is_self INTEGER,body TEXT,kind TEXT);"
                        "INSERT INTO conversations VALUES('alice','Alice','direct');")
        c.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                      ((i, 'alice', 1700000000+i, i%2, '商量今天的具体安排。'*8, 'text')
                       for i in range(1, 27051)))
    class Archive:
        @contextlib.contextmanager
        def connect(self):
            with sqlite3.connect(db) as c:
                c.row_factory = sqlite3.Row
                yield c
    class App:
        state = tmp_path
        automation = type('AutomationStub', (), {'config': {'provider_id': 'ollama', 'api_url': 'http://127.0.0.1:11434/v1', 'model': 'fixture'},
                                                  '_secret_for': lambda self, provider: tmp_path / 'key.json'})()
        def account(self): return {'id': 'account-a'}
        def archive(self): return Archive()
    monkeypatch.setattr('wxdesk.insights.model_capacity', lambda *args: (1048576, 393216))
    service = InsightService(App())
    chunks = []
    def model(instruction, material, *_):
        if instruction.startswith('逐条阅读'):
            window = json.loads(material)
            chunks.append(len(window))
            return json.dumps({'observations': [{'fact': '讨论安排', 'refs': [window[0]['id']],
                                                  'alternative': '也可能只是日常闲聊'}]}, ensure_ascii=False)
        return json.dumps(_raw(['1'], sparse=True), ensure_ascii=False)
    service._model = model
    report = service.analyze('alice')
    assert report['total'] == report['sampled'] == 27050
    assert sum(chunks) == 27050 and len(chunks) > 1
    assert [row['id'] for row in report['evidence']] == ['1']
