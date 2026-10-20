"""
验证图片代理的地址校验与响应处理。
"""

import asyncio
import io
import ipaddress
import os
from collections.abc import Iterator, Mapping
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock, patch

import httpx2
import pytest
import requests
from PIL import Image

from app.adapters.network import http as http_adapter
from app.adapters.system.host import SystemUtils
from app.api.endpoints import system as system_endpoint
from app.application import image as image_service
from app.application.configuration import ChainRuntimeConfig
from app.application.image import (
    ImageHelper,
    ImageResponsePort,
    configure_image_ports,
    reset_image_ports,
)
from app.application.security.image import SiteImageDomainCache
from app.runtime.cache import AsyncFileCache, FileCache
from app.schemas.site import Site
from app.startup.composition.network import _ImageTransportAdapter


@pytest.fixture(autouse=True)
def configured_image_sites(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """
    隔离图片白名单的站点查询，避免其他用例的数据库状态影响结果。
    """
    query = AsyncMock()
    query.list.return_value = []
    monkeypatch.setattr(system_endpoint, "site_image_domains", SiteImageDomainCache())
    monkeypatch.setattr(system_endpoint, "get_configured_site_query_service", lambda: query)
    return query


@pytest.mark.parametrize("endpoint", ["proxy", "cache"])
@pytest.mark.parametrize(
    ("url", "address", "ranges", "expected"),
    [
        ("https://www.qingwapt.com/favicon.svg", "8.8.8.8", [], True),
        ("https://www.qingwapt.com/favicon.svg", "198.18.0.1", ["198.18.0.0/15"], True),
        ("https://www.qingwapt.com/favicon.svg", "198.18.0.1", [], False),
        ("https://www.qingwapt.com/favicon.svg", "127.0.0.1", ["198.18.0.0/15"], False),
        ("https://www.qingwapt.com.evil.example/favicon.svg", "8.8.8.8", [], False),
        ("https://evilqingwapt.com/favicon.svg", "8.8.8.8", [], False),
        ("https://qingwapt.com@evil.example/favicon.svg", "8.8.8.8", [], False),
        ("https://mirror.example:8443/favicon.svg", "8.8.8.8", [], True),
        ("https://mirror.example:9443/favicon.svg", "8.8.8.8", [], False),
    ],
)
def test_site_image_endpoints_preserve_dns_safety(
    configured_image_sites: AsyncMock,
    endpoint: str,
    url: str,
    address: str,
    ranges: list[str],
    expected: bool,
) -> None:
    """
    站点图片自动放行，两个接口均保留私网、相似域名及端口边界。
    """
    configured_image_sites.list.return_value = [
        Site(domain="qingwapt.com", url="https://mirror.example:8443/", is_active=False),
    ]
    image_helper = Mock()
    image_helper.async_fetch_image_with_mime_type = AsyncMock(return_value=(b"image", "image/png"))
    with patch.object(system_endpoint, "get_runtime_settings", return_value={
        "IMAGE_PROXY_ALLOWED_PRIVATE_RANGES": ranges,
    }), patch.object(system_endpoint, "_get_image_proxy_trusted_hosts", return_value=set()), patch.object(
        system_endpoint.SecurityUtils, "_hostname_addresses_async",
        new=AsyncMock(return_value=[ipaddress.ip_address(address)]),
    ), patch.object(system_endpoint, "ImageHelper", return_value=image_helper), patch(
        "app.application.security.url._emit_image_proxy_block_warning", new=AsyncMock(),
    ):
        if endpoint == "proxy":
            response = asyncio.run(system_endpoint.proxy_img(imgurl=url))
        else:
            response = asyncio.run(system_endpoint.cache_img(url=url))
    assert (response is not None) is expected
    assert image_helper.async_fetch_image_with_mime_type.await_count == int(expected)


def test_site_image_allowlist_tracks_configuration(configured_image_sites: AsyncMock) -> None:
    """
    站点改址、删除不残留白名单，非法地址不能打断其他图片请求。
    """
    configured_image_sites.list.side_effect = [
        [Site(domain="qingwapt.com", url="https://old.example/")],
        [Site(domain="qingwapt.com", url="https://new.example/")],
        [Site(url=url) for url in (None, "https://[", "file://host/path", "https://user@host/")],
    ]
    with patch.object(system_endpoint, "get_runtime_settings", return_value={}):
        assert asyncio.run(system_endpoint._get_image_proxy_allowed_domains()) == {
            "https://qingwapt.com", "https://old.example",
        }
        system_endpoint.site_image_domains.invalidate()
        assert asyncio.run(system_endpoint._get_image_proxy_allowed_domains()) == {
            "https://qingwapt.com", "https://new.example",
        }
        system_endpoint.site_image_domains.invalidate()
        assert asyncio.run(system_endpoint._get_image_proxy_allowed_domains()) == set()


class _FakeImageTransport:
    """
    返回测试响应并记录同步、异步图片请求。
    """

    def __init__(
        self,
        *,
        sync_response: Optional[ImageResponsePort] = None,
        async_response: Optional[ImageResponsePort] = None,
    ) -> None:
        """
        保存固定响应。
        """
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
        """
        记录同步请求并返回固定响应。
        """
        self.sync_calls.append((url, options))
        return self.sync_response

    async def async_get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """
        记录异步请求并返回固定响应。
        """
        self.async_calls.append((url, options))
        return self.async_response


class _FakeInternalAddress:
    """
    返回固定内部地址判断并记录 URL。
    """

    def __init__(self, internal: bool = False) -> None:
        """
        保存固定判断。
        """
        self.internal = internal
        self.calls: list[str] = []

    def is_internal(self, url: str) -> bool:
        """
        记录 URL 并返回固定判断。
        """
        self.calls.append(url)
        return self.internal


@pytest.fixture(autouse=True)
def restore_image_ports() -> Iterator[None]:
    """
    为每个用例装配无网络 fake，并在结束后恢复原端口。
    """
    previous = configure_image_ports(
        transport=_FakeImageTransport(),
        internal_address=_FakeInternalAddress(),
    )
    yield
    reset_image_ports(*previous)


def _image_bytes(
    image_format: str,
    trailing: bytes = b"",
    *,
    color: tuple[int, int, int] = (32, 96, 160),
) -> bytes:
    """
    生成离线图片载荷，用于验证内容识别和响应头。
    """
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=color).save(buffer, format=image_format)
    return buffer.getvalue() + trailing


