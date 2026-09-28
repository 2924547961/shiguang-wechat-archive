import json
import time
import threading

from bs4 import BeautifulSoup

from backend.articles.downloader import Article, Options, WeChatClient, run_download
from backend.articles.service import _safe_url
from wxdesk.demo import make_demo
from wxdesk.server import Application


def done(app, aid, task_id):
    for _ in range(200):
        task = app.harness.get(aid, task_id)
        if task['status'] != 'running':
            return task
        time.sleep(.01)
    raise AssertionError('article task did not finish')


def test_downloads_two_same_title_articles_without_collision_and_strips_active_html(tmp_path):
    class Client:
        def fetch_article(self, article):
            article.title = '同名文章'
            text = '<div id="js_content"><p onclick="alert(1)">正文</p><script>alert(2)</script></div>'
            return BeautifulSoup(text, 'html.parser'), text

    result = run_download(Client(), 'https://mp.weixin.qq.com/s/one\nhttps://mp.weixin.qq.com/s/two',
                          Options(tmp_path, formats=('html', 'txt', 'json'), images=False,
                                  delay=0, retries=0), threading.Event(), lambda _: None, 'links')
    files = list(tmp_path.rglob('*.html'))
    assert result['saved'] == 2 and len(files) == 2
    assert result['manifest'] and len(list(tmp_path.glob('*.csv'))) == 1
    assert all('onclick' not in path.read_text('utf-8') and '<script>' not in path.read_text('utf-8')
               for path in files)


def test_article_task_keeps_session_credentials_out_of_persistent_state(tmp_path, monkeypatch):
    make_demo(tmp_path)
    app = Application(state=tmp_path, base=tmp_path, demo=True)
    captured = {}

    def fake_download(client, source, options, stop, log, mode):
        captured.update(source=source, auth=client.auth, mode=mode, formats=options.formats)
        log('已保存：样本文章')
        return {'saved': 1, 'failed': 0, 'output': str(options.output), 'manifest': ''}

    monkeypatch.setattr('backend.articles.service.run_download', fake_download)
    try:
        aid = app.account()['id']
        task = app.article_service.start(aid, {'mode': 'single',
            'source': 'https://mp.weixin.qq.com/s/example?uin=secret-uin&key=secret-key&pass_ticket=secret-ticket',
            'formats': ['html', 'md'], 'output': str(tmp_path / 'articles')})
        result = done(app, aid, task['id'])
        assert result['status'] == 'done'
        assert captured['auth']['key'] == 'secret-key'
        assert captured['source'] == 'https://mp.weixin.qq.com/s/example'
        assert captured['formats'] == ('html', 'md')
        with app.harness.connect() as db:
            record = db.execute('SELECT payload FROM tasks WHERE id=?', (task['id'],)).fetchone()[0]
            event_log = json.dumps([dict(row) for row in db.execute('SELECT * FROM events WHERE task_id=?', (task['id'],))])
        assert 'secret-key' not in record + event_log
        assert 'secret-ticket' not in record + event_log
        assert result['result']['saved'] == 1
    finally:
        app.close()


def test_article_url_splits_session_parameters():
    url, auth = _safe_url('https://mp.weixin.qq.com/s/example?__biz=abc&uin=u&key=k#fragment')
    assert url == 'https://mp.weixin.qq.com/s/example?__biz=abc'
    assert auth == {'uin': 'u', 'key': 'k'}


def test_article_client_rejects_non_wechat_hosts_without_network():
    client = WeChatClient()
    try:
        try:
            client._get('https://127.0.0.1/private')
        except Exception as exc:
            assert '不受支持' in str(exc)
        else:
            raise AssertionError('local URL was not rejected')
    finally:
        client.session.close()


def test_builtin_article_status_works_before_wechat_account_is_detected(tmp_path):
    app = Application(state=tmp_path, base=tmp_path, demo=True)
    try:
        assert app.selected is None
        status = app.article_service.status('local-articles')
        assert status['built_in'] is True
        assert 'single' in status['modes']
    finally:
        app.close()
