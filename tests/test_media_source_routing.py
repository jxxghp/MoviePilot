from unittest.mock import Mock, patch

from app.chain import ChainBase
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.schemas.types import MediaType


def _chain_without_init() -> ChainBase:
    """构造不加载真实模块和外部服务的识别链实例。"""
    return object.__new__(ChainBase)


def test_generic_source_id_wins_over_legacy_ids() -> None:
    """显式source与media_id应优先于同一请求残留的兼容ID字段。"""
    resolved = ChainBase._resolve_media_source_params(
        source="anilist",
        mediaid="154587",
        tmdbid=999,
        doubanid="888",
    )

    assert resolved == ("anilist", None, None, None, 154587)


def test_explicit_source_recognition_runs_system_modules_only() -> None:
    """显式选择数据源时应跳过插件并只向系统模块传递该来源ID。"""
    chain = _chain_without_init()
    media = MediaInfo(
        anilist_info={
            "id": 154587,
            "title": {"english": "Frieren"},
            "format": "TV",
        }
    )
    chain.run_module = Mock(return_value=media)

    with patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ):
        result = chain.recognize_media(
            source="anilist",
            mediaid="154587",
            tmdbid=999,
            mtype=MediaType.TV,
        )

    assert result is media
    call = chain.run_module.call_args
    assert call.kwargs["system_only"] is True
    assert call.kwargs["source"] == "anilist"
    assert call.kwargs["anilistid"] == 154587
    assert call.kwargs["tmdbid"] is None


def test_default_recognition_preserves_plugin_method_contract() -> None:
    """未显式选择来源时不应向既有插件额外传递source参数。"""
    chain = _chain_without_init()
    media = MediaInfo(title="测试电影", type=MediaType.MOVIE, tmdb_id=1)
    chain.run_module = Mock(return_value=media)
    meta = MetaBase("测试电影")
    meta.cn_name = "测试电影"
    meta.type = MediaType.MOVIE

    with patch(
        "app.chain.MoviePilotServerHelper.report_recognize_share",
        return_value=False,
    ):
        result = chain.recognize_media(meta=meta)

    assert result is media
    call = chain.run_module.call_args
    assert call.kwargs["system_only"] is False
    assert "source" not in call.kwargs
    assert "anilistid" not in call.kwargs


def test_system_only_module_dispatch_skips_plugins() -> None:
    """模块调度的system_only模式不得执行插件模块。"""
    chain = _chain_without_init()
    chain._ChainBase__execute_plugin_modules = Mock(return_value="plugin")
    chain._ChainBase__execute_system_modules = Mock(return_value="system")

    result = chain.run_module("search_medias", system_only=True, meta=MetaBase("test"))

    assert result == "system"
    chain._ChainBase__execute_plugin_modules.assert_not_called()
    chain._ChainBase__execute_system_modules.assert_called_once()


def test_explicit_search_source_uses_system_only_dispatch() -> None:
    """请求级搜索来源应进入仅系统模块调度。"""
    chain = _chain_without_init()
    chain.run_module = Mock(return_value=[])
    meta = MetaBase("Frieren")

    result = chain.search_medias(meta, source="anilist")

    assert result == []
    chain.run_module.assert_called_once_with(
        "search_medias",
        meta=meta,
        source="anilist",
        system_only=True,
    )