@pytest.fixture
def isolated_image_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ImageHelper:
    """
    让真实同步、异步文件缓存共用临时目录，用例结束后恢复单例属性。
    """
    helper = ImageHelper()
    monkeypatch.setattr(helper, "file_cache", FileCache(base=tmp_path, local_only=True))
    monkeypatch.setattr(helper, "async_file_cache", AsyncFileCache(base=tmp_path, local_only=True))
    return helper


def _mock_image_http(monkeypatch, respond):
    """
    替换同步和异步真实传输层，保留 HTTP 请求头与组合根的实际处理。
    """
    def sync_request(method, url, **kwargs):
        """
        通过 requests 标准准备过程构造请求，随后交给内存响应器。
        """
        headers = {name: value for name, value in kwargs["headers"].items() if value is not None}
        prepared = requests.Request(method, url, headers=headers, cookies=kwargs.get("cookies")).prepare()
        return respond(httpx2.Request(method, url, headers=dict(prepared.headers)))

    monkeypatch.setattr(http_adapter.requests, "request", sync_request)
    monkeypatch.setattr(http_adapter, "_get_shared_async_transport", lambda **_: httpx2.MockTransport(respond))
    configure_image_ports(transport=_ImageTransportAdapter(), internal_address=_FakeInternalAddress())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_same_origin_redirect_preserves_cookie_scope_and_fetch_isolation(
    monkeypatch, isolated_image_cache, mode, scheme,
):
    """
    同源临时 Cookie 支持多头、路径、Secure、删除及域校验，且不跨图片请求积累。
    """
    observed = []
    content = _image_bytes("PNG")
    source = f"{scheme}://image.example/start"

    def respond(request):
        """
        设置多个作用域 Cookie，记录原地址和下一跳收到的有效凭据。
        """
        observed.append((request.url.path, request.headers.get("cookie", "")))
        if request.url.path == "/start":
            return httpx2.Response(302, headers=[
                ("location", "/poster/cover"), ("set-cookie", "auth=new; Path=/; Secure"),
                ("set-cookie", "remove=; Max-Age=0; Path=/"),
                ("set-cookie", "scoped=child; Path=/poster"), ("set-cookie", "scoped=root; Path=/"),
                ("set-cookie", "outside=hidden; Path=/else"),
                ("set-cookie", "wrong=hidden; Domain=unrelated.invalid; Path=/"),
                ("set-cookie", "expired=hidden; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/"),
            ])
        return httpx2.Response(200, content=content)

    async def allow(_target):
        """
        本用例只验证 Cookie；全部跳转均落在隔离的内存传输。
        """
        return True

    _mock_image_http(monkeypatch, respond)
    for user in ("one", "two"):
        cookies = f"session={user}; remove=old"
        if mode == "sync":
            result = isolated_image_cache.fetch_image_with_mime_type(
                source, proxy=False, use_cache=False, cookies=cookies, redirect_validator=lambda _url: True,
            )
        else:
            result = await isolated_image_cache.async_fetch_image_with_mime_type(
                source, proxy=False, use_cache=False, cookies=cookies, redirect_validator=allow,
            )
        assert result == (content, "image/png")
    assert len(observed) == 4
    for index, user in enumerate(("one", "two")):
        initial, redirected = observed[index * 2:index * 2 + 2]
        assert "auth=" not in initial[1] and f"session={user}" in initial[1]
        assert f"session={user}" in redirected[1] and "remove=" not in redirected[1]
        assert ("auth=new" in redirected[1]) is (scheme == "https")
        assert "wrong=" not in redirected[1] and "outside=" not in redirected[1] and "expired=" not in redirected[1]
        assert redirected[1].index("scoped=child") < redirected[1].index("scoped=root")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_cross_origin_redirect_drops_cookie_jar_and_does_not_restore_credentials(
    monkeypatch, isolated_image_cache, mode,
):
    """
    跨来源后返回初始来源也不恢复原 Cookie，服务器宽域 Cookie 同样不能越界。
    """
    observed = []
    content = _image_bytes("PNG")
    source = "https://img.doubanio.com/start"

    def respond(request):
        """
        模拟原来源、CDN、返回原来源三跳，每次都试图设置宽域 Cookie。
        """
        observed.append(dict(request.headers))
        target = "https://cdn.doubanio.com/hop" if request.url.path == "/start" else "https://img.doubanio.com/final"
        if request.url.path != "/final":
            return httpx2.Response(302, headers={
                "location": target, "set-cookie": "auth=private; Domain=.doubanio.com; Path=/",
            })
        return httpx2.Response(200, content=content)

    async def allow(_target):
        """
        跨来源授权与 Cookie 作用域分别验证，目标只使用内存传输。
        """
        return True

    _mock_image_http(monkeypatch, respond)
    if mode == "sync":
        result = isolated_image_cache.fetch_image_with_mime_type(
            source, proxy=False, use_cache=False, cookies="session=private", redirect_validator=lambda _url: True,
        )
    else:
        result = await isolated_image_cache.async_fetch_image_with_mime_type(
            source, proxy=False, use_cache=False, cookies="session=private", redirect_validator=allow,
        )
    assert result == (content, "image/png")
    assert observed[0]["cookie"] == "session=private" and observed[0]["referer"]
    assert len(observed) == 3
    for headers in observed[1:]:
        assert "cookie" not in headers and "referer" not in headers


