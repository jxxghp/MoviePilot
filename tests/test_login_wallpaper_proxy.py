import asyncio
import io
import threading
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from PIL import Image

from app.api.endpoints import login as login_endpoint
from app.helper import image as image_module
from app.helper.image import ImageHelper


def test_wallpapers_preserves_default_external_contract(monkeypatch):
    urls = [
        "https://images.example/one.jpg",
        "/relative/two.jpg",
        "https://images.example/one.jpg",
    ]
    helper = Mock()
    helper.get_wallpapers.return_value = urls
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))

    result = login_endpoint.wallpapers(same_origin=False)

    assert result == urls
    helper.get_wallpapers.assert_called_once_with()


def test_same_origin_wallpapers_preserve_order_duplicates_and_relative_urls(
    monkeypatch,
):
    urls = [
        "https://images.example/one.jpg",
        "/relative/two.jpg",
        "https://images.example/one.jpg",
    ]
    helper = Mock()
    helper.get_wallpapers.return_value = urls
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))
    monkeypatch.setattr(login_endpoint.settings, "WALLPAPER", "customize")

    result = login_endpoint.wallpapers(same_origin=True)

    assert len(result) == len(urls)
    assert result[1] == urls[1]
    assert result[0] == result[2]
    signed_source = parse_qs(urlparse(result[0]).query)["url"][0]
    assert (
        login_endpoint.SecurityUtils.verify_signed_url(
            signed_source,
            purpose=login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE,
        )
        == urls[0]
    )


def test_same_origin_wallpapers_do_not_truncate_custom_sources(monkeypatch):
    urls = [f"https://images.example/{index}.jpg" for index in range(12)]
    helper = Mock()
    helper.get_wallpapers.return_value = urls
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))
    monkeypatch.setattr(login_endpoint.settings, "WALLPAPER", "customize")

    result = login_endpoint.wallpapers(same_origin=True)

    assert len(result) == len(urls)
    decoded_sources = [
        login_endpoint.SecurityUtils.verify_signed_url(
            parse_qs(urlparse(item).query)["url"][0],
            purpose=login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE,
        )
        for item in result
    ]
    assert decoded_sources == urls


def test_same_origin_wallpapers_convert_network_path_references(monkeypatch):
    """`//host/path` 会被浏览器解析成跨源地址，同源模式必须一并代理。"""
    urls = ["//cdn.example/wallpaper.jpg", "/relative/two.jpg"]
    helper = Mock()
    helper.get_wallpapers.return_value = urls
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))
    monkeypatch.setattr(login_endpoint.settings, "WALLPAPER", "customize")

    result = login_endpoint.wallpapers(same_origin=True)

    assert result[1] == urls[1]
    assert urlparse(result[0]).netloc == ""
    signed_source = parse_qs(urlparse(result[0]).query)["url"][0]
    assert (
        login_endpoint.SecurityUtils.verify_signed_url(
            signed_source,
            purpose=login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE,
        )
        == "https://cdn.example/wallpaper.jpg"
    )


def test_url_origin_rejects_malformed_port():
    assert login_endpoint._url_origin("https://images.example:invalid/one.jpg") is None


@pytest.mark.asyncio
async def test_wallpaper_image_uses_signed_public_source_without_credentials(
    monkeypatch,
):
    source_url = "https://images.example/one.jpg"
    signed_url = login_endpoint.SecurityUtils.sign_url(
        source_url,
        purpose=login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE,
    )
    image_helper = Mock()
    image_helper.async_fetch_image_guarded = AsyncMock(return_value=b"image-bytes")
    safety_check = AsyncMock(return_value=True)
    monkeypatch.setattr(login_endpoint, "ImageHelper", Mock(return_value=image_helper))
    monkeypatch.setattr(
        login_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        safety_check,
    )

    response = await login_endpoint.wallpaper_image(signed_url)

    assert response.status_code == 200
    assert response.body == b"image-bytes"
    assert response.headers["cache-control"] == "public, max-age=86400"
    fetch_options = image_helper.async_fetch_image_guarded.await_args.kwargs
    assert fetch_options["url"] == source_url
    assert fetch_options["use_cache"] is True
    assert fetch_options["max_bytes"] == 32 * 1024 * 1024
    assert (
        fetch_options["redirect_policy"]
        == login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE
    )
    assert "cookies" not in fetch_options
    assert "max_pixels" not in fetch_options
    assert await fetch_options["redirect_validator"](
        "https://cdn.example/redirected.jpg"
    )
    assert safety_check.await_count == 2


