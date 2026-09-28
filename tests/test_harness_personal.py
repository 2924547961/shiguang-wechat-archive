import json
import time

from wxdesk.harness import Harness
from wxdesk.personal import reply_memory, split_material
from wxdesk.demo import make_demo
from wxdesk.server import Application


def _finished(harness, account, task_id):
    for _ in range(100):
        task = harness.get(account, task_id)
        if task['status'] != 'running': return task
        time.sleep(.01)
    raise AssertionError('task did not finish')


def test_harness_checkpoint_survives_retry_and_is_account_scoped(tmp_path):
    harness = Harness(tmp_path)
    calls = []
    failed = [False]

    def work(context, payload):
        result = context.tool('model.complete', {'input': payload['input']},
                              lambda: calls.append(payload['input']) or 'stored output')
        if payload['fail'] and not failed[0]:
            failed[0] = True
            raise ValueError('temporary provider failure')
        context.progress(85, '合并结果')
        return result

    harness.register('test.work', work)
    first = harness.submit('account-a', 'contact-a', 'test.work', {'input': 'private', 'fail': True})
    assert _finished(harness, 'account-a', first['id'])['status'] == 'error'
    try:
        harness.get('account-b', first['id'])
    except ValueError:
        pass
    else:
        raise AssertionError('another account read a private task')
    resumed = harness.resume('account-a', first['id'])
    assert _finished(harness, 'account-a', resumed['id'])['result'] == 'stored output'
    assert calls == ['private']
    events = harness.get('account-a', first['id'])['events']
    assert any(x['kind'] == 'tool_reused' for x in events)
    assert 'private' not in json.dumps(events)
    harness.close()


def test_external_action_is_logged_without_result_or_automatic_replay(tmp_path):
    harness = Harness(tmp_path)
    calls = []

    def work(context, payload):
        context.external('weixin.voice', {'cid': payload['cid']},
                         lambda: calls.append('sent') or {'private': 'voice-result'})
        raise ValueError('confirmation unavailable')

    harness.register('voice.send', work, tools=('weixin.voice',))
    task = harness.submit('account-a', 'contact-a', 'voice.send', {'cid': 'contact-a'})
    result = _finished(harness, 'account-a', task['id'])
    assert result['status'] == 'error'
    assert calls == ['sent']
    assert [event['kind'] for event in result['events'] if event['kind'].startswith('external_')] == [
        'external_started', 'external_completed']
    assert 'voice-result' not in json.dumps(result['events'])
    try:
        harness.resume('account-a', task['id'])
    except ValueError:
        pass
    else:
        raise AssertionError('a voice send must never replay automatically')
    assert calls == ['sent']
    harness.close()


def test_tool_allowlist_is_scoped_to_task_action(tmp_path):
    harness = Harness(tmp_path)
    harness.register('test.readonly', lambda context, _: context.external('weixin.voice', {}, lambda: 'sent'))
    task = harness.submit('account-a', '', 'test.readonly', {})
    result = _finished(harness, 'account-a', task['id'])
    assert result['status'] == 'error'
    assert '不允许' in result['error']
    harness.close()


def test_split_material_covers_every_character_and_side():
    rows = [{'id': 1, 'ts': 10, 'is_self': True, 'body': '甲' * 301},
            {'id': 2, 'ts': 11, 'is_self': False, 'body': '乙' * 17}]
    flattened = [item for chunk in split_material(rows, 100) for item in chunk]
    assert ''.join(x['text'] for x in flattened if x['id'] == '1') == rows[0]['body']
    assert ''.join(x['text'] for x in flattened if x['id'] == '2') == rows[1]['body']
    assert {x['side'] for x in flattened} == {'我', '对方'}


def test_memory_and_coach_use_selected_chat_without_sending(tmp_path):
    make_demo(tmp_path)
    app = Application(state=tmp_path, base=tmp_path, demo=True)
    try:
        account_id, cid = app.account()['id'], 'demo_xiaoyu'
        def model(instruction, material):
            if '蒸馏为可复用记忆' in instruction:
                return json.dumps({'facts': [{'text': '双方讨论周末安排',
                                               'refs': [material[0]['id']]}]}, ensure_ascii=False)
            if '你与用户直接进行表达训练' in instruction:
                return '模拟情境：朋友临时改期。你会怎样表达自己的感受和可行时间？'
            return '已确认的安排与表达习惯；保留证据，不照抄旧回复。'
        memory = app.personal.distill(account_id, cid, {'start': None, 'end': None},
                                      3000, model, lambda *_: None, {'api_url': 'local', 'model': 'fixture'})
        with app.archive().connect() as db:
            count = db.execute("SELECT count(*) FROM messages WHERE conversation_id=? AND kind='text' AND body<>''",
                               (cid,)).fetchone()[0]
        assert memory['count'] == count
        assert memory['enabled'] is False
        assert app.personal.state(cid)['memory']['count'] == count
        assert reply_memory(app, account_id, cid) == ''
        app.personal.configure_memory(cid, True)
        assert '已确认的安排' in reply_memory(app, account_id, cid)
        session = app.personal.coach(account_id, cid, {'goal': '练习表达改期'}, 3000,
                                     model, lambda *_: None)
        assert session['turns'][0]['role'] == 'assistant'
        assert '模拟情境' in session['turns'][0]['text']
    finally:
        app.close()
