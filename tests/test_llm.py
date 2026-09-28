import io
import json

import pytest

from wxdesk.llm import complete, endpoint, load_key, save_key


def test_endpoint_validation():
    assert endpoint('https://api.deepseek.com') == 'https://api.deepseek.com/chat/completions'
    assert endpoint('http://localhost:8080/v1') == 'http://localhost:8080/v1/chat/completions'
    with pytest.raises(ValueError):
        endpoint('http://example.com')


def test_key_is_protected_and_reply_uses_chat_completion(tmp_path, monkeypatch):
    path = tmp_path / 'secret.json'
    save_key(path, 'account-a', 'fake-key-for-test')
    assert 'fake-key-for-test' not in path.read_text('utf-8')
    assert load_key(path, 'account-a') == 'fake-key-for-test'
    assert load_key(path, 'account-b') == ''

    calls = []
    class Response(io.BytesIO):
        headers = {}
    def fake_open(request, timeout):
        calls.append((request.full_url, request.get_header('Authorization'), json.loads(request.data)))
        return Response(b'{"choices":[{"message":{"content":"\xe6\x94\xb6\xe5\x88\xb0\xe4\xba\x86"}}]}')
    monkeypatch.setattr('urllib.request.urlopen', fake_open)
    assert complete('https://api.deepseek.com', 'fake-key-for-test', 'deepseek-chat', '简短回复', '你好', 'Alice') == '收到了'
    assert calls[0][0] == 'https://api.deepseek.com/chat/completions'
    assert calls[0][1] == 'Bearer fake-key-for-test'
    assert calls[0][2]['messages'][1]['role'] == 'user'
