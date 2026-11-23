"""
MediaChain 跨来源身份投影的同步、异步对称性测试。
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

from app.application.server.mapping import (
    configure_media_mapping_port,
    get_media_mapping_port,
)
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.media import MediaChain
from app.chain.subscribe.metadata import SubscribeMetadataOwner
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.schemas.mapping import MediaMapping, MediaMappingQuery
from app.schemas.types import MediaSource, MediaType


class _SyncProjectionChain:
    """
    记录同步来源读取和目标匹配参数的投影测试桩。
    """

    def __init__(self) -> None:
        """
        初始化固定来源详情和调用记录。
        """
        self.tmdb_match: dict[str, Any] = {}
        self.imdb_tmdb_match: dict[str, Any] = {}
        self.douban_match: dict[str, Any] = {}
        self.tmdb_result = {"id": 550, "media_type": "tv"}
        self.imdb_tmdb_result = {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}

    def match_tmdbinfo(self, **kwargs: Any) -> dict[str, Any]:
        """
        记录直接 TMDB 匹配参数并返回模拟电影详情。
        """
        self.imdb_tmdb_match = kwargs
        return self.imdb_tmdb_result

    @staticmethod
    def douban_info(
        doubanid: str,
        mtype: Optional[MediaType] = None,
    ) -> dict[str, Any]:
        """
        返回包含中英文标题和显式类型的豆瓣详情。
        """
        assert doubanid == "1295644"
        assert mtype is None
        return {
            "id": doubanid,
            "title": "电视剧测试",
            "original_title": "Series Test",
            "year": "2026",
            "media_type": MediaType.TV,
        }

    def _match_tmdb_with_names(self, **kwargs: Any) -> dict[str, Any]:
        """
        记录 TMDB 匹配参数并返回可观察的原始详情。
        """
        self.tmdb_match = kwargs
        return self.tmdb_result

    @staticmethod
    def tmdb_info(
        tmdbid: int,
        mtype: Optional[MediaType],
    ) -> dict[str, Any]:
        """
        返回含第零季年份和 IMDb 身份的 TMDB 详情。
        """
        assert tmdbid == 550
        assert mtype == MediaType.TV
        return {
            "id": tmdbid,
            "name": "Series Test",
            "external_ids": {"imdb_id": "tt0000550"},
            "seasons": [
                {"season_number": 0, "air_date": "2025-12-01"},
                {"season_number": 1, "air_date": "2026-01-01"},
            ],
        }

    def match_doubaninfo(self, **kwargs: Any) -> dict[str, Any]:
        """
        记录豆瓣匹配参数并返回目标身份。
        """
        self.douban_match = kwargs
        return {"id": "1295644", "media_type": "tv"}


class _AsyncProjectionChain:
    """
    记录异步来源读取和目标匹配参数的投影测试桩。
    """

    def __init__(self) -> None:
        """
        初始化固定来源详情和调用记录。
        """
        self.tmdb_match: dict[str, Any] = {}
        self.imdb_tmdb_match: dict[str, Any] = {}
        self.douban_match: dict[str, Any] = {}
        self.tmdb_result = {"id": 550, "media_type": "tv"}
        self.imdb_tmdb_result = {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}

    async def async_match_tmdbinfo(self, **kwargs: Any) -> dict[str, Any]:
        """
        异步记录直接 TMDB 匹配参数并返回模拟电影详情。
        """
        self.imdb_tmdb_match = kwargs
        return self.imdb_tmdb_result

    async def async_douban_info(
        self,
        doubanid: str,
        mtype: Optional[MediaType] = None,
    ) -> dict[str, Any]:
        """
        异步返回包含中英文标题和显式类型的豆瓣详情。
        """
        return _SyncProjectionChain.douban_info(doubanid, mtype)

    async def _async_match_tmdb_with_names(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        记录异步 TMDB 匹配参数并返回原始详情。
        """
        self.tmdb_match = kwargs
        return self.tmdb_result

    async def async_tmdb_info(
        self,
        tmdbid: int,
        mtype: Optional[MediaType],
    ) -> dict[str, Any]:
        """
        异步返回含第零季年份和 IMDb 身份的 TMDB 详情。
        """
        return _SyncProjectionChain.tmdb_info(tmdbid, mtype)

    async def async_match_doubaninfo(self, **kwargs: Any) -> dict[str, Any]:
        """
        记录异步豆瓣匹配参数并返回目标身份。
        """
        self.douban_match = kwargs
        return {"id": "1295644", "media_type": "tv"}


