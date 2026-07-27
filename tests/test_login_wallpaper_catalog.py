import io
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, HTTPException
from PIL import Image

from app.api.endpoints import login as login_endpoint
from app.helper import image as image_module
from app.helper.image import ImageHelper, WallpaperHelper
from app.utils.singleton import Singleton


@pytest.fixture
def wallpaper_helper(tmp_path, monkeypatch):
    """为每个用例提供独立的持久化目录和单例实例。"""
    monkeypatch.setattr(type(image_module.settings), "CACHE_PATH", property(lambda _: tmp_path))
    singleton_key = (WallpaperHelper, (), frozenset())
    Singleton._instances.pop(singleton_key, None)
    helper = WallpaperHelper()
    yield helper
    Singleton._instances.pop(singleton_key, None)


def test_wallpapers_preserves_default_external_contract(monkeypatch):
    helper = Mock()
    helper.get_wallpapers.return_value = ["https://images.example/one.jpg"]
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))

    result = login_endpoint.wallpapers(BackgroundTasks(), same_origin=False)

    assert result == ["https://images.example/one.jpg"]
    helper.get_wallpapers.assert_called_once_with()
    helper.get_wallpaper_catalog_ids.assert_not_called()


def test_same_origin_wallpapers_return_cached_catalog_and_refresh_in_background(monkeypatch):
    helper = Mock()
    helper.get_wallpaper_catalog_ids.return_value = ["opaque-id"]
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))
    background_tasks = BackgroundTasks()

    result = login_endpoint.wallpapers(background_tasks, same_origin=True)

    assert result == ["/api/v1/login/wallpapers/opaque-id"]
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].func == helper.refresh_wallpaper_catalog


def test_catalog_persists_last_success_and_rejects_unknown_ids(wallpaper_helper, monkeypatch):
    ids = wallpaper_helper.register_wallpaper_catalog(
        [
            "https://images.example/one.jpg",
            "https://images.example/two.jpg",
        ]
    )
    monkeypatch.setattr(wallpaper_helper, "get_wallpapers", Mock(return_value=[]))

    assert wallpaper_helper.refresh_wallpaper_catalog() == ids
    assert wallpaper_helper.get_wallpaper_catalog_source(ids[0]) == "https://images.example/one.jpg"
    assert wallpaper_helper.get_wallpaper_catalog_source("unknown") is None

    singleton_key = (WallpaperHelper, (), frozenset())
    Singleton._instances.pop(singleton_key, None)
    restored = WallpaperHelper()
    assert restored.get_wallpaper_catalog_ids() == ids


def test_catalog_keeps_retired_sources_without_returning_them_as_active(wallpaper_helper):
    retired_ids = wallpaper_helper.register_wallpaper_catalog(
        [
            "https://images.example/one.jpg",
            "https://images.example/two.jpg",
        ]
    )
    active_ids = wallpaper_helper.register_wallpaper_catalog(
        ["https://images.example/three.jpg"]
    )

    assert wallpaper_helper.get_wallpaper_catalog_ids() == active_ids
    assert wallpaper_helper.get_wallpaper_catalog_source(retired_ids[0]) == (
        "https://images.example/one.jpg"
    )


def test_catalog_restores_active_list_separately_from_retained_sources(
    wallpaper_helper, tmp_path
):
    wallpaper_helper.register_wallpaper_catalog(
        [
            "https://images.example/one.jpg",
            "https://images.example/two.jpg",
        ]
    )
    active_ids = wallpaper_helper.register_wallpaper_catalog(
        ["https://images.example/three.jpg"]
    )

    singleton_key = (WallpaperHelper, (), frozenset())
    Singleton._instances.pop(singleton_key, None)
    restored = WallpaperHelper()

    assert restored.get_wallpaper_catalog_ids() == active_ids
    assert (tmp_path / "login_wallpapers" / "catalog.json").is_file()


