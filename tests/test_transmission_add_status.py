"""
Transmission 添加响应的新增与重复任务区分，以及 SDK 传输复用回归。
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from transmission_rpc import Client

from app.modules.transmission.transmission import Transmission

_HASH = "0123456789abcdef0123456789abcdef01234567"
_MAGNET = f"magnet:?xt=urn:btih:{_HASH}"


def _response(result="success", arguments=None, status=200, headers=None):
    """
    构造原始 HTTP 响应，保留 SDK 会话握手及原始 RPC 参数解释。
    """
    return SimpleNamespace(
        status_code=status, headers=headers or {},
        text=json.dumps({"result": result, "arguments": arguments or {}}),
    )


@pytest.fixture
def client(monkeypatch):
    """
    仅替换 HTTP 发送边界，SDK 仍执行认证地址、超时和 409 会话重试逻辑。
    """
    monkeypatch.setattr(Client, "get_session", Mock())
    sdk = Client(host="transmission.invalid", path="/proxy/transmission/rpc", username="user", password="pass")
    sdk._http_session.post = Mock()
    server = Transmission.__new__(Transmission)
    server.trc = sdk
    server.reconnect = Mock()
    yield server
    sdk._http_session.close()


@pytest.mark.parametrize("key,duplicate", [("torrent-added", False), ("torrent-duplicate", True)])
@pytest.mark.parametrize("content", [b"torrent bytes", _MAGNET, _MAGNET.encode(), "https://tracker.invalid/file.torrent"])
def test_add_status_preserves_raw_duplicate_and_parameters(client, key, duplicate, content) -> None:
    """
    两种成功结果均返回原始身份，重复标志独立保留且添加参数完整传到 SDK 传输。
    """
    client.trc._http_session.post.return_value = _response(arguments={
        key: {"id": 1, "name": "Show", "hashString": _HASH},
    })

    torrent, already_exists = client.add_torrent_with_status(
        content, is_paused=True, download_dir="/downloads", labels=["MP"], cookie="session=value",
    )

    assert torrent.hashString == _HASH
    assert already_exists is duplicate
    request = client.trc._http_session.post.call_args
    assert request.args == ("http://user:pass@transmission.invalid:9091/proxy/transmission/rpc",)
    assert request.kwargs["timeout"] == client.trc.timeout
    arguments = request.kwargs["json"]["arguments"]
    assert {key: arguments[key] for key in ("paused", "download-dir", "labels", "cookies")} == {
        "paused": True, "download-dir": "/downloads", "labels": ["MP"], "cookies": "session=value",
    }
    if content == b"torrent bytes":
        assert base64.b64decode(arguments["metainfo"]) == content
        assert "filename" not in arguments
    else:
        assert arguments["filename"] == (content.decode() if isinstance(content, bytes) else content)
        assert "metainfo" not in arguments
    client.reconnect.assert_not_called()


def test_add_status_keeps_sdk_session_handshake(client) -> None:
    """
    409 响应仍由 SDK 更新会话并重试，新增任务标志在握手后保持准确。
    """
    client.trc._http_session.post.side_effect = [
        _response(status=409, headers={"x-transmission-session-id": "new-session"}),
        _response(arguments={"torrent-added": {"id": 1, "name": "Show", "hashString": _HASH}}),
    ]

    torrent, duplicate = client.add_torrent_with_status(_MAGNET)

    assert torrent.hashString == _HASH and duplicate is False
    calls = client.trc._http_session.post.call_args_list
    assert calls[0].kwargs["headers"] == {"x-transmission-session-id": "0"}
    assert calls[1].kwargs["headers"] == {"x-transmission-session-id": "new-session"}
    assert calls[1].kwargs["json"]["arguments"] == {"filename": _MAGNET, "paused": False}
    client.reconnect.assert_not_called()


@pytest.mark.parametrize("response", [
    _response(result="invalid torrent"), _response(),
    SimpleNamespace(status_code=200, headers={}, text="not json"),
    SimpleNamespace(status_code=200, headers={}, text='{"result":"success","arguments":null}'),
])
def test_add_status_rejects_invalid_responses(client, response) -> None:
    """
    无效、失败或缺任务字段的 RPC 响应只重连一次，不能被误报成新增成功。
    """
    client.trc._http_session.post.return_value = response

    assert client.add_torrent_with_status(_MAGNET) == (None, False)
    assert client.trc._http_session.post.call_count == 2
    client.reconnect.assert_called_once_with()


def test_add_status_reconnects_without_losing_duplicate_result(client) -> None:
    """
    添加响应丢失后重试得到重复结果，也只能作为已有任务交还宿主。
    """
    first_sdk = client.trc
    first_sdk._http_session.post.side_effect = ConnectionError("response lost")
    second_sdk = Mock()
    second_sdk._http_query.return_value = json.dumps({
        "result": "success", "arguments": {"torrent-duplicate": {"id": 1, "name": "Show", "hashString": _HASH}},
    })
    client.reconnect.side_effect = lambda: setattr(client, "trc", second_sdk)

    torrent, duplicate = client.add_torrent_with_status(_MAGNET)

    assert torrent.hashString == _HASH and duplicate is True
    assert first_sdk._http_session.post.call_count == 1
    second_sdk._http_query.assert_called_once()
    client.reconnect.assert_called_once_with()


def test_add_status_without_connection_does_not_claim_success() -> None:
    """
    未连接且没有地址时不发起添加或重连，缺少的任务对象不能当作新增成功。
    """
    server = Transmission.__new__(Transmission)
    server.trc = None
    server.reconnect = Mock()

    assert server.add_torrent_with_status(_MAGNET) == (None, False)
    server.reconnect.assert_not_called()
