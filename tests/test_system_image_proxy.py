import asyncio
import io
import ipaddress
from collections.abc import Iterator, Mapping
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest
from PIL import Image

from app.api.endpoints import system as system_endpoint
from app.application import image as image_service
from app.application.image import (
    ImageHelper,
    ImageResponsePort,
    configure_image_ports,
    reset_image_ports,
)


class _FakeImageTransport:
    """返回测试响应并记录同步、异步图片请求。"""

    def __init__(
        self,
        *,
        sync_response: Optional[ImageResponsePort] = None,
        async_response: Optional[ImageResponsePort] = None,
    ) -> None:
        """保存固定响应。"""
        self.sync_response = sync_response
        self.async_response = async_response
        self.sync_calls: list[tuple[str, Mapping[str, Any]]] = []
        self.async_calls: list[tuple[str, Mapping[str, Any]]] = []

    def get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """记录同步请求并返回固定响应。"""
        self.sync_calls.append((url, options))
        return self.sync_response

    async def async_get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """记录异步请求并返回固定响应。"""
        self.async_calls.append((url, options))
        return self.async_response


class _FakeInternalAddress:
    """返回固定内部地址判断并记录 URL。"""

    def __init__(self, internal: bool = False) -> None:
        """保存固定判断。"""
        self.internal = internal
        self.calls: list[str] = []

    def is_internal(self, url: str) -> bool:
        """记录 URL 并返回固定判断。"""
        self.calls.append(url)
        return self.internal


@pytest.fixture(autouse=True)
def restore_image_ports() -> Iterator[None]:
    """为每个用例装配无网络 fake，并在结束后恢复原端口。"""
    previous = configure_image_ports(
        transport=_FakeImageTransport(),
        internal_address=_FakeInternalAddress(),
    )
    yield
    reset_image_ports(*previous)


def _image_bytes(image_format: str, trailing: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=(32, 96, 160)).save(buffer, format=image_format)
    return buffer.getvalue() + trailing


def test_bangumi_image_proxy_domain_is_added_to_allowlist() -> None:
    """启用 Bangumi 图片代理时，应允许其配置主机通过通用图片端点。"""
    with patch.object(
        system_endpoint,
        "get_runtime_settings",
        return_value={
            "BANGUMI_PROXY_ENABLE": True,
            "BANGUMI_IMAGE_DOMAIN": "https://image-proxy.example/?url=",
            "SECURITY_IMAGE_DOMAINS": ["lain.bgm.tv"],
        },
    ):
        assert system_endpoint._get_image_proxy_allowed_domains() == {
            "lain.bgm.tv",
            "image-proxy.example",
        }


def test_image_proxy_trusts_enabled_mediaserver_hosts() -> None:
    """已启用媒体服务器的 host / play_host 应被规范化为受信主机。"""
    confs = [
        SimpleNamespace(config={"host": "192.168.1.10:8096", "play_host": "https://Emby.Example.com"}),
        SimpleNamespace(config={"host": "http://jellyfin.lan"}),
        SimpleNamespace(config=None),
    ]
    with patch.object(system_endpoint, "get_mediaserver_configs", return_value=confs) as getter:
        assert system_endpoint._get_image_proxy_trusted_hosts() == {
            "192.168.1.10:8096",
            "emby.example.com",
            "jellyfin.lan",
        }
    getter.assert_called_once_with()


def test_image_proxy_trusted_hosts_empty_when_lookup_fails() -> None:
    """媒体服务器配置读取失败时不信任任何主机，也不抛出异常。"""
    with patch.object(system_endpoint, "get_mediaserver_configs", side_effect=RuntimeError("db down")):
        assert system_endpoint._get_image_proxy_trusted_hosts() == set()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.168.1.10:8096/Items/1/Images/Primary", True),
        ("http://jellyfin.lan:8096/Items/1/Images/Primary", True),
        ("http://192.168.1.10:22/poster.jpg", False),
        ("http://192.168.1.11:8096/Items/1/Images/Primary", False),
    ],
)
def test_image_proxy_allows_lan_url_only_for_trusted_mediaserver_host(url: str, expected: bool) -> None:
    """未配置私网网段时，仅受信媒体服务器主机的内网图片可通过代理。"""
    trusted = {"192.168.1.10:8096", "jellyfin.lan"}
    with patch.object(
        system_endpoint.SecurityUtils,
        "_hostname_addresses_async",
        new=AsyncMock(return_value=[ipaddress.ip_address("192.168.1.10")]),
    ), patch("app.application.security.url._emit_image_proxy_block_warning", new=AsyncMock()):
        allowed = asyncio.run(
            system_endpoint.SecurityUtils.is_safe_image_url_async(
                url,
                {"image.tmdb.org"},
                allowed_private_ranges=[],
                trusted_hosts=trusted,
            )
        )
    assert allowed is expected