def test_custom_image_transport_cookie_header_fallback(isolated_image_cache):
    """
    旧图片端口只有普通响应头时，仍可在同源跳转时设置临时 Cookie。
    """
    content = _image_bytes("PNG")
    transport = _FakeImageTransport()
    transport.get = Mock(side_effect=[
        SimpleNamespace(status_code=302, headers={"Location": "/poster", "Set-Cookie": "auth=new; Path=/"}),
        SimpleNamespace(status_code=200, content=content, headers={}),
    ])
    configure_image_ports(transport=transport, internal_address=_FakeInternalAddress())
    result = isolated_image_cache.fetch_image_with_mime_type(
        "https://image.example/start", proxy=False, use_cache=False, redirect_validator=lambda _url: True,
    )
    assert result == (content, "image/png")
    assert transport.get.call_args.kwargs["options"]["cookie_header"] == "auth=new"


@pytest.mark.asyncio
@pytest.mark.parametrize("target,address,expected", [
    ("http://127.0.0.1/private", "127.0.0.1", False),
    ("http://[::1]/private", "::1", False),
    ("http://169.254.169.254/latest/meta-data", "169.254.169.254", False),
    ("https://private.example/poster", "192.168.1.10", False),
    ("file:///etc/passwd", "8.8.8.8", False),
    ("https://user:password@cdn.example/poster", "8.8.8.8", False),
    ("https://cdn.example/poster", "8.8.8.8", True),
    ("/relative-poster", "8.8.8.8", True),
])
async def test_real_image_transport_checks_redirect_before_request(
    monkeypatch: pytest.MonkeyPatch, isolated_image_cache: ImageHelper,
    target: str, address: str, expected: bool,
) -> None:
    """
    端点经过真实 HTTP Adapter 时，也不能先自动跟随跳转再进行校验。
    """
    source = "https://image.tmdb.org/redirect"
    requests = []
    content = _image_bytes("PNG")

    def respond(request):
        """
        模拟允许站点的跳转和目标响应，记录真正到达传输层的请求。
        """
        requests.append(request)
        if str(request.url) == source:
            return httpx2.Response(302, headers={"location": target})
        return httpx2.Response(200, content=content)

    async def addresses(host):
        """
        初始主机是公网，跳转主机使用参数指定的解析结果。
        """
        return [ipaddress.ip_address("8.8.8.8" if host == "image.tmdb.org" else address)]

    monkeypatch.setattr(http_adapter, "_get_shared_async_transport", lambda **_: httpx2.MockTransport(respond))
    monkeypatch.setattr(system_endpoint.SecurityUtils, "_hostname_addresses_async", addresses)
    monkeypatch.setattr(system_endpoint, "_get_image_proxy_trusted_hosts", lambda: set())
    monkeypatch.setattr(system_endpoint, "get_runtime_settings", lambda: {})
    configure_image_ports(transport=_ImageTransportAdapter(), internal_address=_FakeInternalAddress())

    response = await system_endpoint.fetch_image(
        source, proxy=False, allowed_domains={"image.tmdb.org"}, cookies="session=private-cookie",
    )

    assert (response is not None) is expected
    assert len(requests) == (2 if expected else 1)
    if expected:
        assert response.body == content
        if target.startswith("https://cdn"):
            assert "cookie" not in requests[1].headers
        else:
            assert requests[1].headers["cookie"] == "session=private-cookie"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,trusted,expected", [
    (False, False, False), (True, False, True), (False, True, True),
])
async def test_redirect_private_exceptions_apply_only_to_configured_targets(
    monkeypatch: pytest.MonkeyPatch, configured: bool, trusted: bool, expected: bool,
) -> None:
    """
    允许私网段不能扩大为任意跳转授权，明确配置的主机才可使用该例外。
    """
    monkeypatch.setattr(
        system_endpoint.SecurityUtils, "_hostname_addresses_async",
        AsyncMock(return_value=[ipaddress.ip_address("198.18.0.2")]),
    )
    result = await system_endpoint.SecurityUtils.is_safe_image_redirect_url_async(
        "https://cdn.example/poster",
        {"cdn.example"} if configured else {"image.tmdb.org"},
        allowed_private_ranges=["198.18.0.0/15"],
        trusted_hosts={"cdn.example"} if trusted else set(),
    )
    assert result is expected


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://private.example/poster"])
def test_sync_image_redirect_refuses_private_target(
    monkeypatch: pytest.MonkeyPatch, isolated_image_cache: ImageHelper, target: str,
) -> None:
    """
    同步图片路径同样必须在第二次请求之前校验 DNS。
    """
    transport = _FakeImageTransport(sync_response=SimpleNamespace(
        status_code=302, content=b"", headers={"location": target},
    ))
    configure_image_ports(transport=transport, internal_address=_FakeInternalAddress())
    monkeypatch.setattr(
        system_endpoint.SecurityUtils, "_hostname_addresses", lambda _: [ipaddress.ip_address("127.0.0.1")],
    )
    assert isolated_image_cache.fetch_image("https://image.tmdb.org/redirect", use_cache=False) is None
    assert len(transport.sync_calls) == 1
    assert transport.sync_calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_redirects_are_bounded_and_every_hop_is_checked(isolated_image_cache: ImageHelper) -> None:
    """
    环形跳转有限终止，每次目标均先经过安全回调。
    """
    transport = _FakeImageTransport(async_response=SimpleNamespace(
        status_code=302, content=b"", headers={"location": "/loop"},
    ))
    validator = AsyncMock(return_value=True)
    configure_image_ports(transport=transport, internal_address=_FakeInternalAddress())
    assert await isolated_image_cache.async_fetch_image_with_mime_type(
        "https://image.tmdb.org/redirect", use_cache=False, redirect_validator=validator,
    ) is None
    assert len(transport.async_calls) == 6
    assert validator.await_count == 5