def test_douban_to_tmdb_keeps_result_fields_and_season_zero() -> None:
    """
    豆瓣转 TMDB 应保留目标详情字段并显式返回第零季。
    """
    chain = _SyncProjectionChain()

    result = MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Douban,
        media_id="1295644",
        season=0,
    )

    assert result == {"id": 550, "media_type": "tv", "season": 0}
    assert chain.tmdb_result == {"id": 550, "media_type": "tv"}
    assert chain.tmdb_match == {
        "meta_names": ("Series Test", "测试"),
        "year": "2026",
        "mtype": MediaType.TV,
        "season": 0,
    }


def test_tmdb_to_douban_uses_season_zero_year_and_identity() -> None:
    """
    TMDB 转豆瓣应使用第零季年份并保留目标 id 和媒体类型。
    """
    chain = _SyncProjectionChain()

    result = MediaChain.convert_media_identity(
        chain,
        target_source=MediaSource.Douban,
        media_source=MediaSource.TMDB,
        media_id="550",
        mtype=MediaType.TV,
        season=0,
    )

    assert result == {"id": "1295644", "media_type": "tv"}
    assert chain.douban_match == {
        "name": "Series Test",
        "year": "2025",
        "mtype": MediaType.TV,
        "imdbid": "tt0000550",
        "season": None,
    }


def test_async_douban_to_tmdb_matches_sync_projection() -> None:
    """
    异步豆瓣转 TMDB 应与同步规则保持字段和第零季语义一致。
    """
    chain = _AsyncProjectionChain()

    result = asyncio.run(
        MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.TMDB,
            media_source=MediaSource.Douban,
            media_id="1295644",
            season=0,
        )
    )

    assert result == {"id": 550, "media_type": "tv", "season": 0}
    assert chain.tmdb_result == {"id": 550, "media_type": "tv"}
    assert chain.tmdb_match["season"] == 0
    assert chain.tmdb_match["mtype"] == MediaType.TV


def test_async_tmdb_to_douban_matches_sync_projection() -> None:
    """
    异步 TMDB 转豆瓣应复用同步纯规则生成第零季年份。
    """
    chain = _AsyncProjectionChain()

    result = asyncio.run(
        MediaChain.async_convert_media_identity(
            chain,
            target_source=MediaSource.Douban,
            media_source=MediaSource.TMDB,
            media_id="550",
            mtype=MediaType.TV,
            season=0,
        )
    )

    assert result == {"id": "1295644", "media_type": "tv"}
    assert chain.douban_match["year"] == "2025"
    assert chain.douban_match["imdbid"] == "tt0000550"


def test_imdb_to_tmdb_sync_and_async_normalize_id_and_share_mapping() -> None:
    """
    IMDb 转 TMDB 应规范化标识、直连 match_tmdbinfo 并在同步与异步入口共享映射上报。
    """
    previous_port = get_media_mapping_port()
    port = SimpleNamespace(
        query=Mock(return_value=None),
        async_query=AsyncMock(return_value=None),
        share=Mock(),
    )
    configure_media_mapping_port(port)
    try:
        sync_chain = _SyncProjectionChain()
        sync_result = MediaChain.convert_media_identity(
            sync_chain,
            target_source=MediaSource.TMDB,
            media_source=MediaSource.IMDb,
            media_id=" TT27165187 ",
            mtype=MediaType.MOVIE,
        )
        assert sync_result == {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}
        assert sync_chain.imdb_tmdb_match == {
            "name": "",
            "mtype": MediaType.MOVIE,
            "season": None,
            "imdbid": "tt27165187",
        }

        async_chain = _AsyncProjectionChain()
        async_result = asyncio.run(
            MediaChain.async_convert_media_identity(
                async_chain,
                target_source=MediaSource.TMDB,
                media_source=MediaSource.IMDb,
                media_id="TT27165187",
                mtype=MediaType.MOVIE,
            )
        )
        assert async_result == sync_result
        assert async_chain.imdb_tmdb_match == sync_chain.imdb_tmdb_match
        assert port.query.call_args.args[0] == MediaMappingQuery(
            media_source=MediaSource.IMDb,
            media_id="tt27165187",
            type="movie",
        )
        assert port.share.call_count == 2
        assert port.share.call_args.args[0] == MediaMapping(
            media_source=MediaSource.IMDb,
            media_id="tt27165187",
            type="movie",
            tmdb_id=1101383,
            tmdb_type="movie",
            tmdb_season=None,
        )
    finally:
        configure_media_mapping_port(previous_port)


