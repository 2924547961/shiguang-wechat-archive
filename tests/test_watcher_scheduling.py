import threading
from pathlib import Path

import pytest

from wxdesk.server import Application
from wxdesk.automation import Automation


def test_readonly_export_does_not_block_chat_sync(tmp_path, monkeypatch):
    """A slow export must not hold up the next incremental message pass."""
    app = Application.__new__(Application)
    app.state = Path(tmp_path)
    app.demo = False
    app.settings = {'include_media': False, 'force_keys': False, 'output_dir': str(tmp_path)}
    app.accounts = [{'id': 'one', 'active': True, 'account': 'wxid_one'}]
    app.selected = 'one'
    app.lock = threading.RLock()
    app.jobs = {}
    app.cancel_events = {}

    export_started = threading.Event()
    release_export = threading.Event()
    sync_started = threading.Event()
    release_sync = threading.Event()

    def slow_export(*args):
        export_started.set()
        assert release_export.wait(3)
        return {'ok': True}

    monkeypatch.setattr('wxdesk.server.export_data', slow_export)
    def slow_sync(*args, **kwargs):
        sync_started.set()
        assert release_sync.wait(3)
        return {'new_messages': 1}

    monkeypatch.setattr('wxdesk.server.sync_account', slow_sync)
    try:
        export = app.start_job('export', {'account_id': 'one'})
        assert export_started.wait(2)
        sync = app.start_job('quick_sync', {'account_id': 'one', 'automatic': True})
        assert sync_started.wait(2)
        with pytest.raises(ValueError, match='归档写入'):
            app.start_job('moments_quick', {'account_id': 'one'})
        release_sync.set()
        app.jobs[sync['id']]['thread'].join(2)
        assert app.jobs[sync['id']]['status'] == 'done'
    finally:
        release_sync.set()
        release_export.set()
        app.jobs[export['id']]['thread'].join(2)


def test_scheduled_send_does_not_pause_incoming_poll(tmp_path, monkeypatch):
    class App:
        state = tmp_path
        demo = False

    automation = Automation(App())
    automation.config['enabled'] = True
    scheduled_started = threading.Event()
    release_scheduled = threading.Event()
    polled = threading.Event()

    def slow_scheduled():
        scheduled_started.set()
        release_scheduled.wait(3)

    monkeypatch.setattr(automation, '_run_scheduled', slow_scheduled)
    monkeypatch.setattr(automation, 'poll_once', polled.set)
    try:
        automation.start()
        assert scheduled_started.wait(2)
        assert polled.wait(2)
    finally:
        release_scheduled.set()
        automation.stop()