class _RedirectThenImageTransport:
    """
    首个请求返回跨来源重定向，其后返回图片内容并记录请求地址。
    """

    def __init__(self, location: str, content: bytes) -> None:
        """
        保存重定向目标与最终图片内容。

        :param location: 首个请求返回的 Location 头
        :param content: 跟随重定向后返回的图片字节
        """
        self.location = location
        self.content = content
        self.sync_urls: list[str] = []
        self.async_urls: list[str] = []

    def get(self, url: str, *, options: Mapping[str, Any]) -> Optional[ImageResponsePort]:
        """
        记录同步请求地址，首个请求返回重定向，其余返回图片。

        :param url: 本次请求地址
        :param options: 传输参数，用于确认重定向未被自动跟随
        :return: 构造的图片响应契约
        """
        self.sync_urls.append(url)
        if len(self.sync_urls) == 1:
            return SimpleNamespace(status_code=307, content=b"", headers={"location": self.location})
        return SimpleNamespace(status_code=200, content=self.content, headers={})

    async def async_get(self, url: str, *, options: Mapping[str, Any]) -> Optional[ImageResponsePort]:
        """
        记录异步请求地址，首个请求返回重定向，其余返回图片。

        :param url: 本次请求地址
        :param options: 传输参数，用于确认重定向未被自动跟随
        :return: 构造的图片响应契约
        """
        self.async_urls.append(url)
        if len(self.async_urls) == 1:
            return SimpleNamespace(status_code=307, content=b"", headers={"location": self.location})
        return SimpleNamespace(status_code=200, content=self.content, headers={})


