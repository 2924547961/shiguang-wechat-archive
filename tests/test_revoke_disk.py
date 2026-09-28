import json

import pytest

from wxdesk import revoke_disk
from wxdesk.common import atomic_json


def test_close_then_patch_and_restore_with_backup(tmp_path, monkeypatch):
    folder = tmp_path / '4.1.15.13'
    folder.mkdir()
    target = folder / 'Weixin.dll'
    original = b'prefix\x48\x89\x86ABCD\x4c\x89\xadsuffix'
    target.write_bytes(original)
    rules = tmp_path / 'patch.json'
    rules.write_text(json.dumps({'PatchVersion': 123, 'Apps': {'Weixin': {
        'FileCommonModifyInfos': {'Weixin.dll': [{
            'StartVersion': '4.1.12.0', 'EndVersion': '',
            'ReplacePatterns': [{'Category': '防撤回',
                                 'Search': [72, 137, 134, 63, 63, 63, 63, 76, 137, 173],
                                 'Replace': [72, 41, 134, 63, 63, 63, 63, 76, 137, 173]}]}]}}}}), encoding='utf-8')
    monkeypatch.setattr(revoke_disk, 'RULE_FILE', rules)
    running = [object()]
    monkeypatch.setattr(revoke_disk, 'running_weixin', lambda: running)
    state = tmp_path / 'state'
    state.mkdir()
    service = revoke_disk.DiskPatcher(state)
    atomic_json(service.path, {'path': str(target)})
    service.queue('patched')
    assert service.apply_pending() is False
    assert target.read_bytes() == original
    running.clear()
    assert service.apply_pending() is True
    assert target.read_bytes() != original
    assert (state / 'weixin-4.1.15.13.original.bak').read_bytes() == original
    assert service.status()['file_state'] == 'patched'
    assert service.status()['enabled'] is False  # File state is not proof of recall protection.
    service.queue('original')
    assert service.apply_pending() is True
    assert target.read_bytes() == original


def test_ambiguous_patch_rule_is_rejected(tmp_path, monkeypatch):
    folder = tmp_path / '4.1.15.13'
    folder.mkdir()
    target = folder / 'Weixin.dll'
    target.write_bytes(b'\x48\x89\x86ABCD\x48\x89\x86ABCD')
    rules = tmp_path / 'patch.json'
    rules.write_text(json.dumps({'PatchVersion': 1, 'Apps': {'Weixin': {
        'FileCommonModifyInfos': {'Weixin.dll': [{
            'StartVersion': '4.1.12.0', 'EndVersion': '',
            'ReplacePatterns': [{'Category': '防撤回', 'Search': [72, 137, 134, 63, 63, 63, 63],
                                 'Replace': [72, 41, 134, 63, 63, 63, 63]}]}]}}}}), encoding='utf-8')
    monkeypatch.setattr(revoke_disk, 'RULE_FILE', rules)
    with pytest.raises(ValueError, match='不唯一'):
        revoke_disk.inspect(target)


def test_multi_open_and_recall_patch_can_be_toggled_independently(tmp_path, monkeypatch):
    folder = tmp_path / '4.1.15.13'
    folder.mkdir()
    target = folder / 'Weixin.dll'
    original = b'prefix ABCD middle WXYZ suffix'
    target.write_bytes(original)
    rules = tmp_path / 'patch.json'
    rules.write_text(json.dumps({'PatchVersion': 1, 'Apps': {'Weixin': {
        'FileCommonModifyInfos': {'Weixin.dll': [{
            'StartVersion': '4.1.12.0', 'EndVersion': '', 'ReplacePatterns': [
                {'Category': '防撤回', 'Search': list(b'ABCD'), 'Replace': list(b'AbCD')},
                {'Category': '多开', 'Search': list(b'WXYZ'), 'Replace': list(b'WxYZ')},
            ]}]}}}}), encoding='utf-8')
    monkeypatch.setattr(revoke_disk, 'RULE_FILE', rules)
    monkeypatch.setattr(revoke_disk, 'running_weixin', lambda: [])
    service = revoke_disk.DiskPatcher(tmp_path / 'state')
    service.state.mkdir()
    atomic_json(service.path, {'path': str(target)})
    service.queue('patched')
    service.queue_multi('patched')
    assert service.apply_pending() is True
    assert service.apply_pending() is True
    assert service.status()['file_state'] == 'patched'
    assert service.multi_status()['file_state'] == 'patched'
    service.queue('original')
    assert service.apply_pending() is True
    assert service.multi_status()['file_state'] == 'patched'
    service.queue_multi('original')
    assert service.apply_pending() is True
    assert target.read_bytes() == original