@pytest.mark.asyncio
async def test_wallpaper_image_serves_etag_and_conditional_requests(monkeypatch):
    """条件请求命中时只返回 304，标准引号/弱标签/标签列表/`*` 都要匹配。"""
    source_url = "https://images.example/one.jpg"
    signed_url = login_endpoint.SecurityUtils.sign_url(
        source_url,
        purpose=login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE,
    )
    image_helper = Mock()
    image_helper.async_fetch_image_guarded = AsyncMock(return_value=b"image-bytes")
    monkeypatch.setattr(login_endpoint, "ImageHelper", Mock(return_value=image_helper))
    monkeypatch.setattr(
        login_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        AsyncMock(return_value=True),
    )

    response = await login_endpoint.wallpaper_image(signed_url)
    etag = response.headers["etag"]
    assert etag == f'"{login_endpoint.HashUtils.md5(b"image-bytes")}"'

    for candidate in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        conditional = await login_endpoint.wallpaper_image(
            signed_url, if_none_match=candidate
        )
        assert conditional.status_code == 304
        assert conditional.headers["etag"] == etag
        assert not conditional.body

    stale = await login_endpoint.wallpaper_image(
        signed_url, if_none_match='"stale-digest"'
    )
    assert stale.status_code == 200
    assert stale.body == b"image-bytes"


@pytest.mark.asyncio
async def test_wallpaper_image_rejects_modified_or_unsafe_public_source(monkeypatch):
    source_url = "https://images.example/one.jpg"
    signed_url = login_endpoint.SecurityUtils.sign_url(
        source_url,
        purpose=login_endpoint._LOGIN_WALLPAPER_PUBLIC_PURPOSE,
    )

    with pytest.raises(HTTPException) as invalid_signature:
        await login_endpoint.wallpaper_image(f"{signed_url}modified")
    assert invalid_signature.value.status_code == 404

    monkeypatch.setattr(
        login_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        AsyncMock(return_value=False),
    )
    with pytest.raises(HTTPException) as unsafe_source:
        await login_endpoint.wallpaper_image(signed_url)
    assert unsafe_source.value.status_code == 404


@pytest.mark.asyncio
async def test_media_wallpaper_only_inherits_private_authority_on_same_origin(
    monkeypatch,
):
    source_url = "http://mediaserver.local:8096/image.jpg"
    signed_url = login_endpoint.SecurityUtils.sign_url(
        source_url,
        purpose=login_endpoint._LOGIN_WALLPAPER_MEDIA_PURPOSE,
    )
    image_helper = Mock()
    image_helper.async_fetch_image_guarded = AsyncMock(return_value=b"image-bytes")
    safety_check = AsyncMock(return_value=True)
    monkeypatch.setattr(login_endpoint, "ImageHelper", Mock(return_value=image_helper))
    monkeypatch.setattr(
        login_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        safety_check,
    )

    await login_endpoint.wallpaper_image(signed_url)

    redirect_validator = (
        image_helper.async_fetch_image_guarded.await_args.kwargs["redirect_validator"]
    )
    assert await redirect_validator(
        "http://mediaserver.local:8096/redirected.jpg"
    )
    assert safety_check.await_count == 0
    assert await redirect_validator("https://cdn.example/redirected.jpg")
    assert safety_check.await_count == 1
    # 私网授权范围写入合并键，公共来源的并发请求不会共享这次抓取
    fetch_options = image_helper.async_fetch_image_guarded.await_args.kwargs
    assert fetch_options["redirect_policy"] == (
        f"{login_endpoint._LOGIN_WALLPAPER_MEDIA_PURPOSE}"
        ":http://mediaserver.local:8096"
    )


class _StreamResponse:
    """提供受限图片抓取测试所需的最小流式响应合同。"""

    def __init__(self, status_code, *, headers=None, chunks=None, gate=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._gate = gate

    async def aiter_bytes(self):
        if self._gate:
            await self._gate.wait()
        for chunk in self._chunks:
            yield chunk


class _StreamContext:
    """模拟 AsyncRequestUtils.get_stream 返回的异步上下文。"""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_):
        return False


def _guarded_image_helper():
    helper = object.__new__(ImageHelper)
    helper.async_file_cache = Mock()
    helper.async_file_cache.get = AsyncMock(return_value=None)
    helper.async_file_cache.delete = AsyncMock()
    helper.async_file_cache.set = AsyncMock()
    helper._guarded_fetch_tasks = {}
    helper._guarded_fetch_tasks_lock = threading.Lock()
    return helper


