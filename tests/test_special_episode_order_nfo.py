"""Season 0 NFO 的 TVDB 特典播放位置补全回归测试。"""

import subprocess
import sys
from unittest.mock import Mock
from xml.dom import minidom

import pytest

from app.chain import specials as specials_module
from app.chain.scraping import ScrapingChain
from app.domain.context import MediaInfo
from app.modules import thetvdb as tvdb_module
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.thetvdb import TheTvDbModule
from app.schemas.types import MediaType

_DEFAULT = object()


def test_scraping_import_defers_special_episode_order_module():
    """冷导入刮削链和普通单集刮削均不加载特典补全模块。"""
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys\n"
            "from app.testing.bootstrap import ensure_sites_stub\n"
            "ensure_sites_stub()\n"
            "import app.chain.scraping\n"
            "assert 'app.chain.specials' not in sys.modules\n"
            "chain = object.__new__(app.chain.scraping.ScrapingChain)\n"
            "chain.run_module = lambda method, **kwargs: '<episodedetails/>'\n"
            "chain.metadata_nfo(meta=None, mediainfo=None, season=1, episode=1)\n"
            "assert 'app.chain.specials' not in sys.modules\n"
        )],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _nfo(*, as_bytes: bool = True, extra: str = "", root: str = "episodedetails"):
    """构造带 TMDb 单集身份的基础 NFO。"""
    content = (
        f'<{root}><tmdbid>99</tmdbid><season>0</season><episode>5</episode>'
        f"{extra}</{root}>"
    )
    return content.encode("utf-8") if as_bytes else content


def _media(**overrides) -> MediaInfo:
    """构造使用 TMDb 默认季集顺序的电视剧。"""
    return MediaInfo(type=MediaType.TV, tmdb_id=123, title="测试剧集", **overrides)


def _chain(monkeypatch, nfo, external_ids=_DEFAULT, tvdb_info=_DEFAULT):
    """以隔离的模块调度替身调用真实 ScrapingChain 方法。"""
    monkeypatch.setattr(
        specials_module,
        "get_runtime_setting",
        lambda key: {"SCRAP_SOURCE": "themoviedb", "TVDB_V4_API_KEY": "configured"}[key],
    )
    responses = {
        "metadata_nfo": nfo,
        "tmdb_episode_external_ids": {"id": 99, "tvdb_id": 456}
        if external_ids is _DEFAULT
        else external_ids,
        "tvdb_episode_extended": {"airsBeforeSeason": 2, "airsBeforeEpisode": 7}
        if tvdb_info is _DEFAULT
        else tvdb_info,
    }
    chain = object.__new__(ScrapingChain)
    dispatcher = Mock(side_effect=lambda method, **_kwargs: responses[method])
    monkeypatch.setattr(chain, "run_module", dispatcher)
    return chain, dispatcher


def _tags(content) -> dict[str, str]:
    """读取单集根节点的直接子标签。"""
    root = minidom.parseString(content).documentElement
    return {
        node.tagName: "".join(child.data for child in node.childNodes if child.nodeType == child.TEXT_NODE)
        for node in root.childNodes
        if node.nodeType == node.ELEMENT_NODE
    }


@pytest.mark.parametrize("as_bytes", [True, False])
def test_special_order_enriches_matching_episode_and_preserves_type(monkeypatch, as_bytes):
    """精确的单集映射只补非空位置，且保持 NFO 的原始类型。"""
    chain, dispatcher = _chain(monkeypatch, _nfo(as_bytes=as_bytes))

    result = chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5)

    assert isinstance(result, bytes if as_bytes else str)
    tags = _tags(result)
    assert tags["airsbefore_season"] == "2"
    assert tags["airsbefore_episode"] == "7"
    assert "airsafter_season" not in tags
    assert [call.args[0] for call in dispatcher.call_args_list] == [
        "metadata_nfo", "tmdb_episode_external_ids", "tvdb_episode_extended"
    ]
    assert dispatcher.call_args_list[1].kwargs == {
        "tmdbid": 123, "season": 0, "episode": 5
    }
    assert dispatcher.call_args_list[2].kwargs == {"episode_id": 456}


@pytest.mark.parametrize("as_bytes", [True, False])
def test_enrichment_does_not_add_blank_lines_to_pretty_nfo(monkeypatch, as_bytes):
    """已缩进的基础 NFO 补全后不产生多余空行。"""
    original = (
        "<episodedetails>\n"
        "  <tmdbid>99</tmdbid>\n"
        "  <season>0</season>\n"
        "  <episode>5</episode>\n"
        "</episodedetails>\n"
    )
    chain, _ = _chain(monkeypatch, original.encode("utf-8") if as_bytes else original)

    result = chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5)

    assert isinstance(result, bytes if as_bytes else str)
    content = result.decode("utf-8") if isinstance(result, bytes) else result
    assert "\n\n" not in content
    assert "  <tmdbid>99</tmdbid>\n" in content
    assert "  <airsbefore_season>2</airsbefore_season>\n" in content


def test_special_order_preserves_existing_fields_and_zero(monkeypatch):
    """已有位置不覆盖，TVDB 的零值不因 truthiness 丢失。"""
    original = _nfo(extra="<airsbefore_season>3</airsbefore_season>")
    chain, _ = _chain(
        monkeypatch,
        original,
        tvdb_info={"airsAfterSeason": 0, "airsBeforeSeason": 2, "airsBeforeEpisode": 7},
    )

    tags = _tags(chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5))

    assert tags["airsafter_season"] == "0"
    assert tags["airsbefore_season"] == "3"
    assert tags["airsbefore_episode"] == "7"