@pytest.mark.parametrize(("ranges", "expected"), [([], False), (["198.18.0.0/15"], True)])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_default_image_redirect_honours_configured_private_ranges(
    monkeypatch: pytest.MonkeyPatch,
    isolated_image_cache: ImageHelper,
    mode: str,
    ranges: list[str],
    expected: bool,
) -> None:
    """
    内部调用方不传 redirect_validator 时，默认重定向校验沿用图片代理配置的
    非公网放行网段；未配置时仍按默认拒绝非公网解析结果。
    """
    content = _image_bytes("PNG")
    transport = _RedirectThenImageTransport("https://cdn.example/poster.png", content)
    configure_image_ports(transport=transport, internal_address=_FakeInternalAddress())
    monkeypatch.setattr(
        image_service,
        "get_chain_runtime_config_snapshot",
        lambda: ChainRuntimeConfig(
            media_extensions=(),
            image_proxy_allowed_private_ranges=tuple(ranges),
        ),
    )
    monkeypatch.setattr(
        system_endpoint.SecurityUtils, "_hostname_addresses", lambda _: [ipaddress.ip_address("198.18.0.2")],
    )
    monkeypatch.setattr(
        system_endpoint.SecurityUtils, "_hostname_addresses_async",
        AsyncMock(return_value=[ipaddress.ip_address("198.18.0.2")]),
    )
    url = "https://image.tmdb.org/redirect"
    if mode == "sync":
        result = isolated_image_cache.fetch_image(url, use_cache=False)
        requested = transport.sync_urls
    else:
        result = asyncio.run(isolated_image_cache.async_fetch_image(url, use_cache=False))
        requested = transport.async_urls
    assert (result == content) is expected
    assert requested == ([url] if not expected else [url, "https://cdn.example/poster.png"])


def test_bangumi_image_proxy_domain_is_added_to_allowlist() -> None:
    """
    启用 Bangumi 图片代理时，应允许其配置主机通过通用图片端点。
    """
    with patch.object(
        system_endpoint,
        "get_runtime_settings",
        return_value={
            "BANGUMI_PROXY_ENABLE": True,
            "BANGUMI_IMAGE_DOMAIN": "https://image-proxy.example/?url=",
            "SECURITY_IMAGE_DOMAINS": ["lain.bgm.tv"],
        },
    ):
        assert asyncio.run(system_endpoint._get_image_proxy_allowed_domains()) == {
            "lain.bgm.tv",
            "https://image-proxy.example",
        }


