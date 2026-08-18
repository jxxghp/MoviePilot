from unittest.mock import Mock, patch

from app.application.orchestration import ChainBase
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import MediaSource, MediaType
from app.domain.media import parse_media_source_selection
from app.schemas.media import build_media_key, resolve_media_identity


def _chain_without_init() -> ChainBase:
    """构造不加载真实模块和外部服务的识别链实例。"""
    chain = object.__new__(ChainBase)
    chain.eventmanager = Mock(check=Mock(return_value=False))
    return chain


def test_generic_source_id_resolves_as_fixed_enum() -> None:
    """规范来源和原生 ID 应组成唯一媒体身份。"""
    assert resolve_media_identity(
        media_source="anilist",
        media_id="154587",
    ) == (MediaSource.AniList, "154587")


def test_iqiyi_source_routes_through_unified_identity() -> None:
    """爱奇艺探索来源应支持选择解析、身份解析和媒体键构造。"""
    assert parse_media_source_selection("iqiyi,iqiyidiscover") == (
        MediaSource.Iqiyi,
    )
    assert resolve_media_identity(
        media_source="iqiyi",
        media_id="album-1",
    ) == (MediaSource.Iqiyi, "album-1")
    assert build_media_key("iqiyi", "album-1") == "iqiyidiscover:album-1"


def test_plugin_source_is_preserved_as_dynamic_enum() -> None:
    """格式合法的插件来源应作为动态枚举成员进入通用识别链。"""
    assert resolve_media_identity(
        media_source="plugin_source",
        media_id="custom-1",
    ) == (MediaSource("plugin_source"), "custom-1")


def test_invalid_source_identifier_is_rejected() -> None:
    """包含空格或分隔符的来源标识不得进入通用识别链。"""
    assert resolve_media_identity(
        media_source="Plugin Source:Invalid",
        media_id="custom-1",
    ) == (None, None)


def test_zero_media_id_is_rejected() -> None:
    """零值 ID 是旧表占位符，不得重新进入统一媒体身份链路。"""
    assert resolve_media_identity(
        media_source=MediaSource.TMDB,
        media_id="0",
    ) == (None, None)


def test_explicit_source_recognition_reaches_modules_with_unified_identity() -> None:
    """显式身份应只以 media_source 和 media_id 进入模块调度。"""
    chain = _chain_without_init()
    media = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="154587",
        type=MediaType.TV,
        title="Frieren",
    )
    chain.unicast = Mock(return_value=media)

    with patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ):
        result = chain.recognize_media(
            media_source=MediaSource.AniList,
            media_id="154587",
            mtype=MediaType.TV,
        )

    assert result is media
    call = chain.unicast.call_args
    assert call.kwargs["media_source"] == MediaSource.AniList
    assert call.kwargs["media_id"] == "154587"
    assert not {
        "source", "mediaid", "tmdbid", "doubanid", "bangumiid", "anilistid"
    }.intersection(call.kwargs)


def test_default_recognition_passes_empty_generic_identity() -> None:
    """默认识别也只向模块传递为空的统一媒体身份。"""
    chain = _chain_without_init()
    media = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1",
        title="测试电影",
        type=MediaType.MOVIE,
        tmdb_id=1,
    )
    chain.unicast = Mock(return_value=media)
    meta = MetaBase("测试电影")
    meta.cn_name = "测试电影"
    meta.type = MediaType.MOVIE

    with patch(
        "app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ):
        result = chain.recognize_media(meta=meta)

    assert result is media
    call = chain.unicast.call_args
    assert call.kwargs["media_source"] is None
    assert call.kwargs["media_id"] is None


def test_module_dispatch_always_reaches_plugins() -> None:
    """模块调度必须始终先执行插件模块。"""
    chain = _chain_without_init()
    chain._module_dispatcher = Mock()
    chain._module_dispatcher.dispatch.return_value = "plugin"

    result = chain.run_module("search_medias", meta=MetaBase("test"))

    assert result == "plugin"
    chain._module_dispatcher.dispatch.assert_called_once()


def test_explicit_search_source_reaches_plugins() -> None:
    """请求级搜索来源应以统一字段进入完整模块调度。"""
    chain = _chain_without_init()
    chain.multicast = Mock(return_value=[])
    meta = MetaBase("Frieren")

    result = chain.search_medias(meta, media_source=MediaSource.AniList)

    assert result == []
    chain.multicast.assert_called_once_with(
        "search_medias",
        meta=meta,
        media_source=MediaSource.AniList,
    )