@pytest.mark.parametrize(
    ("media", "season", "episode"),
    [
        (_media(), 1, 5),
        (_media(episode_group="group-1"), 0, 5),
        (_media(scrape_source="douban"), 0, 5),
        (_media(), 0, None),
        (MediaInfo(type=MediaType.MOVIE, tmdb_id=123), 0, 5),
    ],
)
def test_non_matching_nfo_never_queries_external_sources(monkeypatch, media, season, episode):
    """非目标集、剧集组与其他刮削源不增加请求。"""
    original = _nfo()
    chain, dispatcher = _chain(monkeypatch, original)

    assert chain.metadata_nfo(meta=Mock(), mediainfo=media, season=season, episode=episode) is original
    dispatcher.assert_called_once()


def test_missing_tvdb_key_skips_both_external_queries(monkeypatch):
    """运行时密钥为空时，连 TMDb external IDs 也不查询。"""
    original = _nfo()
    chain, dispatcher = _chain(monkeypatch, original)
    monkeypatch.setattr(
        specials_module,
        "get_runtime_setting",
        lambda key: "" if key == "TVDB_V4_API_KEY" else "themoviedb",
    )

    assert chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5) is original
    dispatcher.assert_called_once()


@pytest.mark.parametrize("value", [None, "", "abc", 0, -1, True])
def test_invalid_tvdb_id_skips_tvdb_lookup(monkeypatch, value):
    """无效或缺失的单集 TVDB ID 不进入扩展详情查询。"""
    original = _nfo()
    chain, dispatcher = _chain(monkeypatch, original, external_ids={"tvdb_id": value})

    assert chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5) is original
    assert [call.args[0] for call in dispatcher.call_args_list] == [
        "metadata_nfo", "tmdb_episode_external_ids"
    ]


@pytest.mark.parametrize(
    "external_ids",
    [{}, {"tvdb_id": None}, {"id": 98, "tvdb_id": 456}],
)
def test_missing_or_mismatched_tmdb_mapping_preserves_nfo(monkeypatch, external_ids):
    """无映射或单集身份与 NFO 不符时，不查询 TVDB。"""
    original = _nfo()
    chain, dispatcher = _chain(monkeypatch, original, external_ids=external_ids)

    assert chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5) is original
    assert dispatcher.call_count == 2


@pytest.mark.parametrize(
    "tvdb_info",
    [None, {}, {"airsAfterSeason": None, "airsBeforeSeason": None, "airsBeforeEpisode": None}],
)
def test_missing_special_position_preserves_original_nfo(monkeypatch, tvdb_info):
    """TVDB 未提供可用位置时不重新序列化基础 NFO。"""
    original = _nfo()
    chain, _ = _chain(monkeypatch, original, tvdb_info=tvdb_info)

    assert chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5) is original


@pytest.mark.parametrize(
    "original",
    [_nfo(root="movie"), _nfo(extra="<airsbefore_season>1</airsbefore_season>"
                            "<airsafter_season>1</airsafter_season>"
                            "<airsbefore_episode>7</airsbefore_episode>"), b"<episodedetails>"],
)
def test_unsuitable_xml_does_not_query_external_sources(monkeypatch, original):
    """非单集、字段齐全或无效 XML 不触发新增网络查询。"""
    chain, dispatcher = _chain(monkeypatch, original)

    assert chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5) is original
    dispatcher.assert_called_once()


@pytest.mark.parametrize("failed_method", ["tmdb_episode_external_ids", "tvdb_episode_extended"])
def test_enrichment_failure_preserves_base_nfo(monkeypatch, failed_method):
    """可选补全异常不会使基础 NFO 刮削失败。"""
    original = _nfo()
    chain, _ = _chain(monkeypatch, original)
    dispatcher = Mock(
        side_effect=lambda method, **_kwargs: original
        if method == "metadata_nfo"
        else (_ for _ in ()).throw(RuntimeError("offline"))
        if method == failed_method
        else {"id": 99, "tvdb_id": 456}
    )
    monkeypatch.setattr(chain, "run_module", dispatcher)

    assert chain.metadata_nfo(meta=Mock(), mediainfo=_media(), season=0, episode=5) is original


def test_tmdb_external_ids_capability_reuses_episode_client():
    """TMDb 两层能力调用现有 Episode 客户端并隔离失败。"""
    api = object.__new__(TmdbApi)
    api.episode_obj = Mock()
    api.episode_obj.external_ids.return_value = {"tvdb_id": 456}
    module = object.__new__(TheMovieDbModule)
    module.tmdb = api

    assert module.tmdb_episode_external_ids(123, 0, 5) == {"tvdb_id": 456}
    api.episode_obj.external_ids.assert_called_once_with(
        tv_id=123, season_num=0, episode_num=5
    )
    api.episode_obj.external_ids.side_effect = RuntimeError("offline")
    assert api.get_tv_episode_external_ids(123, 0, 5) == {}


def test_tvdb_episode_capability_uses_current_session_and_handles_missing_key(monkeypatch):
    """TVDB 能力使用现有会话；缺密钥时不初始化客户端。"""
    module = object.__new__(TheTvDbModule)
    call = Mock(return_value={"airsAfterSeason": 1})
    monkeypatch.setattr(module, "_handle_tvdb_call", call)
    monkeypatch.setattr(tvdb_module, "get_runtime_setting", lambda _key: "")
    assert module.tvdb_episode_extended(456) is None
    call.assert_not_called()

    monkeypatch.setattr(tvdb_module, "get_runtime_setting", lambda _key: "configured")
    assert module.tvdb_episode_extended(456) == {"airsAfterSeason": 1}
    call.assert_called_once_with("get_episode_extended", 456)
    call.side_effect = RuntimeError("offline")
    assert module.tvdb_episode_extended(456) is None
