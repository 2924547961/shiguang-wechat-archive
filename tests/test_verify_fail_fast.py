import json
from types import SimpleNamespace

import wxdump


def test_encrypted_header_salt_is_first_sixteen_bytes(tmp_path):
    path = tmp_path / 'encrypted.db'
    salt = bytes(range(1, 17))
    path.write_bytes(salt + b'ciphertext' * 8)
    assert wxdump.file_salt_hex(path) == salt.hex()
    path.write_bytes(b'SQLite format 3\0' + b'\0' * 64)
    assert wxdump.file_salt_hex(path) is None


def test_repeated_rejected_candidates_stop_before_other_databases(tmp_path, monkeypatch):
    dbdir = tmp_path / 'db_storage'
    dbdir.mkdir()
    for name in ('message_0.db', 'contact.db', 'emoticon.db'):
        p = dbdir / name
        p.write_bytes(b'not-a-database')
    raw = tmp_path / 'keys.raw.json'
    raw.write_text(json.dumps({'account': 'sample', 'dbdir': str(dbdir), 'candidates': {
        name: {'salt': None, 'keys': [{'key': 'abc'}]}
        for name in ('message_0.db', 'contact.db', 'emoticon.db')}}), encoding='utf-8')
    visited = []
    monkeypatch.setattr(wxdump, 'verify_one', lambda path, key: (visited.append(path) or False, 'file is not a database'))
    monkeypatch.setattr(wxdump.time, 'sleep', lambda seconds: None)
    output = []
    monkeypatch.setattr(wxdump, 'out', output.append)
    assert wxdump.cmd_verify(SimpleNamespace(raw=str(raw), out=str(tmp_path/'verified.json'))) == 1
    assert len(visited) == 4
    assert 'message_0.db' in visited[0]
    assert any('已停止重复验证' in line for line in output)


def test_process_probe_requires_key_that_opens_selected_account(tmp_path, monkeypatch):
    database = tmp_path / 'message_0.db'
    database.write_bytes(b'dummy')
    checked = []
    def verify(path, key):
        checked.append(key)
        return (key == 'right', {})
    monkeypatch.setattr(wxdump, 'verify_one', verify)
    assert wxdump._probe_payload({'globalKeys': {'wrong': {}, 'right': {}}}, str(tmp_path), ['message_0.db']) == ('message_0.db', 'right')
    assert checked == ['wrong', 'right']
    assert wxdump._probe_payload({'globalKeys': {'wrong': {}}}, str(tmp_path), ['message_0.db']) is None
