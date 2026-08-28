from collections.abc import Mapping
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Optional

import app.application.image as image_service
from app.application.image import (
    ImageHelper,
    ImageResponsePort,
    WallpaperHelper,
    configure_image_ports,
    configure_wallpaper_providers,
    reset_image_ports,
)


class _WallpaperTransport:
    """返回固定 Bing JSON 的测试图片传输。"""

    def __init__(self, path: str) -> None:
        """保存壁纸路径。"""
        self.path = path

    def get(
        self,
        _url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """返回带固定 JSON 的成功响应。"""
        assert options == {"timeout": 5}
        return SimpleNamespace(
            status_code=200,
            content=b"",
            headers={},
            json=lambda: {"images": [{"url": self.path}]},
        )

    async def async_get(
        self,
        _url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """本用例不使用异步壁纸读取。"""
        assert options
        return None


class _ExternalAddress:
    """测试壁纸 URL 始终按外部地址处理。"""

    @staticmethod
    def is_internal(_url: str) -> bool:
        """返回 False。"""
        return False


def test_wallpaper_service_uses_startup_providers(monkeypatch):
    """壁纸服务通过组合根注入的 provider 访问业务 Chain。"""
    provider_names = (
        "_tmdb_wallpaper_provider",
        "_tmdb_wallpaper_list_provider",
        "_mediaserver_wallpaper_provider",
        "_mediaserver_wallpaper_list_provider",
    )
    for provider_name in provider_names:
        monkeypatch.setattr(
            image_service,
            provider_name,
            getattr(image_service, provider_name),
        )
    cached_methods = (
        WallpaperHelper.get_tmdb_wallpaper,
        WallpaperHelper.get_tmdb_wallpapers,
        WallpaperHelper.get_mediaserver_wallpaper,
        WallpaperHelper.get_mediaserver_wallpapers,
    )
    for method in cached_methods:
        method.cache_clear()
    try:
        configure_wallpaper_providers(
            tmdb_wallpaper=lambda: "tmdb-one",
            tmdb_wallpapers=lambda count: [f"tmdb-{count}"],
            mediaserver_wallpaper=lambda: "server-one",
            mediaserver_wallpapers=lambda count: [f"server-{count}"],
        )
        helper = WallpaperHelper()

        assert helper.get_tmdb_wallpaper() == "tmdb-one"
        assert helper.get_tmdb_wallpapers(3) == ["tmdb-3"]
        assert helper.get_mediaserver_wallpaper() == "server-one"
        assert helper.get_mediaserver_wallpapers(4) == ["server-4"]
    finally:
        for method in cached_methods:
            method.cache_clear()


def test_reconfigure_image_ports_clears_wallpaper_cache():
    """切换 transport 后不得复用旧实现写入的壁纸缓存。"""
    previous = configure_image_ports(
        transport=_WallpaperTransport("/old.jpg"),
        internal_address=_ExternalAddress(),
    )
    try:
        helper = WallpaperHelper()
        assert helper.get_bing_wallpaper() == "https://cn.bing.com/old.jpg"

        configure_image_ports(
            transport=_WallpaperTransport("/new.jpg"),
            internal_address=_ExternalAddress(),
        )

        assert helper.get_bing_wallpaper() == "https://cn.bing.com/new.jpg"
    finally:
        reset_image_ports(*previous)


def test_image_legacy_helpers_keep_canonical_class_identity():
    """旧 helper 路径必须继续导出 canonical 图片与壁纸类。"""
    legacy_image = import_module("app.helper.image")
    legacy_wallpaper = import_module("app.helper.wallpaper")

    assert legacy_image.ImageHelper is ImageHelper
    assert legacy_wallpaper.WallpaperHelper is WallpaperHelper
