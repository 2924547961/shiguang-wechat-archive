import hashlib
import struct

import pytest

from wxdesk import antirevoke as ar


def fixture_binary(monkeypatch, duplicate=False, executable=True):
    data = bytearray(2048)
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HH', data, 0x84, 0x8664, 1)
    struct.pack_into('<H', data, 0x94, 240)
    section = 0x80 + 24 + 240
    struct.pack_into('<III', data, section+12, 0x1000, 1024, 512)
    struct.pack_into('<I', data, section+36, 0x20000000 if executable else 0)
    original = bytes.fromhex('48 89 86 c8 01 00 00 4c 89 ad 08 02 00 00 4c 8d 05')
    data[600:617] = original
    if duplicate: data[700:717] = original
    monkeypatch.setattr(ar, 'SUPPORTED_SHA256', hashlib.sha256(data).hexdigest())
    return bytes(data), original


def test_file_offset_is_translated_to_executable_rva(monkeypatch):
    data, original = fixture_binary(monkeypatch)
    assert ar.binary_plan(data) == (0x1000 + 88, original)


@pytest.mark.parametrize('duplicate,executable', [(True, True), (False, False)])
def test_ambiguous_or_noncode_match_is_rejected(monkeypatch, duplicate, executable):
    data, _ = fixture_binary(monkeypatch, duplicate, executable)
    with pytest.raises(ValueError): ar.binary_plan(data)


def test_unknown_file_is_rejected_before_patch(monkeypatch):
    data, _ = fixture_binary(monkeypatch)
    with pytest.raises(ValueError): ar.binary_plan(data + b'changed')


def test_known_broken_version_cannot_be_enabled(monkeypatch):
    monkeypatch.setattr(ar, 'kernel', lambda: (_ for _ in ()).throw(AssertionError('must not open process')))
    with pytest.raises(ValueError, match='规则已停用'):
        ar.operate('enable')


def test_failed_flush_rolls_back_byte_and_protection(monkeypatch):
    original = bytes.fromhex('48 89 86 c8 01 00 00 4c 89 ad 08 02 00 00 4c 8d 05')
    memory = bytearray(original)
    protections = []

    class Kernel:
        def VirtualProtectEx(self, handle, address, size, protection, old):
            protections.append(protection); old._obj.value = 0x20
            return True

        def WriteProcessMemory(self, handle, address, data, size, written):
            memory[1] = data.raw[0]; written._obj.value = size
            return True

        def FlushInstructionCache(self, *args):
            return memory[1] == 0x89

    def check(ok):
        if not ok: raise OSError('simulated flush failure')

    monkeypatch.setattr(ar, 'check', check)
    monkeypatch.setattr(ar, 'read', lambda *args: bytes(memory))
    with pytest.raises(OSError):
        ar.change(Kernel(), 1, 100, original, original[:1]+b'\x29'+original[2:])
    assert bytes(memory) == original
    assert protections == [0x40, 0x20]