def _png_bytes():
    content = io.BytesIO()
    Image.new("RGB", (1, 1), color="black").save(content, format="PNG")
    return content.getvalue()


@pytest.mark.asyncio
async def test_guarded_image_fetch_rejects_redirect_before_second_request(
    monkeypatch,
):
    helper = _guarded_image_helper()
    request = Mock()
    request.get_stream.return_value = _StreamContext(
        _StreamResponse(302, headers={"location": "http://127.0.0.1/private.jpg"})
    )
    request_factory = Mock(return_value=request)
    redirect_validator = AsyncMock(return_value=False)
    monkeypatch.setattr(image_module, "AsyncRequestUtils", request_factory)

    content = await helper.async_fetch_image_guarded(
        "https://images.example/one.jpg",
        redirect_validator=redirect_validator,
        redirect_policy="public",
        max_bytes=1024,
        use_cache=False,
    )

    assert content is None
    redirect_validator.assert_awaited_once_with("http://127.0.0.1/private.jpg")
    request_factory.assert_called_once()


@pytest.mark.asyncio
async def test_guarded_image_fetch_stops_chunked_response_over_byte_limit(
    monkeypatch,
):
    helper = _guarded_image_helper()
    request = Mock()
    request.get_stream.return_value = _StreamContext(
        _StreamResponse(200, chunks=[b"1234", b"5678"])
    )
    monkeypatch.setattr(image_module, "AsyncRequestUtils", Mock(return_value=request))

    content = await helper.async_fetch_image_guarded(
        "https://images.example/one.jpg",
        redirect_validator=AsyncMock(return_value=True),
        redirect_policy="public",
        max_bytes=6,
        use_cache=False,
    )

    assert content is None


@pytest.mark.asyncio
async def test_guarded_image_fetch_coalesces_same_cache_key(monkeypatch):
    helper = _guarded_image_helper()
    gate = asyncio.Event()
    request = Mock()
    request.get_stream.return_value = _StreamContext(
        _StreamResponse(200, chunks=[_png_bytes()], gate=gate)
    )
    request_factory = Mock(return_value=request)
    monkeypatch.setattr(image_module, "AsyncRequestUtils", request_factory)

    first = asyncio.create_task(
        helper.async_fetch_image_guarded(
            "https://images.example/one.png",
            redirect_validator=AsyncMock(return_value=True),
            redirect_policy="public",
            max_bytes=1024,
            use_cache=True,
        )
    )
    second = asyncio.create_task(
        helper.async_fetch_image_guarded(
            "https://images.example/one.png",
            redirect_validator=AsyncMock(return_value=True),
            redirect_policy="public",
            max_bytes=1024,
            use_cache=True,
        )
    )
    await asyncio.sleep(0)
    gate.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result == _png_bytes()
    assert request_factory.call_count == 1
    helper.async_file_cache.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_guarded_image_fetch_does_not_share_across_fetch_policies(monkeypatch):
    """字节上限或重定向策略不同的调用必须各自抓取，不能继承他人的限制。"""
    helper = _guarded_image_helper()
    gate = asyncio.Event()
    request = Mock()
    request.get_stream.return_value = _StreamContext(
        _StreamResponse(200, chunks=[_png_bytes()], gate=gate)
    )
    request_factory = Mock(return_value=request)
    monkeypatch.setattr(image_module, "AsyncRequestUtils", request_factory)

    def fetch(*, max_bytes, redirect_policy):
        return asyncio.create_task(
            helper.async_fetch_image_guarded(
                "https://images.example/one.png",
                redirect_validator=AsyncMock(return_value=True),
                redirect_policy=redirect_policy,
                max_bytes=max_bytes,
                use_cache=True,
            )
        )

    larger_limit = fetch(max_bytes=4096, redirect_policy="public")
    smaller_limit = fetch(max_bytes=8, redirect_policy="public")
    other_policy = fetch(max_bytes=4096, redirect_policy="media:http://host:8096")
    await asyncio.sleep(0)
    gate.set()

    results = await asyncio.gather(larger_limit, smaller_limit, other_policy)

    assert request_factory.call_count == 3
    # 8 字节上限的调用不能收到超出自身上限的共享结果
    assert results[1] is None
    assert results[0] == results[2] == _png_bytes()
