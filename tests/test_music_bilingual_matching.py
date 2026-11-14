"""
双语作品名与艺人署名在音乐资源搜索和订阅复核中的匹配边界。
"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain.search import SearchChain
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MusicInfo, TorrentInfo
from app.domain.music import match_music_resource, music_artist_matches, music_title_matches
from app.schemas.types import MediaType
from tests.test_music_resource_names import music_engine as music_engine
from tests.test_music_subscribe import _subscribe


@pytest.mark.usefixtures("music_engine")
@pytest.mark.parametrize("music_type,title,artist,aliases,resource", [
    (
        "recording", "晴天", "周杰伦", ["Jay Chou"],
        "周杰伦 (Jay Chou) - 晴天 FLAC",
    ),
    (
        "album", "江南多雨时", "程响", ["Cheng Xiang"],
        "程响 (Cheng Xiang) - 江南多雨时 [2020] [单曲专辑] FLAC / Lossless / WEB / 2020",
    ),
    (
        "album", "âge 20th Archives Song Archives", "Various Artists", [],
        "群星 (Various Artists) - âge 20th Archives Song Archives [2020] [合辑] "
        "FLAC / Lossless / Log (100%) / Cue / CD / 2020",
    ),
    (
        "album", "南泥湾", "Various Artists", [],
        "群星 (Various Artists) - 南泥湾 (Nanni Bay) [1990] [专辑] "
        "FLAC / Lossless / Log (100%) / Cue / CD / 自抓 / 1990",
    ),
])
def test_bilingual_music_resource_matches_verified_names(music_type, title, artist, aliases, resource):
    """
    双语署名各部分均属于已确认实体时，同步搜索与订阅复核应保留资源。
    """
    target = MusicInfo(
        media_source="musicbrainz", media_id="target", music_type=music_type,
        title=title, title_aliases=["Nanni Bay"] if title == "南泥湾" else [],
        artists=[artist], artist_aliases=aliases,
    )
    snapshot = deepcopy(target.to_dict())
    torrent = TorrentInfo(title=resource, category=MediaType.MUSIC.value, site=1)

    contexts = SearchChain()._build_music_contexts([torrent], target, rule_groups=[])

    assert len(contexts) == 1
    assert contexts[0].match_status == "exact"
    assert contexts[0].meta_info.media_id is None
    assert len(SubscribeChain()._filter_music_subscribe_contexts(
        _subscribe(name=title, music_type=music_type), target, [Context(torrent_info=torrent)],
    )) == 1
    assert target.to_dict() == snapshot


@pytest.mark.parametrize("credit", ["周杰伦 (Jay Chou)", "Jay Chou（周杰倫）", "周杰伦(JAY CHOU)"])
def test_bilingual_artist_credit_requires_verified_aliases(credit):
    """
    中文与外文署名均须由同一目标的正式名称或来源别名确认。
    """
    target = MusicInfo(title="晴天", artists=["周杰伦"], artist_aliases=["Jay Chou"])

    assert music_artist_matches(target, [credit])
    assert not music_artist_matches(MusicInfo(title="晴天", artists=["周杰伦"]), [credit])
    assert not music_artist_matches(target, ["另一位歌手 (Jay Chou)"])


@pytest.mark.parametrize("title", ["南泥湾 (Nanni Bay)", "Nanni Bay（南泥灣）"])
def test_bilingual_title_requires_both_verified_names(title):
    """
    仅当括号两侧均为可信名称时允许组合名称，未知副标题继续保留作品边界。
    """
    target = MusicInfo(music_type="album", title="南泥湾", title_aliases=["Nanni Bay"])

    assert music_title_matches(target, title)
    assert not music_title_matches(MusicInfo(music_type="album", title="南泥湾"), title)
    assert not music_title_matches(target, "另一张专辑 (Nanni Bay)")


@pytest.mark.parametrize("artist", ["Artist (2)", "Sun O)))"])
def test_complete_artist_name_keeps_its_literal_parentheses(artist):
    """
    来源正式署名自身包含括号时，完整名称仍优先于双语格式解释。
    """
    target = MusicInfo(title="Song", artists=[artist])

    assert music_artist_matches(target, [artist])
    assert not music_artist_matches(target, ["Artist"])


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_bilingual_resource_survives_all_music_search_entries(monkeypatch, mode):
    """
    三个搜索入口在真实解析和匹配后都应返回双语署名资源，只替换站点请求。
    """
    chain = SearchChain()
    target = MusicInfo(title="晴天", artists=["周杰伦"], artist_aliases=["Jay Chou"])
    torrent = TorrentInfo(title="周杰伦 (Jay Chou) - 晴天 FLAC", category=MediaType.MUSIC.value)
    monkeypatch.setattr(chain, "_SearchChain__search_all_sites", Mock(return_value=[torrent]))
    monkeypatch.setattr(chain, "_SearchChain__async_search_all_sites", AsyncMock(return_value=[torrent]))

    async def site_stream(**_kwargs):
        """
        从离线站点返回原始资源，保留后续真实搜索处理。
        """
        yield {"items": [torrent], "stage": "searching", "value": 100}

    async def collect():
        """
        调用异步或流式入口，读取最终的匹配上下文。
        """
        if mode == "async":
            return await chain.async_process(target, rule_groups=[])
        events = [event async for event in chain.async_process_stream(target, rule_groups=[])]
        assert events[-1]["total_items"] == 1
        return events[-1]["contexts"]

    monkeypatch.setattr(chain, "_SearchChain__async_search_all_sites_stream", site_stream)

    contexts = chain.process(target, rule_groups=[]) if mode == "sync" else asyncio.run(collect())

    assert len(contexts) == 1
    assert contexts[0].match_status == "exact"
    assert contexts[0].meta_info.media_id is None


@pytest.mark.usefixtures("music_engine")
@pytest.mark.parametrize("title,reason", [
    ("周杰伦 (Other Singer) - 晴天 FLAC", "artist_unverified"),
    ("Other Singer (Jay Chou) - 晴天 FLAC", "artist_unverified"),
    ("周杰伦 (Jay Chou) - 晴天 (Unknown Subtitle) FLAC", "title_mismatch"),
    ("周杰伦 (Jay Chou) - 晴天 (叶惠美) FLAC", "title_mismatch"),
    ("周杰伦 (Jay Chou) - 晴天 (Live) FLAC", "version_mismatch"),
    ("周杰伦 (Jay Chou) - 叶惠美 FLAC", "related_album"),
])
def test_bilingual_credit_preserves_automatic_matching_limits(title, reason):
    """
    双语排版兼容不能放行不同艺人、未知副标题、现场版或仅有关联的整专。
    """
    target = MusicInfo(title="晴天", artists=["周杰伦"], artist_aliases=["Jay Chou"], album="叶惠美")

    result = match_music_resource(target, title)

    assert result.status != "exact"
    assert result.reason == reason


def test_bilingual_related_album_keeps_recording_identity_separate():
    """
    专辑名的双语别名只证明单曲归属，不能让整专取得单曲的精确身份。
    """
    target = MusicInfo(title="晴天", artists=["周杰伦"], album="叶惠美", album_aliases=["Ye Hui Mei"])

    result = match_music_resource(target, "周杰伦 - 叶惠美 (Ye Hui Mei) FLAC")

    assert (result.status, result.reason) == ("album", "related_album")
