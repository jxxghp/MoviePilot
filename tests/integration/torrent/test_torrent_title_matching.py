"""
种子标题片段匹配回归，防止带标点的英文单词被当作完整作品名。
"""

from unittest.mock import Mock

import pytest

from app.application.torrent.download import TorrentHelper
from app.chain.search import SearchChain
from app.chain.search import result as search_module
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def target() -> MediaInfo:
    """
    构造与 Issue #7019 相同的待播剧身份及英文别名。
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="279923",
        tmdb_id=279923,
        title="醉梦",
        original_title="醉梦",
        names=["Dream"],
        type=MediaType.TV,
        year="2026",
        season_years={1: "2026"},
        seasons={1: [1]},
    )


@pytest.mark.parametrize("fragment", ["Dream!", "Dream！", "(Dream)", "Dream?", "Dream。", "Dream\u200b"])
@pytest.mark.parametrize("field", ["title", "description"])
def test_english_fragment_cannot_match_alias_after_punctuation(target, fragment, field):
    """
    英文单词附带标点或零宽字符时仍不能成为标题片段证据。
    """
    if field == "title":
        target.original_title = "Dream"
    torrent = TorrentInfo(
        title=f"BanG {fragment} YUME MITA S01 1080p WEB-DL" if field == "title"
        else "BanG Dream YUME MITA S01 1080p WEB-DL",
        description=f"BanG {fragment} It's MyGO!!!!! | 01-13 日语+简繁字幕"
        if field == "description" else None,
        category=MediaType.TV.value,
    )
    meta = MetaInfo(title=torrent.title, subtitle=torrent.description)

    assert not TorrentHelper.match_torrent(target, meta, torrent)


@pytest.mark.parametrize("description", ["BanG Dream! It's MyGO!!!!! | 01-13 日语+简繁字幕", "【】 | !!!"])
def test_empty_normalized_alias_is_not_matching_evidence(target, description):
    """
    空白或纯标点别名不能与副标题里的空片段相交。
    """
    target.names = [" ", "!!!", "\u200b"]
    torrent = TorrentInfo(title="Unrelated S01 1080p WEB-DL", description=description)

    assert not TorrentHelper.match_torrent(target, MetaInfo(torrent.title), torrent)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("醉梦 S01 2026 1080p WEB-DL", None),
        ("Dream S01 2026 1080p WEB-DL", None),
        ("Dream! S01 2026 1080p WEB-DL", None),
        ("Other Title S01 2026 1080p WEB-DL", "【醉梦！】 | 国语中字"),
        ("Other Title S01 2026 1080p WEB-DL 【醉梦！】", None),
    ],
)
def test_complete_titles_and_chinese_fragments_still_match(target, title, description):
    """
    完整英文别名和有效中文片段继续匹配，不受英文碎片过滤影响。
    """
    torrent = TorrentInfo(title=title, description=description)
    meta = MetaInfo(title=title, subtitle=description)

    assert TorrentHelper.match_torrent(target, meta, torrent)


def test_complete_multiword_alias_still_matches(target):
    """
    英文多词别名仍按完整解析名称比较。
    """
    target.names = ["Drunken Dream!"]
    torrent = TorrentInfo(title="Drunken Dream S01 2026 1080p WEB-DL")

    assert TorrentHelper.match_torrent(target, MetaInfo(torrent.title), torrent)


@pytest.mark.parametrize("year", ["", "2026 "])
def test_subscription_search_drops_bang_dream_candidates(target, monkeypatch, year):
    """
    搜索结果在产生可下载候选之前排除带标点的 Dream 误匹配。
    """
    torrents = [
        TorrentInfo(
            title=f"BanG Dream YUME MITA S01 {year}1080p friDay WEB-DL AAC2.0 H.264-CHDWEB",
            description="BanG Dream! YUME∞MITA | 01-13 日语+简繁字幕",
            category=MediaType.TV.value,
        ),
        TorrentInfo(
            title="醉梦 S01 2026 1080p WEB-DL",
            description="醉梦 | 国语中字",
            category=MediaType.TV.value,
        ),
    ]
    media_chain = Mock()
    monkeypatch.setattr(search_module, "MediaChain", Mock(return_value=media_chain))
    monkeypatch.setattr(TorrentHelper, "sort_torrents", staticmethod(lambda contexts: contexts))

    contexts = object.__new__(SearchChain)._parse_result(
        torrents=torrents,
        mediainfo=target,
        rule_groups=[],
    )

    assert [context.torrent_info.title for context in contexts] == [torrents[1].title]
    media_chain.recognize_by_meta.assert_not_called()
