"""网络应用端口组合根契约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.startup.composition import network as network_composition


@pytest.mark.asyncio
async def test_network_test_transport_enforces_safe_http_options(monkeypatch) -> None:
    """网络探测适配器固定证书校验、超时和手动重定向策略。"""
    captured = {}
    response = SimpleNamespace(status_code=200, headers={}, text="ok")

    class _RequestUtils:
        """捕获网络探测适配器使用的 HTTP 参数。"""

        def __init__(self, **kwargs) -> None:
            """记录通用请求工具的构造参数。"""
            captured["options"] = kwargs

        async def get_res(self, url, allow_redirects=True):
            """记录请求地址和重定向开关并返回固定响应。"""
            captured["url"] = url
            captured["allow_redirects"] = allow_redirects
            return response

    monkeypatch.setattr(network_composition, "AsyncRequestUtils", _RequestUtils)

    result = await network_composition._NetworkTestTransportAdapter().get(
        "https://example.com/health",
        proxy="http://proxy.example:7890",
        headers={"Authorization": "Bearer test"},
        user_agent="MoviePilot-Test",
    )

    assert result is response
    assert captured == {
        "url": "https://example.com/health",
        "allow_redirects": False,
        "options": {
            "proxies": "http://proxy.example:7890",
            "headers": {"Authorization": "Bearer test"},
            "timeout": 10,
            "ua": "MoviePilot-Test",
            "verify": True,
            "follow_redirects": False,
        },
    }


def test_image_transport_forwards_sync_request_options(monkeypatch) -> None:
    """同步图片适配器完整透传请求参数，并单独传递目标 URL。"""
    response = object()
    request = MagicMock()
    request.get_res.return_value = response
    request_factory = MagicMock(return_value=request)
    monkeypatch.setattr(network_composition, "RequestUtils", request_factory)
    options = {
        "timeout": 15,
        "proxies": "http://proxy.example:7890",
        "headers": {"Accept": "image/*"},
    }

    result = network_composition._ImageTransportAdapter().get(
        "https://images.example/poster.jpg",
        options=options,
    )

    assert result is response
    request_factory.assert_called_once_with(**options)
    request.get_res.assert_called_once_with(
        url="https://images.example/poster.jpg",
    )


@pytest.mark.asyncio
async def test_image_transport_forwards_async_request_options(monkeypatch) -> None:
    """异步图片适配器完整透传请求参数，并单独传递目标 URL。"""
    response = object()
    request = MagicMock()
    request.get_res = AsyncMock(return_value=response)
    request_factory = MagicMock(return_value=request)
    monkeypatch.setattr(network_composition, "AsyncRequestUtils", request_factory)
    options = {
        "timeout": 20,
        "proxies": None,
        "verify": True,
    }

    result = await network_composition._ImageTransportAdapter().async_get(
        "https://images.example/backdrop.webp",
        options=options,
    )

    assert result is response
    request_factory.assert_called_once_with(**options)
    request.get_res.assert_awaited_once_with(
        url="https://images.example/backdrop.webp",
    )


def test_internal_address_adapter_delegates_to_ip_utility(monkeypatch) -> None:
    """内部地址判断必须原样委托通用 IP 工具。"""
    probe = MagicMock(return_value=True)
    monkeypatch.setattr(network_composition.IpUtils, "is_internal", probe)

    assert network_composition._InternalAddressAdapter().is_internal("http://192.168.1.10/image.jpg") is True
    probe.assert_called_once_with("http://192.168.1.10/image.jpg")


def test_network_port_composition_does_not_read_settings_early(monkeypatch) -> None:
    """端口装配只登记延迟 reader，不得在配置发布前读取 RuntimeSettings。"""
    get_runtime_settings = MagicMock(side_effect=AssertionError("装配阶段不得读取 RuntimeSettings"))
    configured = {}
    monkeypatch.setattr(
        network_composition,
        "get_runtime_settings",
        get_runtime_settings,
    )
    monkeypatch.setattr(
        network_composition,
        "configure_network_test_service",
        lambda service: configured.update(network=service),
    )
    monkeypatch.setattr(
        network_composition,
        "configure_image_ports",
        lambda **ports: configured.update(image=ports),
    )
    monkeypatch.setattr(
        network_composition,
        "configure_message_ingress_port",
        lambda port: configured.update(message=port),
    )

    network_composition.configure_application_network_ports()

    get_runtime_settings.assert_not_called()
    assert set(configured) == {"network", "image", "message"}
    runtime_settings = MagicMock()
    runtime_settings.get.return_value = "configured"
    get_runtime_settings.side_effect = None
    get_runtime_settings.return_value = runtime_settings
    assert configured["network"]._settings("PROXY", None) == "configured"
    runtime_settings.get.assert_called_once_with("PROXY", None)
