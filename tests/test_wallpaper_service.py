import app.application.image as image_service
from app.application.image import WallpaperHelper, configure_wallpaper_providers


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
