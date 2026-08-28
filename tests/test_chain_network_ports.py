"""Chain 同步网络窄端口与响应资源所有权测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.download.ports as download_ports
from app.chain import message as message_module
from app.chain import scraping as scraping_module
from app.chain import system as system_module
from app.startup.initializers import network as network_initializer


@pytest.fixture(autouse=True)
def isolate_chain_network_ports():
    """每个用例从未装配状态开始，并恢复全局测试运行时。"""
    download_http, download_archive = download_ports._download_ports_snapshot()
    message_http = message_module._message_http_snapshot()
    scraping_http = scraping_module._scraping_http_snapshot()
    system_http, system_environment = system_module._system_ports_snapshot()
    network_initializer.reset_chain_network_ports()
    yield
    download_ports.configure_download_ports(
        http=download_http,
        archive=download_archive,
    )
    message_module.configure_message_http_port(message_http)
    scraping_module.configure_scraping_http_port(scraping_http)
    system_module.configure_system_ports(
        http=system_http,
        environment=system_environment,
    )


class _FakeResponse:
    """记录同步响应是否在成功和异常路径被关闭。"""

    def __init__(self, payload=None, *, content: bytes = b"payload") -> None:
        """保存测试载荷并初始化关闭计数。"""
        self.payload = payload
        self.content = content
        self.headers = {"Content-Type": "application/octet-stream"}
        self.closed = 0

    def json(self):
        """返回预设 JSON 载荷或抛出预设异常。"""
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self) -> None:
        """记录一次响应释放。"""
        self.closed += 1


class _FakeHttp:
    """为各 Chain 返回同一个可观测同步响应。"""

    def __init__(self, response: _FakeResponse) -> None:
        """保存后续 GET 返回的响应。"""
        self.response = response

    def get(self, _url: str, **_kwargs):
        """返回预设响应。"""
        return self.response


def test_reset_makes_all_chain_ports_fail_explicitly() -> None:
    """未装配时四个 Chain 都应明确失败，不得隐式构造 Adapter。"""
    with pytest.raises(RuntimeError, match="下载链技术端口"):
        download_ports._download_ports_snapshot()
    with pytest.raises(RuntimeError, match="消息附件 HTTP 端口"):
        message_module._message_http_snapshot()
    with pytest.raises(RuntimeError, match="刮削 HTTP 端口"):
        scraping_module._scraping_http_snapshot()
    with pytest.raises(RuntimeError, match="系统链技术端口"):
        system_module._system_ports_snapshot()


def test_initializer_supports_repeated_init_and_reset() -> None:
    """重复启动应替换完整端口快照，重复关闭应保持幂等。"""
    network_initializer.init_chain_network_ports()
    first = (
        download_ports._download_ports_snapshot(),
        message_module._message_http_snapshot(),
        scraping_module._scraping_http_snapshot(),
        system_module._system_ports_snapshot(),
    )
    network_initializer.init_chain_network_ports()
    second = (
        download_ports._download_ports_snapshot(),
        message_module._message_http_snapshot(),
        scraping_module._scraping_http_snapshot(),
        system_module._system_ports_snapshot(),
    )
    assert all(old != new for old, new in zip(first, second))

    network_initializer.reset_chain_network_ports()
    network_initializer.reset_chain_network_ports()
    with pytest.raises(RuntimeError):
        message_module._message_http_snapshot()


def test_initializer_rolls_back_partial_configuration(monkeypatch) -> None:
    """任一端口装配异常后不得留下可用的半套运行时。"""
    monkeypatch.setattr(
        network_initializer,
        "configure_scraping_http_port",
        Mock(side_effect=RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        network_initializer.init_chain_network_ports()
    with pytest.raises(RuntimeError):
        download_ports._download_ports_snapshot()
    with pytest.raises(RuntimeError):
        message_module._message_http_snapshot()
    with pytest.raises(RuntimeError):
        system_module._system_ports_snapshot()


def test_message_http_response_is_closed_after_copy() -> None:
    """消息附件读取应复制字节与响应头后立即释放连接。"""
    response = _FakeResponse(content=b"audio")
    message_module.configure_message_http_port(_FakeHttp(response))

    content, headers = message_module._read_message_http("https://example.test/a")

    assert content == b"audio"
    assert headers["Content-Type"] == "application/octet-stream"
    assert response.closed == 1


def test_system_release_response_is_closed_on_json_error(monkeypatch) -> None:
    """发布列表解析异常也必须释放响应，并维持 None 失败返回。"""
    response = _FakeResponse(ValueError("invalid json"))
    environment = SimpleNamespace(is_docker=lambda: False)
    system_module.configure_system_ports(http=_FakeHttp(response), environment=environment)
    monkeypatch.setattr(
        system_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(proxy=None, github_headers={}),
    )

    result = SystemChainProbe.get_server_release_version()

    assert result is None
    assert response.closed == 1


class SystemChainProbe:
    """只暴露系统 Chain 的私有发布查询，避免构造完整 Chain 运行时。"""

    get_server_release_version = staticmethod(
        system_module.SystemChain._SystemChain__get_server_release_version
    )
