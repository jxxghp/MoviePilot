from collections.abc import Mapping
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Optional

import app.application.image as image_service
from app.api.endpoints import system as system_endpoint
from app.application.image import (
    ImageHelper,
    ImageResponsePort,
    WallpaperHelper,
    configure_image_ports,
    configure_wallpaper_providers,
    reset_image_ports,
)
from app.runtime.config import ConfigModel


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


def test_static_wallpaper_returns_local_or_remote_address_directly(monkeypatch):
    """静态模式不发起 API 请求，直接沿用用户填写的本地路径或 URL。"""
    config = SimpleNamespace(wallpaper="static", wallpaper_image_url="/local/wallpaper.jpg")
    monkeypatch.setattr(image_service, "get_chain_runtime_config_snapshot", lambda: config)
    helper = WallpaperHelper()

    assert helper.get_wallpaper() == "/local/wallpaper.jpg"
    assert helper.get_wallpapers() == ["/local/wallpaper.jpg"]

    config.wallpaper_image_url = "https://images.example/wallpaper.webp"
    assert helper.get_wallpaper() == "https://images.example/wallpaper.webp"
    assert helper.get_wallpapers() == ["https://images.example/wallpaper.webp"]


def test_wallpaper_settings_define_backward_compatible_defaults():
    """未配置新字段时维持 15 秒轮换，静态地址保持为空。"""
    assert ConfigModel.model_fields["WALLPAPER_ROTATION_INTERVAL"].default == 15
    assert ConfigModel.model_fields["WALLPAPER_IMAGE_URL"].default is None


def test_login_global_settings_expose_wallpaper_rotation_interval(monkeypatch):
    """登录前全局设置必须下发轮换间隔，避免前端退回硬编码时钟。"""
    values = {
        "GLOBAL_IMAGE_CACHE": False,
        "TMDB_IMAGE_DOMAIN": "image.tmdb.org",
        "WALLPAPER_ROTATION_INTERVAL": 300,
    }
    runtime_settings = SimpleNamespace(
        snapshot=lambda *, include: {
            key: value for key, value in values.items() if key in include
        },
        get=lambda key: False if key == "DEV" else values.get(key),
    )
    monkeypatch.setattr(system_endpoint, "get_runtime_settings", lambda: runtime_settings)
    monkeypatch.setattr(system_endpoint, "get_frontend_version", lambda: "v3-test")
    monkeypatch.setattr(system_endpoint, "get_app_version", lambda: "v3-test")

    response = system_endpoint.get_global_setting("moviepilot")

    assert response.data["WALLPAPER_ROTATION_INTERVAL"] == 300


def test_image_legacy_helpers_keep_canonical_class_identity():
    """旧 helper 路径必须继续导出 canonical 图片与壁纸类。"""
    legacy_image = import_module("app.helper.image")
    legacy_wallpaper = import_module("app.helper.wallpaper")

    assert legacy_image.ImageHelper is ImageHelper
    assert legacy_wallpaper.WallpaperHelper is WallpaperHelper
