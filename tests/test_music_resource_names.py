"""音乐站点发行命名的解析和搜索匹配回归。"""

import pytest

from app.adapters.system import rust
from app.chain.search import SearchChain
from app.domain.context import MusicInfo, TorrentInfo
from app.domain.meta import runtime
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import match_music_resource
from app.schemas.types import MediaType

RESOURCE_TITLES = [
    "赵雷-署前街少年 2022 - WEB-DL - 24bit ALAC-HHWEB",
    "赵雷 - 署前街少年 2022 - FLAC TJUPT",
    "[赵雷-署前街少年][2022][Hi-Res 44 1kHz_24bit][FLAC]",
    "赵雷 - 署前街少年 2022 - WEB-DL - 24bit ALAC - HHWEB",
]


@pytest.fixture(params=["python", "rust"])
def music_engine(request, monkeypatch):
    """分别验证纯 Python 和实际 Rust 解析器，恢复全局加速器配置。"""
    if request.param == "rust":
        assert rust.is_available(), "锁定环境应安装 moviepilot-rust"
        monkeypatch.setattr(rust, "is_enabled", lambda: True)

        def reject_python_fallback(*_args, **_kwargs):
            raise AssertionError("Rust 资源名回归不能用 Python 回退冒充通过")

        monkeypatch.setattr(MetaMusic, "_prepare_name_context", reject_python_fallback)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if request.param == "rust" else None)


@pytest.mark.usefixtures("music_engine")
@pytest.mark.parametrize("title", RESOURCE_TITLES)
def test_album_release_names_match_without_target_assisted_parsing(title):
    """真实命名变体应独立解析身份，并通过专辑搜索的严格匹配。"""
    meta = MetaMusic.parse_resource(title)
    assert (meta.title, meta.artists, meta.year) == ("署前街少年", ["赵雷"], 2022)
    assert meta.org_string == title
    assert meta.audio_format == ("ALAC" if "ALAC" in title else "FLAC")
    assert meta.audio_lossless is True
    target = MusicInfo(music_type="album", title="署前街少年", artists=["赵雷"], year="2022")
    assert match_music_resource(target, title).status == "exact"


@pytest.mark.usefixtures("music_engine")
def test_album_search_preserves_resources_from_different_sites():
    """同一专辑的站点命名差异不能隐藏有折扣的候选。"""
    target = MusicInfo(music_type="album", title="署前街少年", artists=["赵雷"], year="2022")
    torrents = [
        TorrentInfo(title=title, category=MediaType.MUSIC.value, site_name=f"Site{index}",
                    size=599533814, downloadvolumefactor=0.5 if index == 0 else 1,
                    hit_and_run=index != 0)
        for index, title in enumerate(RESOURCE_TITLES)
    ]
    contexts = SearchChain()._build_music_contexts(torrents, target, rule_groups=[])
    assert [context.torrent_info for context in contexts] == torrents
    assert contexts[0].torrent_info.downloadvolumefactor == 0.5
    assert contexts[0].torrent_info.hit_and_run is False


@pytest.mark.usefixtures("music_engine")
@pytest.mark.parametrize("title,reason", [
    ("赵雷-成都 2022 - FLAC TJUPT", "title_mismatch"),
    ("其他艺人-署前街少年 2022 - FLAC TJUPT", "artist_unverified"),
    ("赵雷-署前街少年 2021 - FLAC TJUPT", "year_mismatch"),
    ("赵雷-署前街少年 (Live) 2022 - FLAC TJUPT", "version_mismatch"),
    ("[赵雷-署前街少年 (Remix)][2022][FLAC]", "version_mismatch"),
])
def test_release_normalization_preserves_identity_constraints(title, reason):
    """清理发行标记不能放宽作品、艺人、年份和版本的原有匹配约束。"""
    target = MusicInfo(music_type="album", title="署前街少年", artists=["赵雷"], year="2022")
    result = match_music_resource(target, title)
    assert result.status != "exact"
    assert result.reason == reason


@pytest.mark.usefixtures("music_engine")
@pytest.mark.parametrize("title", [
    "为你盛开——许巍",
    "署前街少年-赵雷 FLAC",
    "Artist - Best Album FLAC",
    "Artist - Single Ladies FLAC",
    "Artist - Live At Montreux 1999 2022 FLAC",
    "Artist - Album 2022 - FLAC LIVE",
    "Artist - Album 2022 - FLAC DELUXE",
    "Artist - Album 2022 - FLAC REMASTERED",
    "Artist - Album 2022 - FLAC RE-RECORDED",
    "Artist - Album 2022 - FLAC Bonus Track",
])
def test_release_normalization_preserves_other_naming_styles(title):
    """缺少明确发行尾链时沿用原解析规则，不能把自然语言尾段当发布组。"""
    assert MetaMusic._normalize_resource_release(title) == title
    resource = MetaMusic.parse_resource(title)
    query = MetaMusic.parse_query(title)
    assert (resource.title, resource.artists, resource.year) == (query.title, query.artists, query.year)


@pytest.mark.usefixtures("music_engine")
@pytest.mark.parametrize("title,artists", [
    ("Artist - Album 2022 - FLAC", ["Artist"]),
    ("Album 2022 - FLAC", []),
    ("[Artist - Album][2022][FLAC]", ["Artist"]),
])
def test_release_names_without_group_or_cjk_credit(title, artists):
    """规格不带发布组也应保留年份，且不能为缺少署名的资源补写艺术家。"""
    meta = MetaMusic.parse_resource(title)
    assert (meta.title, meta.artists, meta.year, meta.audio_format) == ("Album", artists, 2022, "FLAC")