def test_imdb_to_tmdb_rejects_invalid_id_without_calling_tmdb() -> None:
    """
    非 tt 数字格式的 IMDb 标识在投影规划阶段直接拒绝，不发起模块查询。
    """
    sync_chain = _SyncProjectionChain()
    async_chain = _AsyncProjectionChain()

    assert (
        MediaChain.convert_media_identity(
            sync_chain,
            target_source=MediaSource.TMDB,
            media_source=MediaSource.IMDb,
            media_id="27165187",
        )
        is None
    )
    assert (
        asyncio.run(
            MediaChain.async_convert_media_identity(
                async_chain,
                target_source=MediaSource.TMDB,
                media_source=MediaSource.IMDb,
                media_id="invalid",
            )
        )
        is None
    )
    assert sync_chain.imdb_tmdb_match == {}
    assert async_chain.imdb_tmdb_match == {}


def test_match_tmdbinfo_dispatcher_keeps_legacy_provider_compatible() -> None:
    """
    不接受可选 imdbid 的旧 provider 应被安全过滤参数并继续回退到支持 imdbid 的模块。
    """
    legacy_calls: list[dict[str, Any]] = []
    modern_calls: list[dict[str, Any]] = []

    class _LegacyProvider:
        """
        模拟仅声明原有四参数的旧 match_tmdbinfo 插件。
        """

        @staticmethod
        def get_name() -> str:
            """
            返回旧插件名称。
            """
            return "LegacyProvider"

        @staticmethod
        def get_priority() -> int:
            """
            设置较高优先级以确保先于默认模块执行。
            """
            return 0

        @staticmethod
        def match_tmdbinfo(name: str, mtype=None, year=None, season=None):
            """
            仅接受旧四参数，空名称时返回空以交由后续模块处理。
            """
            legacy_calls.append({"name": name, "mtype": mtype, "year": year, "season": season})
            return None

        @staticmethod
        async def async_match_tmdbinfo(name: str, mtype=None, year=None, season=None):
            """
            异步仅接受旧四参数，空名称时返回空以交由后续模块处理。
            """
            legacy_calls.append({"name": name, "mtype": mtype, "year": year, "season": season})
            return None

    class _ModernProvider:
        """
        模拟支持 imdbid 的 TMDB 宿主模块。
        """

        @staticmethod
        def get_name() -> str:
            """
            返回宿主模块名称。
            """
            return "ModernProvider"

        @staticmethod
        def get_priority() -> int:
            """
            设置低于旧插件的优先级以验证回退。
            """
            return 1

        @staticmethod
        def match_tmdbinfo(name: str = "", mtype=None, year=None, season=None, imdbid=None):
            """
            记录收到的完整参数并返回匹配的 TMDB 详情。
            """
            modern_calls.append(
                {"name": name, "mtype": mtype, "year": year, "season": season, "imdbid": imdbid}
            )
            return {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}

        @staticmethod
        async def async_match_tmdbinfo(
            name: str = "", mtype=None, year=None, season=None, imdbid=None
        ):
            """
            异步记录收到的完整参数并返回匹配的 TMDB 详情。
            """
            modern_calls.append(
                {"name": name, "mtype": mtype, "year": year, "season": season, "imdbid": imdbid}
            )
            return {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}

    module_manager = Mock()
    module_manager.get_running_modules.return_value = [_LegacyProvider(), _ModernProvider()]
    plugin_manager = Mock()
    plugin_manager.get_plugin_modules.return_value = {}
    chain = object.__new__(MediaChain)
    chain._module_dispatcher = ModuleInvocationDispatcher(
        module_catalog=module_manager,
        plugin_catalog=plugin_manager,
        plugin_error_handler=lambda *args, **kwargs: None,
        system_error_handler=lambda *args, **kwargs: None,
        rate_limit_handler=lambda *args, **kwargs: None,
    )

    result = chain.match_tmdbinfo(
        name="",
        mtype=None,
        year=None,
        season=None,
        imdbid="tt27165187",
    )
    async_result = asyncio.run(
        chain.async_match_tmdbinfo(
            name="",
            mtype=None,
            year=None,
            season=None,
            imdbid="tt27165187",
        )
    )

    expected = {"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}
    assert result == expected
    assert async_result == expected
    assert legacy_calls == [
        {"name": "", "mtype": None, "year": None, "season": None},
        {"name": "", "mtype": None, "year": None, "season": None},
    ]
    assert modern_calls == [
        {"name": "", "mtype": None, "year": None, "season": None, "imdbid": "tt27165187"},
        {"name": "", "mtype": None, "year": None, "season": None, "imdbid": "tt27165187"},
    ]


def test_subscribe_metadata_retry_converts_imdb_subscription_to_tmdb() -> None:
    """
    订阅元数据巡检应对保留 IMDb 身份的订阅重试 TMDB 身份转换。
    """
    subscribe = SubscriptionSnapshot(
        id=88,
        name="The End of Oak Street",
        year="2026",
        type=MediaType.MOVIE.value,
        media_source=MediaSource.IMDb.value,
        media_id="tt27165187",
    )
    meta = MetaInfo("The End of Oak Street 2026")
    meta.type = MediaType.MOVIE
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 1101383,
            "media_type": MediaType.MOVIE,
            "title": "逃出绝命街",
            "release_date": "2026-01-01",
            "vote_average": 7.5,
        }
    )
    media_chain = SimpleNamespace(
        convert_media_identity=Mock(
            return_value={"id": 1101383, "media_type": MediaType.MOVIE, "title": "逃出绝命街"}
        ),
        recognize_media=Mock(return_value=tmdb_media),
    )
    updates: list[dict[str, Any]] = []

    class _Owner(SubscribeMetadataOwner):
        """
        隔离订阅持久化写入的最小元数据巡检测试替身。
        """

        @staticmethod
        def _SubscribeChain__is_tmdb_recognize_source() -> bool:
            """
            固定模拟当前识别源为 TMDB。
            """
            return True

        @staticmethod
        def _SubscribeChain__apply_subscribe_update(
            current: SubscriptionSnapshot,
            update_data: dict[str, Any],
            *,
            scene: str,
        ) -> SubscriptionSnapshot:
            """
            记录更新字段并返回模拟更新后的快照。
            """
            assert scene == "identity_refresh"
            updates.append(update_data)
            return SubscriptionSnapshot(
                id=current.id,
                name=update_data["name"],
                year=update_data["year"],
                type=current.type,
                media_source=MediaSource.TMDB.value,
                media_id=update_data["media_id"],
            )

    updated = _Owner()._retry_subscribe_media_identity(subscribe, meta, lambda: media_chain)

    assert updated is not None
    assert updated.media_source == MediaSource.TMDB.value
    assert updated.media_id == "1101383"
    assert updated.name == "逃出绝命街"
    assert updates[0]["media_source"] == MediaSource.TMDB
    assert updates[0]["media_id"] == "1101383"
    media_chain.convert_media_identity.assert_called_once_with(
        target_source=MediaSource.TMDB,
        media_source=MediaSource.IMDb,
        media_id="tt27165187",
        mtype=MediaType.MOVIE,
        season=None,
    )