def test_fetch_image_passes_mediaserver_trusted_hosts() -> None:
    """fetch_image 应把媒体服务器受信主机传给图片 URL 安全校验。"""
    image_helper = Mock()
    image_helper.async_fetch_image_with_mime_type = AsyncMock(return_value=(_image_bytes("PNG"), "image/png"))
    is_safe = AsyncMock(return_value=True)
    with patch.object(system_endpoint.SecurityUtils, "is_safe_image_url_async", new=is_safe), patch.object(
        system_endpoint, "ImageHelper", return_value=image_helper
    ), patch.object(system_endpoint, "_get_image_proxy_trusted_hosts", return_value={"192.168.1.10:8096"}):
        response = asyncio.run(
            system_endpoint.fetch_image(
                url="http://192.168.1.10:8096/Items/1/Images/Primary",
                allowed_domains={"image.tmdb.org"},
            )
        )
    assert response is not None
    assert is_safe.await_args.kwargs["trusted_hosts"] == {"192.168.1.10:8096"}


@pytest.mark.parametrize(
    ("image_format", "expected_mime"),
    [
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
        ("PCX", "image/x-pcx"),
        ("PPM", "image/x-portable-anymap"),
    ],
)
def test_get_image_mime_type_uses_pillow_detected_format(
    image_format: str,
    expected_mime: str,
):
    assert ImageHelper.get_image_mime_type(_image_bytes(image_format)) == expected_mime


def test_get_image_mime_type_rejects_non_image_pillow_mime():
    assert ImageHelper.get_image_mime_type(_image_bytes("EPS")) is None


def test_get_image_mime_type_rejects_scriptable_svg_mime():
    with patch.dict(Image.MIME, {"PNG": "image/svg+xml"}):
        assert ImageHelper.get_image_mime_type(_image_bytes("PNG")) is None


def test_fetch_image_with_mime_type_only_reads_cached_format_header():
    content = _image_bytes("PNG")
    image_helper = ImageHelper()

    with patch.object(
        image_helper.file_cache,
        "get",
        return_value=content,
    ), patch.object(
        image_helper,
        "get_image_mime_type",
        return_value="image/png",
    ) as get_mime_type:
        result = image_helper.fetch_image_with_mime_type(
            "https://images.example/wallpaper.png"
        )

    assert result == (content, "image/png")
    get_mime_type.assert_called_once_with(content, verify=False)


def test_fetch_image_with_mime_type_validates_network_content_once():
    content = _image_bytes("PNG")
    image_helper = ImageHelper()
    response = Mock(status_code=200, content=content)
    transport = _FakeImageTransport(sync_response=response)
    configure_image_ports(
        transport=transport,
        internal_address=_FakeInternalAddress(),
    )

    with patch.object(
        image_helper.file_cache,
        "get",
        return_value=None,
    ), patch.object(
        image_helper.file_cache,
        "set",
    ), patch.object(
        image_helper,
        "get_image_mime_type",
        return_value="image/png",
    ) as get_mime_type:
        result = image_helper.fetch_image_with_mime_type(
            "https://images.example/wallpaper.png"
        )

    assert result == (content, "image/png")
    get_mime_type.assert_called_once_with(content)
    assert transport.sync_calls[0][0] == "https://images.example/wallpaper.png"


def test_async_fetch_image_with_mime_type_only_reads_cached_format_header():
    content = _image_bytes("PNG")
    image_helper = ImageHelper()

    with patch.object(
        image_helper.async_file_cache,
        "get",
        new=AsyncMock(return_value=content),
    ), patch.object(
        image_helper,
        "get_image_mime_type",
        return_value="image/png",
    ) as get_mime_type:
        result = asyncio.run(
            image_helper.async_fetch_image_with_mime_type(
                "https://images.example/wallpaper.png"
            )
        )

    assert result == (content, "image/png")
    get_mime_type.assert_called_once_with(content, verify=False)