@pytest.mark.parametrize("key", [
    "TMDB_IMAGE_DOMAIN", "MUSIC_COVER_PROXY", "BANGUMI_IMAGE_DOMAIN",
    "WALLPAPER_IMAGE_URL", "CUSTOMIZE_WALLPAPER_API_URL",
])
@pytest.mark.parametrize("enabled", [False, True])
def test_custom_image_proxy_settings_are_live(
    configured_image_sites: AsyncMock, key: str, enabled: bool,
) -> None:
    """
    图片代理配置即时更新；Bangumi 关闭时不放行其自定义代理。
    """
    settings = {key: "https://proxy.example:8443/base/?url=", "BANGUMI_PROXY_ENABLE": enabled}
    with patch.object(system_endpoint, "get_runtime_settings", return_value=settings):
        expected = key != "BANGUMI_IMAGE_DOMAIN" or enabled
        assert ("https://proxy.example:8443" in asyncio.run(
            system_endpoint._get_image_proxy_allowed_domains(),
        )) is expected
        settings[key] = "https://new.example/path/"
        domains = asyncio.run(system_endpoint._get_image_proxy_allowed_domains())
        assert "https://proxy.example:8443" not in domains
        assert ("https://new.example" in domains) is expected
    assert configured_image_sites.list.await_count == 1


def test_image_proxy_trusts_enabled_mediaserver_hosts() -> None:
    """
    已启用媒体服务器的 host / play_host 应被规范化为受信主机。
    """
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
    """
    媒体服务器配置读取失败时不信任任何主机，也不抛出异常。
    """
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
    """
    未配置私网网段时，仅受信媒体服务器主机的内网图片可通过代理。
    """
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
    """
    fetch_image 应把媒体服务器受信主机传给图片 URL 安全校验。
    """
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
    """
    图片 MIME 类型依据 Pillow 检测出的实际格式确定。
    """
    assert ImageHelper.get_image_mime_type(_image_bytes(image_format)) == expected_mime


def test_get_image_mime_type_rejects_non_image_pillow_mime():
    """
    Pillow 返回非图片 MIME 类型时拒绝该内容。
    """
    assert ImageHelper.get_image_mime_type(_image_bytes("EPS")) is None


def test_get_image_mime_type_rejects_scriptable_svg_mime():
    """
    即使图像可解析，也拒绝可执行脚本的 SVG MIME 类型。
    """
    with patch.dict(Image.MIME, {"PNG": "image/svg+xml"}):
        assert ImageHelper.get_image_mime_type(_image_bytes("PNG")) is None


def test_fetch_image_with_mime_type_only_reads_cached_format_header():
    """
    同步缓存命中时只读取已缓存格式头，避免重复完整验证图片。
    """
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
    """
    同步网络图片内容只完整验证一次，并返回实际 MIME 类型。
    """
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


def test_fetch_image_retries_transient_upstream_failure():
    """
    图片上游短暂返回 500 时应重试并缓存成功响应。
    """
    content = _image_bytes("PNG")
    image_helper = ImageHelper()
    transport = _FakeImageTransport()
    transport.get = Mock(
        side_effect=[
            Mock(status_code=500, content=b""),
            Mock(status_code=200, content=content),
        ],
    )
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
    ):
        result = image_helper.fetch_image_with_mime_type(
            "https://coverartarchive.org/release/example/front-500"
        )

    assert result == (content, "image/png")
    assert transport.get.call_count == 2


@pytest.mark.asyncio
async def test_async_fetch_image_retries_transient_upstream_failure():
    """
    异步图片上游短暂返回 500 时应重试并缓存成功响应。
    """
    content = _image_bytes("PNG")
    image_helper = ImageHelper()
    transport = _FakeImageTransport()
    transport.async_get = AsyncMock(
        side_effect=[
            Mock(status_code=500, content=b""),
            Mock(status_code=200, content=content),
        ],
    )
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
    ):
        result = await image_helper.async_fetch_image_with_mime_type(
            "https://coverartarchive.org/release/example/front-500"
        )

    assert result == (content, "image/png")
    assert transport.async_get.await_count == 2


