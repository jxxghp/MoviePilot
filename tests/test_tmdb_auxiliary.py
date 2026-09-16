from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.chain.media import MediaChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.schemas.category import ClassificationSelection
from app.schemas.types import MediaSource, MediaType


class _FakeTmdbModule:
    """返回固定 TMDB 结果，避免测试访问外部元数据服务。"""

    def __init__(self, result: MediaInfo):
        """保存测试需要返回的 TMDB 媒体信息。"""
        self.result = result

    def get_name(self) -> str:
        """返回模块展示名。"""
        return "FakeTmdbModule"

    def get_priority(self) -> int:
        """返回模块调度优先级。"""
        return 0

    def get_media_auxiliary_info(self, **kwargs):
        """与宿主附加信息模块一致：只应答 TMDB 来源的请求。"""
        if kwargs.get("media_source") != MediaSource.TMDB:
            return []
        return [self.result]


class _RecordingClassificationService:
    """记录附加信息收口参数，并返回隔离的分类结果副本。"""

    def __init__(self) -> None:
        """初始化收口调用记录。"""
        self.calls: list[tuple[MediaInfo, bool]] = []

    def finalize(
        self,
        media: MediaInfo,
        *,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> MediaInfo:
        """记录强制刷新语义并模拟执行器写入新分类。"""
        del effective_override
        self.calls.append((media, refresh))
        finalized = deepcopy(media)
        finalized.set_library_category("补充后分类")
        return finalized


def _make_chain(tmdb_media: MediaInfo) -> MediaChain:
    """构造经真实 dispatch 算法路由、但不加载真实模块的媒体处理链。"""
    module = _FakeTmdbModule(tmdb_media)
    module_manager = Mock()
    module_manager.get_running_modules.return_value = [module]
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
    return chain


def test_supplement_tmdb_keeps_primary_source_identity() -> None:
    """非 TMDB 主来源补充 TMDB 后，主身份和展示字段必须保持不变。"""
    primary = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="subject-42",
        type=MediaType.TV,
        title="原识别标题",
        year="2024",
        category="",
    )
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 12345,
            "media_type": MediaType.TV,
            "name": "TMDB 标题",
            "genre_ids": [16, 18],
            "external_ids": {"imdb_id": "tt12345", "tvdb_id": 6789},
        }
    )
    tmdb_media.category = "日本动画"

    result = _make_chain(tmdb_media).supplement_tmdb_info(
        primary, MetaInfo("原识别标题 2024")
    )

    assert result is primary
    assert result.media_source == MediaSource.AniList
    assert result.media_id == "subject-42"
    assert result.title == "原识别标题"
    assert result.tmdb_id == 12345
    assert result.genre_ids == [16, 18]
    assert result.category == ""
    assert result.library_category == ""


def test_supplement_tmdb_does_not_override_custom_category() -> None:
    """下载历史或目录指定的自定义分类优先于 TMDB 自动分类。"""
    primary = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="35593344",
        douban_id="35593344",
        type=MediaType.MOVIE,
        title="测试电影",
        category="纪录片",
    )
    tmdb_media = MediaInfo(
        tmdb_info={
            "id": 9876,
            "media_type": MediaType.MOVIE,
            "title": "Test Movie",
            "genre_ids": [28],
        }
    )
    tmdb_media.category = "动作片"

    result = _make_chain(tmdb_media).supplement_tmdb_info(primary)

    assert result.media_source == MediaSource.Douban
    assert result.media_id == "35593344"
    assert result.category == "纪录片"
    assert result.tmdb_id == 9876


def test_tmdb_supplement_uses_current_season_year_and_keeps_season_zero() -> None:
    """电视剧优先使用当前季年份，并且特别季季号不能退化为空。"""
    media = MediaInfo(
        media_source=MediaSource.Bangumi,
        media_id="42",
        type=MediaType.TV,
        title="测试动画",
        year="2020",
        season=0,
        season_years={0: "2024"},
    )

    tmdb_meta = MediaChain._build_tmdb_supplement_meta(
        media, MetaInfo("测试动画 S00 2020")
    )

    assert tmdb_meta.begin_season == 0
    assert tmdb_meta.year == "2024"


def test_multi_source_auxiliary_merges_aliases_without_source_category_side_effects() -> None:
    """多来源只合并标题候选和 TMDB 兼容字段，目录分类留给统一收口。"""
    primary = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="1",
        type=MediaType.TV,
        title="葬送的芙莉莲",
        names=["Frieren"],
    )
    anilist = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="154587",
        type=MediaType.TV,
        title="Sousou no Frieren",
        names=["FRIEREN", "Frieren: Beyond Journey's End"],
        category="动画冲突分类",
        genre_ids=[99],
        imdb_id="tt-conflict",
    )
    tmdb = MediaInfo(
        tmdb_info={
            "id": 209867,
            "media_type": MediaType.TV,
            "name": "Frieren: Beyond Journey's End",
            "genre_ids": [16, 18],
            "external_ids": {"imdb_id": "tt22248376", "tvdb_id": 424536},
        }
    )
    tmdb.category = "日本动画"

    result = MediaChain._merge_media_auxiliary(
        primary,
        [anilist, tmdb],
        (MediaSource.AniList, MediaSource.TMDB),
    )

    assert result.names == [
        "葬送的芙莉莲",
        "Frieren",
        "Sousou no Frieren",
        "Frieren: Beyond Journey's End",
    ]
    assert result.category == ""
    assert result.library_category == ""
    assert result.genre_ids == [16, 18]
    assert result.imdb_id == "tt22248376"
    assert result.tvdb_id == 424536


def test_supplement_media_info_uses_configured_source_union() -> None:
    """未显式传来源时，Chain 应把用户的 SEARCH_SOURCE 多选完整传给 provider。"""
    primary = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="1",
        type=MediaType.TV,
        title="测试剧",
    )
    chain = object.__new__(MediaChain)
    chain.run_module = Mock(return_value=[])

    with patch(
        "app.chain.media.auxiliary.get_chain_runtime_config_snapshot",
        return_value=SimpleNamespace(search_source="douban,themoviedb,anilist"),
    ):
        result = chain.supplement_media_info(primary)

    assert result is primary
    chain.run_module.assert_called_once_with(
        "get_media_auxiliary_info",
        mediainfo=primary,
        media_source=(MediaSource.Douban, MediaSource.TMDB, MediaSource.AniList),
        metainfo=None,
    )


def test_supplement_media_info_forces_classification_refresh() -> None:
    """附加来源改变标准事实后必须强制刷新同 revision 分类结果。"""
    primary = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="42",
        type=MediaType.MOVIE,
        title="测试动画",
    )
    tmdb = MediaInfo(
        tmdb_info={
            "id": 42,
            "media_type": "movie",
            "genre_ids": [16],
            "production_countries": [{"iso_3166_1": "JP"}],
        }
    )
    chain = _make_chain(tmdb)
    classifier = _RecordingClassificationService()
    chain.classification_service = classifier

    result = chain.supplement_media_info(
        primary,
        media_source=MediaSource.TMDB,
    )

    assert result is not primary
    assert result.library_category == "补充后分类"
    assert len(classifier.calls) == 1
    supplemented, refresh = classifier.calls[0]
    assert refresh is True
    assert supplemented.tmdb_id == 42
    assert supplemented.genre_ids == [16]