def test_async_fetch_image_with_mime_type_validates_network_content_once():
    content = _image_bytes("PNG")
    image_helper = ImageHelper()
    response = Mock(status_code=200, content=content)
    transport = _FakeImageTransport(async_response=response)
    configure_image_ports(
        transport=transport,
        internal_address=_FakeInternalAddress(),
    )

    with patch.object(
        image_helper.async_file_cache,
        "get",
        new=AsyncMock(return_value=None),
    ), patch.object(
        image_helper.async_file_cache,
        "set",
        new=AsyncMock(),
    ), patch.object(
        image_helper,
        "get_image_mime_type",
        return_value="image/png",
    ) as get_mime_type:
        result = asyncio.run(
            image_helper.async_fetch_image_with_mime_type(
                "https://images.example/wallpaper.png"
            )
        )

    assert result == (content, "image/png")
    get_mime_type.assert_called_once_with(content)
    assert transport.async_calls[0][0] == "https://images.example/wallpaper.png"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/",
        "https://example.com/?param=value",
    ],
)
def test_prepare_cache_path_uses_url_hash_for_urls_without_path(url: str) -> None:
    """没有有效路径的图片 URL 应生成稳定合法的缓存文件名。"""
    expected_hash = sha256(url.encode()).hexdigest()[:16]

    assert ImageHelper._prepare_cache_path(url) == f"proxy_{expected_hash}.jpg"


def test_image_request_uses_internal_address_port_for_proxy_decision(monkeypatch):
    """自动代理策略必须通过注入端口判断内部地址。"""
    internal_address = _FakeInternalAddress(internal=True)
    configure_image_ports(
        transport=_FakeImageTransport(),
        internal_address=internal_address,
    )
    monkeypatch.setattr(
        image_service,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(
            proxy={"https": "http://proxy.example"},
            normal_user_agent="MoviePilot/Test",
        ),
    )

    params = ImageHelper._get_request_params(
        "https://images.example/wallpaper.png",
        proxy=None,
        cookies=None,
    )

    assert params["proxies"] is None
    assert internal_address.calls == ["https://images.example/wallpaper.png"]


def test_fetch_image_does_not_trust_active_url_suffix():
    content = _image_bytes("PNG", b"<script>window.xss = true</script>")
    image_helper = Mock()
    image_helper.async_fetch_image_with_mime_type = AsyncMock(
        return_value=(content, "image/png")
    )

    with patch.object(
        system_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        new=AsyncMock(return_value=True),
    ), patch.object(system_endpoint, "ImageHelper", return_value=image_helper):
        response = asyncio.run(
            system_endpoint.fetch_image(
                url="https://images.example/wallpaper.html",
                allowed_domains={"images.example"},
            )
        )

    assert response is not None
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.body == content


def test_fetch_image_rejects_unverified_content():
    image_helper = Mock()
    image_helper.async_fetch_image_with_mime_type = AsyncMock(return_value=None)

    with patch.object(
        system_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        new=AsyncMock(return_value=True),
    ), patch.object(system_endpoint, "ImageHelper", return_value=image_helper):
        response = asyncio.run(
            system_endpoint.fetch_image(
                url="https://images.example/wallpaper.png",
                allowed_domains={"images.example"},
            )
        )

    assert response is None


def test_fetch_image_adds_nosniff_to_not_modified_response():
    content = _image_bytes("JPEG")
    image_helper = Mock()
    image_helper.async_fetch_image_with_mime_type = AsyncMock(
        return_value=(content, "image/jpeg")
    )
    etag = system_endpoint.HashUtils.md5(content)

    with patch.object(
        system_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        new=AsyncMock(return_value=True),
    ), patch.object(system_endpoint, "ImageHelper", return_value=image_helper):
        response = asyncio.run(
            system_endpoint.fetch_image(
                url="https://images.example/wallpaper.jpg",
                if_none_match=etag,
                allowed_domains={"images.example"},
            )
        )

    assert response is not None
    assert response.status_code == 304
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