def test_async_fetch_image_with_mime_type_only_reads_cached_format_header():
    """
    异步缓存命中时只读取已缓存格式头，避免重复完整验证图片。
    """
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
    """
    异步网络图片内容只完整验证一次，并返回实际 MIME 类型。
    """
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    ("first_url", "second_url"),
    [
        ("https://one.example/cover.jpg", "https://two.example/cover.jpg"),
        ("https://images.example/cover.jpg?id=one", "https://images.example/cover.jpg?id=two"),
        ("https://images.example:8443/cover.jpg", "https://images.example:9443/cover.jpg"),
        ("http://images.example/cover.jpg", "https://images.example/cover.jpg"),
    ],
    ids=["host", "query", "port", "scheme"],
)
async def test_image_cache_separates_url_identity(
    isolated_image_cache, mode, first_url, second_url,
):
    """
    不同来源的同路径图片独立缓存，重复请求仍返回各自内容且不再访问网络。
    """
    first_content = _image_bytes("PNG", color=(255, 0, 0))
    second_content = _image_bytes("PNG", color=(0, 0, 255))
    response = Mock(status_code=200, content=first_content)
    transport = _FakeImageTransport(sync_response=response, async_response=response)
    configure_image_ports(transport=transport, internal_address=_FakeInternalAddress())

    async def fetch(url):
        if mode == "sync":
            return isolated_image_cache.fetch_image_with_mime_type(url)
        return await isolated_image_cache.async_fetch_image_with_mime_type(url)

    assert await fetch(first_url) == (first_content, "image/png")
    response.content = second_content
    assert await fetch(second_url) == (second_content, "image/png")
    assert await fetch(first_url) == (first_content, "image/png")
    assert await fetch(second_url) == (second_content, "image/png")
    calls = transport.sync_calls + transport.async_calls
    assert [url for url, _options in calls] == [first_url, second_url]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_image_cache_does_not_reuse_legacy_path_entry(isolated_image_cache, mode):
    """
    旧路径缓存无法确认来源，升级后应重新获取对应 URL 的图片。
    """
    stale_content = _image_bytes("PNG", color=(255, 0, 0))
    fresh_content = _image_bytes("PNG", color=(0, 0, 255))
    isolated_image_cache.file_cache.set("covers/cover.png", stale_content, region="images")
    response = Mock(status_code=200, content=fresh_content)
    transport = _FakeImageTransport(sync_response=response, async_response=response)
    configure_image_ports(transport=transport, internal_address=_FakeInternalAddress())
    url = "https://images.example/covers/cover.png"

    if mode == "sync":
        result = isolated_image_cache.fetch_image_with_mime_type(url)
    else:
        result = await isolated_image_cache.async_fetch_image_with_mime_type(url)

    assert result == (fresh_content, "image/png")
    assert len(transport.sync_calls + transport.async_calls) == 1
    assert isolated_image_cache.file_cache.get(
        ImageHelper._prepare_cache_path(url), region="images",
    ) == fresh_content


def test_image_cache_paths_remain_subject_to_age_cleanup(isolated_image_cache, tmp_path):
    """
    新旧缓存均留在 images 目录，既有清理能删除过期文件并保留新文件。
    """
    old_key = ImageHelper._prepare_cache_path("https://images.example/old.png")
    fresh_key = ImageHelper._prepare_cache_path("https://images.example/fresh.png")
    for key in (old_key, "legacy/cover.jpg", fresh_key):
        isolated_image_cache.file_cache.set(key, _image_bytes("PNG"), region="images")
    for key in (old_key, "legacy/cover.jpg"):
        os.utime(tmp_path / "images" / key, (1, 1))

    SystemUtils.clear(tmp_path / "images", days=7)

    assert isolated_image_cache.file_cache.get(old_key, region="images") is None
    assert isolated_image_cache.file_cache.get("legacy/cover.jpg", region="images") is None
    assert isolated_image_cache.file_cache.get(fresh_key, region="images") == _image_bytes("PNG")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/",
        "https://example.com/?param=value",
    ],
)
def test_prepare_cache_path_uses_url_hash_for_urls_without_path(url: str) -> None:
    """
    没有有效路径的图片 URL 应生成稳定合法的缓存文件名。
    """
    expected_hash = sha256(url.encode()).hexdigest()[:16]

    assert ImageHelper._prepare_cache_path(url) == f"proxy_{expected_hash}.jpg"


def test_image_request_uses_internal_address_port_for_proxy_decision(monkeypatch):
    """
    自动代理策略必须通过注入端口判断内部地址。
    """
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
    """
    图片代理不信任可能触发主动内容的 URL 后缀，使用实际内容类型。
    """
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
    """
    图片内容未通过验证时，代理不生成可用响应。
    """
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
    """
    图片代理的未修改响应也包含禁止 MIME 嗅探的响应头。
    """
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