@pytest.mark.asyncio
async def test_wallpaper_image_uses_catalog_security_and_cache(monkeypatch):
    helper = Mock()
    helper.get_wallpaper_catalog_source.return_value = "https://images.example/one.jpg"
    image_helper = Mock()
    image_helper.async_fetch_image_guarded = AsyncMock(return_value=b"image-bytes")
    mediaserver = Mock()
    mediaserver.get_image_cookies.return_value = {"session": "cookie"}
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))
    monkeypatch.setattr(login_endpoint, "ImageHelper", Mock(return_value=image_helper))
    monkeypatch.setattr(login_endpoint, "MediaServerChain", Mock(return_value=mediaserver))
    monkeypatch.setattr(
        login_endpoint.SecurityUtils,
        "is_safe_image_url_async",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(login_endpoint.HashUtils, "md5", Mock(return_value="etag"))
    monkeypatch.setattr(
        login_endpoint.RequestUtils,
        "generate_cache_headers",
        Mock(return_value={"ETag": "etag"}),
    )

    response = await login_endpoint.wallpaper_image("opaque-id")

    assert response.status_code == 200
    assert response.body == b"image-bytes"
    image_helper.async_fetch_image_guarded.assert_awaited_once()
    fetch_options = image_helper.async_fetch_image_guarded.await_args.kwargs
    assert fetch_options["url"] == "https://images.example/one.jpg"
    assert fetch_options["use_cache"] is True
    assert fetch_options["cookies"] == {"session": "cookie"}
    assert fetch_options["max_bytes"] == 32 * 1024 * 1024
    assert fetch_options["max_pixels"] == 50_000_000

    assert await fetch_options["redirect_validator"](
        "https://images.example/redirected.jpg"
    )
    assert login_endpoint.SecurityUtils.is_safe_image_url_async.await_count == 2


@pytest.mark.asyncio
async def test_wallpaper_image_hides_unknown_catalog_entries(monkeypatch):
    helper = Mock()
    helper.get_wallpaper_catalog_source.return_value = None
    monkeypatch.setattr(login_endpoint, "WallpaperHelper", Mock(return_value=helper))

    with pytest.raises(HTTPException) as error:
        await login_endpoint.wallpaper_image("unknown")

    assert error.value.status_code == 404


class _StreamResponse:
    """提供受限图片抓取测试所需的最小流式响应合同。"""

    def __init__(self, status_code, *, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def aiter_bytes(self):
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
    return helper


def _png_bytes(width=1, height=1):
    content = io.BytesIO()
    Image.new("RGB", (width, height), color="black").save(content, format="PNG")
    return content.getvalue()


@pytest.mark.asyncio
async def test_guarded_image_fetch_rejects_redirect_before_second_request(monkeypatch):
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
        max_bytes=1024,
        max_pixels=100,
        use_cache=False,
    )

    assert content is None
    redirect_validator.assert_awaited_once_with("http://127.0.0.1/private.jpg")
    request_factory.assert_called_once()
    request.get_stream.assert_called_once_with("https://images.example/one.jpg")


@pytest.mark.asyncio
async def test_guarded_image_fetch_follows_safe_redirect_without_forwarding_cookies(
    monkeypatch,
):
    helper = _guarded_image_helper()
    payload = _png_bytes()
    first_request = Mock()
    first_request.get_stream.return_value = _StreamContext(
        _StreamResponse(302, headers={"location": "https://cdn.example/one.png"})
    )
    second_request = Mock()
    second_request.get_stream.return_value = _StreamContext(
        _StreamResponse(200, chunks=[payload])
    )
    request_factory = Mock(side_effect=[first_request, second_request])
    redirect_validator = AsyncMock(return_value=True)
    monkeypatch.setattr(image_module, "AsyncRequestUtils", request_factory)

    content = await helper.async_fetch_image_guarded(
        "https://images.example/one.jpg",
        redirect_validator=redirect_validator,
        max_bytes=1024,
        max_pixels=100,
        use_cache=True,
        cookies={"session": "cookie"},
    )

    assert content == payload
    redirect_validator.assert_awaited_once_with("https://cdn.example/one.png")
    assert request_factory.call_args_list[0].kwargs["cookies"] == {
        "session": "cookie"
    }
    assert request_factory.call_args_list[1].kwargs["cookies"] is None
    helper.async_file_cache.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_guarded_image_fetch_rejects_decoded_image_over_pixel_limit(monkeypatch):
    helper = _guarded_image_helper()
    request = Mock()
    request.get_stream.return_value = _StreamContext(
        _StreamResponse(200, chunks=[_png_bytes(width=2, height=2)])
    )
    monkeypatch.setattr(image_module, "AsyncRequestUtils", Mock(return_value=request))

    content = await helper.async_fetch_image_guarded(
        "https://images.example/one.png",
        redirect_validator=AsyncMock(return_value=True),
        max_bytes=1024,
        max_pixels=3,
        use_cache=False,
    )

    assert content is None
    helper.async_file_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_guarded_image_fetch_stops_chunked_response_over_byte_limit(monkeypatch):
    helper = _guarded_image_helper()
    request = Mock()
    request.get_stream.return_value = _StreamContext(
        _StreamResponse(200, chunks=[b"1234", b"5678"])
    )
    monkeypatch.setattr(image_module, "AsyncRequestUtils", Mock(return_value=request))

    content = await helper.async_fetch_image_guarded(
        "https://images.example/one.jpg",
        redirect_validator=AsyncMock(return_value=True),
        max_bytes=6,
        max_pixels=100,
        use_cache=False,
    )

    assert content is None
