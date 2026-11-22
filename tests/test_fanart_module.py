"""
Fanart 模块缓存清理生命周期测试。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.domain.context import MediaInfo
from app.modules.fanart import FanartModule
from app.runtime.config import settings
from app.runtime.tasks import TaskRegistry


def test_fanart_clear_cache_registers_async_cleanup_in_running_loop() -> None:
    """
    同步清缓存入口应复用宿主 owner，并保持立即返回的兼容约定。
    """

    async def scenario() -> None:
        """
        验证异步缓存清理完成前始终由宿主登记器持有。
        """
        registry = TaskRegistry()
        release = asyncio.Event()

        async def clear_async_cache() -> None:
            """
            等待测试释放，以便观察登记中的清理任务。
            """
            await release.wait()

        sync_cache = SimpleNamespace(cache_clear=Mock())
        async_cache = SimpleNamespace(
            cache_clear=Mock(side_effect=lambda: clear_async_cache())
        )
        module = FanartModule()
        with (
            patch.object(
                FanartModule,
                "_FanartModule__request_fanart",
                sync_cache,
            ),
            patch.object(
                FanartModule,
                "_FanartModule__async_request_fanart",
                async_cache,
            ),
            patch("app.modules.fanart.get_task_registry", return_value=registry),
        ):
            assert module.clear_cache() is None
            assert [record.owner for record in registry.records] == [
                "module.fanart.cache_clear"
            ]

            release.set()
            await registry.records[0].task
            await asyncio.sleep(0)

        sync_cache.cache_clear.assert_called_once_with()
        async_cache.cache_clear.assert_called_once_with()
        assert registry.records == ()

    asyncio.run(scenario())


class _FakeFanartResponse:
    """
    模拟成功的 Fanart HTTP 响应。
    """

    def json(self) -> dict:
        """
        返回最小图集载荷。
        """
        return {"name": "fanart"}


def test_fanart_cache_key_follows_api_key(monkeypatch) -> None:
    """
    同一媒体在 API Key 不变时命中缓存，换 Key 后重新请求而不依赖清空缓存。
    """
    from app.schemas.types import MediaType

    requested_urls: list[str] = []

    class _FakeRequestUtils:
        """
        记录请求地址的同步 HTTP 替身。
        """

        def __init__(self, **_kwargs) -> None:
            """
            接受与真实客户端相同的配置关键字，不创建网络资源。
            """

        def get_res(self, url: str, **_kwargs) -> _FakeFanartResponse:
            """
            记录 Fanart 请求地址，并返回固定图集响应，不访问网络。
            """
            requested_urls.append(url)
            return _FakeFanartResponse()

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _FakeRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "key-a")
    module = FanartModule()
    # 使用测试专属 ID，避免与其他用例共享进程内缓存条目。
    mediainfo = MediaInfo(type=MediaType.MOVIE, tmdb_id=987654321, title="Fanart Cache Key")

    module._FanartModule__obtain_fanart_images(mediainfo=mediainfo)
    module._FanartModule__obtain_fanart_images(mediainfo=mediainfo)
    monkeypatch.setattr(settings, "FANART_API_KEY", "key-b")
    module._FanartModule__obtain_fanart_images(mediainfo=mediainfo)

    assert len(requested_urls) == 2
    assert requested_urls[0].endswith("api_key=key-a")
    assert requested_urls[1].endswith("api_key=key-b")


def test_fanart_init_module_keeps_request_cache() -> None:
    """
    启动和重载不再清空请求缓存，Redis 缓存得以跨重启保留。
    """
    module = FanartModule()
    with patch.object(FanartModule, "clear_cache") as clear_cache:
        module.init_module()

    clear_cache.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("name", ["show4kbackground", "movie4kbackground"])
def test_fanart_4k_background_has_standard_name_and_independent_switch(monkeypatch, enabled, name) -> None:
    """
    4K 背景使用可读取的 background 名称，独立开关不影响普通背景及其它艺术图。
    """
    monkeypatch.setattr(settings, "SCRAP_4K_BACKGROUND", enabled)
    module = FanartModule()
    result = {
        "showbackground": [{"url": "https://artwork.invalid/ordinary.jpeg?token=value", "lang": "en"}],
        name: [{"url": "https://artwork.invalid/4k.JPG", "lang": "en"}],
        "clearlogo": [{"url": "https://artwork.invalid/logo.PNG", "lang": "en"}],
    }

    images = module._FanartModule__extract_images(MediaInfo(), result)

    assert images["fanart.jpg"] == "https://artwork.invalid/ordinary.jpeg?token=value"
    assert images["logo.png"] == "https://artwork.invalid/logo.PNG"
    if enabled:
        assert images["background.jpg"] == "https://artwork.invalid/4k.JPG"
    else:
        assert "background.jpg" not in images
    assert not any(key.startswith(("show4k", "movie4k", "clearlogo")) for key in images)


@pytest.mark.parametrize("season,prefix", [(1, "season01"), (0, "season-specials")])
def test_fanart_season_image_suffix_is_normalized(season, prefix) -> None:
    """
    普通季与特别篇继续保留前缀约定，并剥离 URL 查询参数及 JPEG 后缀差异。
    """
    images = FanartModule()._FanartModule__extract_images(
        MediaInfo(),
        {"seasonposter": [{"url": "https://artwork.invalid/season.JPEG?key=value", "season": str(season)}]},
        season=season,
    )

    assert images == {f"{prefix}-poster.jpg": "https://artwork.invalid/season.JPEG?key=value"}


def test_fanart_missing_warning_is_deduplicated_across_repeated_calls(monkeypatch) -> None:
    """
    同一剧集在逐集整理与刮削重复命中缺图缓存时，只通过 warning 输出一次告警。
    """
    from app.schemas.types import MediaType

    requested_urls: list[str] = []

    class _EmptyRequestUtils:
        """
        返回空响应的同步 HTTP 替身，模拟 Fanart 无图集场景。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        def get_res(self, url: str, **_kwargs) -> None:
            """
            记录请求地址并模拟无有效响应返回。
            """
            requested_urls.append(url)
            return None

    class _EmptyAsyncRequestUtils:
        """
        返回空响应的异步 HTTP 替身，验证同步与异步共享缺图去重状态。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        async def get_json(self, url: str) -> None:
            """
            记录请求地址并模拟无有效 JSON 载荷。
            """
            requested_urls.append(url)
            return None

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _EmptyRequestUtils)
    monkeypatch.setattr("app.modules.fanart.AsyncRequestUtils", _EmptyAsyncRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "dedup-key-a")

    module = FanartModule()
    module.clear_cache()
    mediainfo = MediaInfo(type=MediaType.TV, tvdb_id=7531001, title="走马上任", year="2026")
    other_media = MediaInfo(type=MediaType.TV, tvdb_id=7531002, title="另一部剧", year="2026")

    try:
        with (
            patch("app.modules.fanart.logger.warning") as warning_log,
            patch("app.modules.fanart.logger.warn") as warn_log,
        ):
            assert module.obtain_images(mediainfo) is None
            assert module.obtain_images(mediainfo) is None
            assert module.metadata_img(mediainfo) is None
            assert module.metadata_img(mediainfo, season=1) is None
            assert asyncio.run(module.async_obtain_images(mediainfo)) is None
            assert module.obtain_images(other_media) is None

        warn_log.assert_not_called()
        assert [call.args[0] for call in warning_log.call_args_list] == [
            "没有获取到 走马上任 (2026) 的fanart图片数据",
            "没有获取到 另一部剧 (2026) 的fanart图片数据",
        ]
        assert len(requested_urls) == 2
    finally:
        module.clear_cache()


def test_fanart_missing_warning_resets_on_key_change_cache_clear_and_recovery(
    monkeypatch,
) -> None:
    """
    更换 API Key、清理缓存或后续恢复出图后，再次缺图应重新允许告警一次。
    """
    from app.schemas.types import MediaType

    responses = iter([
        {"status": "error", "error message": "Not found"},
        {"status": "error", "error message": "Not found"},
        {"status": "error", "error message": "Not found"},
    ])

    class _ErrorFanartResponse:
        """
        返回 Fanart 错误 JSON 载荷的 HTTP 响应替身。
        """

        def __init__(self, payload: dict) -> None:
            """
            保存本轮响应载荷。

            :param payload: 模拟 Fanart 返回的 JSON 字典
            """
            self._payload = payload

        def json(self) -> dict:
            """
            返回预置的错误载荷。
            """
            return self._payload

    class _SequencedRequestUtils:
        """
        按顺序消费错误响应的同步 HTTP 替身。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        def get_res(self, _url: str, **_kwargs) -> _ErrorFanartResponse:
            """
            返回队列中的下一条错误响应。
            """
            return _ErrorFanartResponse(next(responses))

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _SequencedRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "reset-key-a")

    module = FanartModule()
    module.clear_cache()
    mediainfo = MediaInfo(type=MediaType.MOVIE, tmdb_id=7531003, title="测试电影", year="2026")

    try:
        with patch("app.modules.fanart.logger.warning") as warning_log:
            # 首次缺图告警，重复调用去重。
            assert module.obtain_images(mediainfo) is None
            assert module.obtain_images(mediainfo) is None
            assert warning_log.call_count == 1

            # 更换 API Key 后指纹变化，新 Key 下再次缺图告警一次。
            monkeypatch.setattr(settings, "FANART_API_KEY", "reset-key-b")
            assert module.obtain_images(mediainfo) is None
            assert module.obtain_images(mediainfo) is None
            assert warning_log.call_count == 2

            # 清理缓存后清空去重记录，再次缺图重新告警一次。
            module.clear_cache()
            assert module.obtain_images(mediainfo) is None
            assert warning_log.call_count == 3

            # 获取到有效图集后清除该媒体缺图状态，后续再缺图时允许重新告警。
            valid = module._FanartModule__extract_images(
                mediainfo,
                {"movieposter": [{"url": "https://artwork.invalid/poster.jpg", "lang": "zh"}]},
            )
            assert valid == {"poster.jpg": "https://artwork.invalid/poster.jpg"}
            assert module._FanartModule__extract_images(mediainfo, {}) is None
            assert warning_log.call_count == 4
    finally:
        module.clear_cache()
