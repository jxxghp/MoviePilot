"""
通过原生 XML-RPC 编解码与离线 socket 验证 rTorrent SCGI 连接。
"""

import xmlrpc.client
from collections import deque
from unittest.mock import Mock

import pytest

from app.modules.rtorrent.rtorrent import Rtorrent, SCGITransport


class _Socket:
    """
    保存实际发送字节，并分块返回预先构造的 SCGI 响应。
    """

    def __init__(self, response):
        """
        把响应拆分为多个读取块，记录关闭次数。
        """
        self.chunks = deque([response[:17], response[17:], b""])
        self.sent = b""
        self.closed = 0

    def sendall(self, data):
        """
        记录 netstring 头与 XML-RPC 请求体。
        """
        self.sent += data

    def recv(self, _size):
        """
        按连接读取顺序提供响应块。
        """
        return self.chunks.popleft()

    def close(self):
        """
        记录连接释放，便于验证成功及异常路径。
        """
        self.closed += 1


def _install_socket(monkeypatch, response):
    """
    替换唯一网络边界，继续使用真实 ServerProxy 和 XML-RPC 解析器。
    """
    sock = _Socket(response)
    connect = Mock(return_value=sock)
    monkeypatch.setattr("app.modules.rtorrent.rtorrent.socket.create_connection", connect)
    return sock, connect


def _assert_version_request(wire):
    """
    独立解析 SCGI netstring 与 XML-RPC，确认没有发送 HTTP 请求。
    """
    size, rest = wire.split(b":", 1)
    length = int(size)
    assert rest[length:length + 1] == b","
    header_fields = rest[:length].split(b"\0")
    assert header_fields[-1] == b""
    headers = dict(zip(header_fields[::2], header_fields[1::2]))
    body = rest[length + 1:]
    assert headers == {
        b"CONTENT_LENGTH": str(len(body)).encode(),
        b"SCGI": b"1",
        b"REQUEST_METHOD": b"POST",
        b"REQUEST_URI": b"/RPC2",
    }
    params, method = xmlrpc.client.loads(body)
    assert params == ()
    assert method == "system.client_version"


@pytest.mark.parametrize("header", [b"", b"Status: 200 OK\r\nContent-Type: text/xml\r\n\r\n"])
@pytest.mark.parametrize("host, port, address", [
    ("scgi://127.0.0.1", 5050, ("127.0.0.1", 5050)),
    ("scgi://[::1]:5051", None, ("::1", 5051)),
    ("scgi://localhost", None, ("localhost", 5000)),
])
def test_scgi_login_uses_native_proxy_and_scgi_wire(monkeypatch, header, host, port, address):
    """
    SCGI 登录支持显式端口、IPv6 和默认端口，原生代理通过自定义传输完成握手。
    """
    payload = xmlrpc.client.dumps(("test-version",), methodresponse=True).encode()
    sock, connect = _install_socket(monkeypatch, header + payload)

    client = Rtorrent(host=host, port=port)

    assert not client.is_inactive()
    connect.assert_called_once_with(address, timeout=60)
    assert sock.closed == 1
    _assert_version_request(sock.sent)


def test_scgi_transport_initializes_native_parser_verbose_state(monkeypatch):
    """
    直接调用传输时也能使用标准库响应解析器，不依赖不存在的 verbose 初值。
    """
    payload = xmlrpc.client.dumps(("test-version",), methodresponse=True).encode()
    sock, _ = _install_socket(monkeypatch, payload)
    request = xmlrpc.client.dumps((), methodname="system.client_version").encode()

    assert SCGITransport().single_request("localhost", "/RPC2", request) == ("test-version",)
    assert sock.closed == 1
    _assert_version_request(sock.sent)


@pytest.mark.parametrize("operation", ["sendall", "recv"])
def test_scgi_transport_closes_socket_on_io_failure(monkeypatch, operation):
    """
    发送或读取失败时保留原异常，并确保已建立的连接释放。
    """
    sock, _ = _install_socket(monkeypatch, b"unused")
    error = OSError("offline I/O failure")
    monkeypatch.setattr(sock, operation, Mock(side_effect=error))

    with pytest.raises(OSError) as caught:
        SCGITransport().single_request("localhost", "/RPC2", b"request")
    assert caught.value is error
    assert sock.closed == 1


def test_scgi_fault_keeps_disconnected_state(monkeypatch):
    """
    服务器返回真实 XML-RPC Fault 时登录仍失败，socket 已被释放。
    """
    payload = xmlrpc.client.dumps(xmlrpc.client.Fault(42, "offline fault")).encode()
    sock, connect = _install_socket(monkeypatch, payload)

    client = Rtorrent(host="scgi://localhost")

    assert client.is_inactive()
    connect.assert_called_once_with(("localhost", 5000), timeout=60)
    assert sock.closed == 1
    _assert_version_request(sock.sent)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_login_keeps_standard_transport(monkeypatch, scheme):
    """
    已有 HTTP/HTTPS 登录继续使用标准传输，保留认证信息和 RPC 路径。
    """
    requests = []

    def single_request(transport, host, handler, body, verbose=False):
        """
        在标准传输执行网络 I/O 前捕获真实代理组装出的请求。
        """
        requests.append((transport, host, handler, body, verbose))
        return ("test-version",)

    monkeypatch.setattr(xmlrpc.client.Transport, "single_request", single_request)
    client = Rtorrent(host=f"{scheme}://example.invalid:8080/RPC2", username="user", password="test-pass")

    assert not client.is_inactive()
    assert len(requests) == 1
    transport, host, handler, body, verbose = requests[0]
    assert not isinstance(transport, SCGITransport)
    assert isinstance(transport, xmlrpc.client.SafeTransport) is (scheme == "https")
    assert host == "user:test-pass@example.invalid:8080"
    assert handler == "/RPC2"
    assert verbose is False
    assert xmlrpc.client.loads(body) == ((), "system.client_version")


@pytest.mark.parametrize("host, port", [(None, None), ("", None), (None, 5000), ("", 5000)])
def test_incomplete_configuration_keeps_client_disabled(monkeypatch, host, port):
    """
    未配置主机时状态检查和重连保持禁用，不尝试建立网络代理。
    """
    proxy = Mock(side_effect=AssertionError("unconfigured client must not connect"))
    monkeypatch.setattr("app.modules.rtorrent.rtorrent.xmlrpc.client.ServerProxy", proxy)

    client = Rtorrent(host=host, port=port)

    assert client.is_inactive() is False
    client.reconnect()
    assert client.is_inactive() is False
    assert client.get_torrents() == ([], True)
    proxy.assert_not_called()
