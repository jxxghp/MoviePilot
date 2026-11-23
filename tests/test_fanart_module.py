"""
Fanart 模块查询、缓存与缺图告警行为测试。
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


def test_fanart_tv_falls_back_to_tmdb_id_with_explicit_source(monkeypatch) -> None:
    """
    电视剧缺失 TVDB 标识时应回退使用 TMDB 标识并携带 ``source=tmdb``，同时保留 TVDB 优先顺序。
    """
    from app.schemas.types import MediaType

    requested_urls: list[str] = []
    async_requested_urls: list[str] = []

    class _PayloadResponse:
        """
        返回固定图集字典的同步 HTTP 响应替身。
        """

        def __init__(self, payload: dict) -> None:
            """
            保存模拟响应载荷。

            :param payload: 模拟 Fanart 返回的 JSON 图集字典
            """
            self._payload = payload

        def json(self) -> dict:
            """
            返回预置图集字典。
            """
            return self._payload

    class _RoutingRequestUtils:
        """
        按请求 URL 返回不同图集的同步 HTTP 替身。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        def get_res(self, url: str, **_kwargs) -> _PayloadResponse:
            """
            记录请求地址并返回对应剧集的模拟图集。

            :param url: Fanart 请求地址
            :return: 模拟的 HTTP 响应对象
            """
            requested_urls.append(url)
            if "/v3/tv/95842?" in url and "source=tmdb" in url:
                return _PayloadResponse(
                    {
                        "hdtvlogo": [
                            {"url": "https://artwork.invalid/joy-logo.png", "lang": "zh", "likes": "5"}
                        ],
                        "seasonposter": [
                            {"url": "https://artwork.invalid/joy-s1.jpg", "season": "1"}
                        ],
                    }
                )
            if "/v3/tv/81189?" in url and "source=tmdb" not in url:
                return _PayloadResponse(
                    {
                        "tvposter": [
                            {"url": "https://artwork.invalid/bb-tvdb-poster.jpg", "lang": "en", "likes": "3"}
                        ]
                    }
                )
            return _PayloadResponse({})

    class _RoutingAsyncRequestUtils:
        """
        记录异步请求并返回模拟图集的异步 HTTP 替身。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        async def get_json(self, url: str) -> dict:
            """
            记录异步请求地址并返回模拟图集。

            :param url: Fanart 请求地址
            :return: 模拟的 JSON 图集字典
            """
            async_requested_urls.append(url)
            if "/v3/tv/1396?" in url and "source=tmdb" in url:
                return {
                    "hdtvlogo": [
                        {"url": "https://artwork.invalid/bb-tmdb-logo.png", "lang": "en", "likes": "9"}
                    ]
                }
            return {}

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _RoutingRequestUtils)
    monkeypatch.setattr("app.modules.fanart.AsyncRequestUtils", _RoutingAsyncRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "tv-fallback-key")

    module = FanartModule()
    module.clear_cache()

    tmdb_only_tv = MediaInfo(type=MediaType.TV, tmdb_id=95842, title="庆余年", year="2019")
    async_tmdb_only_tv = MediaInfo(type=MediaType.TV, tmdb_id=1396, title="绝命毒师", year="2008")
    both_ids_tv = MediaInfo(
        type=MediaType.TV, tvdb_id=81189, tmdb_id=1396, title="绝命毒师", year="2008"
    )

    try:
        # 仅含 TMDB 标识的剧集同步查询应附带 source=tmdb 并共享缓存到季图查询。
        assert module.obtain_images(tmdb_only_tv) is tmdb_only_tv
        assert tmdb_only_tv.logo_path == "https://artwork.invalid/joy-logo.png"
        assert module.metadata_img(tmdb_only_tv, season=1) == {
            "season01-poster.jpg": "https://artwork.invalid/joy-s1.jpg"
        }

        # 同时具备 TVDB 与 TMDB 标识时优先使用 TVDB 标识且不附带 source=tmdb。
        assert module.obtain_images(both_ids_tv) is both_ids_tv
        assert both_ids_tv.poster_path == "https://artwork.invalid/bb-tvdb-poster.jpg"

        # 异步入口同样在缺失 TVDB 标识时回退使用 TMDB 标识。
        assert asyncio.run(module.async_obtain_images(async_tmdb_only_tv)) is async_tmdb_only_tv
        assert async_tmdb_only_tv.logo_path == "https://artwork.invalid/bb-tmdb-logo.png"

        assert requested_urls == [
            "https://webservice.fanart.tv/v3/tv/95842?api_key=tv-fallback-key&source=tmdb",
            "https://webservice.fanart.tv/v3/tv/81189?api_key=tv-fallback-key",
        ]
        assert async_requested_urls == [
            "https://webservice.fanart.tv/v3/tv/1396?api_key=tv-fallback-key&source=tmdb",
        ]
    finally:
        module.clear_cache()


def test_fanart_tv_same_numeric_id_isolates_tvdb_and_tmdb_cache_and_warnings(
    monkeypatch,
) -> None:
    """
    同号 TVDB 与 TMDB 剧集必须隔离请求缓存和缺图告警，避免跨命名空间串图或吞告警。
    """
    from app.schemas.types import MediaType

    requested_urls: list[str] = []

    class _PayloadResponse:
        """
        返回模拟图集的同步 HTTP 响应替身。
        """

        def __init__(self, payload: dict) -> None:
            """
            保存模拟图集载荷。

            :param payload: 模拟 Fanart 返回的 JSON 图集字典
            """
            self._payload = payload

        def json(self) -> dict:
            """
            返回预置图集字典。
            """
            return self._payload

    class _CollisionRequestUtils:
        """
        模拟 TVDB 与 TMDB 同号冲突剧集响应的同步 HTTP 替身。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        def get_res(self, url: str, **_kwargs) -> _PayloadResponse:
            """
            根据是否携带 ``source=tmdb`` 返回不同剧集的图集或空响应。

            :param url: Fanart 请求地址
            :return: 模拟的 HTTP 响应对象
            """
            requested_urls.append(url)
            if "/v3/tv/82856?" in url and "source=tmdb" in url:
                return _PayloadResponse(
                    {
                        "tvposter": [
                            {"url": "https://artwork.invalid/mandalorian.jpg", "lang": "en", "likes": "8"}
                        ]
                    }
                )
            if "/v3/tv/82856?" in url:
                return _PayloadResponse(
                    {
                        "tvposter": [
                            {"url": "https://artwork.invalid/ninja-nonsense.jpg", "lang": "en", "likes": "2"}
                        ]
                    }
                )
            return _PayloadResponse({})

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _CollisionRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "collision-key")

    module = FanartModule()
    module.clear_cache()

    tvdb_show = MediaInfo(type=MediaType.TV, tvdb_id=82856, title="Ninja Nonsense", year="2004")
    tmdb_show = MediaInfo(type=MediaType.TV, tmdb_id=82856, title="The Mandalorian", year="2019")
    empty_tvdb_show = MediaInfo(type=MediaType.TV, tvdb_id=70327, title="同号缺图剧", year="2026")
    empty_tmdb_show = MediaInfo(type=MediaType.TV, tmdb_id=70327, title="同号缺图剧", year="2026")

    try:
        assert module.obtain_images(tvdb_show) is tvdb_show
        assert module.obtain_images(tmdb_show) is tmdb_show
        assert tvdb_show.poster_path == "https://artwork.invalid/ninja-nonsense.jpg"
        assert tmdb_show.poster_path == "https://artwork.invalid/mandalorian.jpg"

        with patch("app.modules.fanart.logger.warning") as warning_log:
            # 同号同名的 TVDB 与 TMDB 缺图应分别告警一次，且重复调用各自去重。
            assert module.obtain_images(empty_tvdb_show) is None
            assert module.obtain_images(empty_tvdb_show) is None
            assert module.obtain_images(empty_tmdb_show) is None
            assert module.obtain_images(empty_tmdb_show) is None
            # 直接调用内部提取方法时，推导出的去重键应与正常请求一致。
            assert module._FanartModule__extract_images(empty_tvdb_show, {}) is None
            assert module._FanartModule__extract_images(empty_tmdb_show, {}) is None
            assert warning_log.call_count == 2

        assert requested_urls == [
            "https://webservice.fanart.tv/v3/tv/82856?api_key=collision-key",
            "https://webservice.fanart.tv/v3/tv/82856?api_key=collision-key&source=tmdb",
            "https://webservice.fanart.tv/v3/tv/70327?api_key=collision-key",
            "https://webservice.fanart.tv/v3/tv/70327?api_key=collision-key&source=tmdb",
        ]
    finally:
        module.clear_cache()


def test_fanart_non_tv_with_only_tmdb_id_does_not_fallback_to_tv_endpoint(
    monkeypatch,
) -> None:
    """
    非电影且非电视剧类型（如合集）仅有 TMDB 标识时，不得误查 TV 端点。
    """
    from app.schemas.types import MediaType

    requested_urls: list[str] = []

    class _UnexpectedRequestUtils:
        """
        记录意外 HTTP 请求的同步替身。
        """

        def __init__(self, **_kwargs) -> None:
            """
            忽略网络配置参数，不创建真实连接。
            """

        def get_res(self, url: str, **_kwargs) -> None:
            """
            记录请求地址并返回空响应。

            :param url: Fanart 请求地址
            """
            requested_urls.append(url)
            return None

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _UnexpectedRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "collection-key")

    module = FanartModule()
    module.clear_cache()
    collection = MediaInfo(type=MediaType.COLLECTION, tmdb_id=10, title="测试系列")

    try:
        with patch("app.modules.fanart.logger.info") as info_log:
            assert module.obtain_images(collection) is None

        assert requested_urls == []
        info_log.assert_called_once()
    finally:
        module.clear_cache()
