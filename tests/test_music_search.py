import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.chain.search import SearchChain
from app.domain.context import MusicInfo, TorrentInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaSource, MediaType


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_music_search_keeps_searching_after_final_audio_filter(mode):
    """首轮名称命中但音质不符时，三条入口均须继续查询艺术家组合词。"""
    chain = SearchChain()
    chain.runtime_config = replace(chain.runtime_config, search_multiple_name=False)
    target = MusicInfo(music_type="album", title="Test Album", artists=["Test Artist"])
    calls = []

    def batch(**kwargs):
        """首轮模拟有损音质，后续组合词返回无损资源。"""
        calls.append(kwargs["keyword"])
        codec = "MP3" if len(calls) == 1 else "FLAC"
        return [TorrentInfo(title=f"Test Artist - Test Album {codec}", category=MediaType.MUSIC.value)]

    async def stream(**kwargs):
        """从同一站点样本生成进度事件。"""
        yield {"items": batch(**kwargs), "stage": "searching", "value": 100}

    async def async_batch(**kwargs):
        """普通异步端口与流式端口读取相同站点样本。"""
        return batch(**kwargs)

    async def collect():
        """调用对应异步入口并返回最终结果。"""
        params = {"mediainfo": target, "rule_groups": [], "filter_params": {"audio_format": "FLAC"}}
        if mode == "async":
            return await chain._async_process_music(**params)
        events = [event async for event in chain._async_process_music_stream(**params)]
        assert events[-1]["candidate_items"] == 2
        assert events[-1]["match_counts"]["filter_params"] == 1
        return events[-1]["contexts"]

    with (
        patch.object(chain, "_SearchChain__search_all_sites", side_effect=batch),
        patch.object(chain, "_SearchChain__async_search_all_sites", side_effect=async_batch),
        patch.object(chain, "_SearchChain__async_search_all_sites_stream", side_effect=stream),
        patch("app.chain.search.execution.time.sleep"),
        patch("app.chain.search.execution.asyncio.sleep", new=AsyncMock()),
    ):
        results = chain._process_music(target, rule_groups=[], filter_params={"audio_format": "FLAC"}) \
            if mode == "sync" else asyncio.run(collect())
    assert calls == ["Test Album", "Test Artist Test Album"]
    assert len(results) == 1
    assert results[0].meta_info.audio_format == "FLAC"


def test_music_manual_candidates_keep_resource_identity_separate():
    """来源署名未经验证时只展示资源自身信息，不能回填成所选单曲。"""
    chain = SearchChain()
    target = MusicInfo(media_source="musicbrainz", media_id="target", title="晴天", artists=["周杰伦"])
    torrent = TorrentInfo(title="Jay Chou - 晴天 FLAC", category=MediaType.MUSIC.value)
    assert chain._build_music_contexts([torrent], target, rule_groups=[]) == []
    candidate = chain._build_music_contexts([torrent], target, rule_groups=[], include_candidates=True)[0]
    assert candidate.match_reason == "artist_unverified"
    assert candidate.media_info is None
    assert candidate.meta_info.artists == ["Jay Chou"]
    assert candidate.meta_info.media_id is None
    assert candidate.media_info_is_target is False


def test_music_stream_counts_rejected_site_results():
    """分类和名称不符仍属于站点原始候选，空态应能说明为何没有最终结果。"""
    chain = SearchChain()
    target = MusicInfo(title="One", artists=["U2"])

    async def batches(**_kwargs):
        """返回一条属于其他作品的候选。"""
        yield {"items": [TorrentInfo(title="U2 - One Tree Hill", category=MediaType.MUSIC.value)]}

    async def collect():
        """使用显式关键词把样例限定为一次站点查询。"""
        return [event async for event in chain._async_process_music_stream(target, keyword="One", rule_groups=[])]

    with patch.object(chain, "_SearchChain__async_search_all_sites_stream", side_effect=batches):
        events = asyncio.run(collect())
    assert events[-1]["candidate_items"] == 1
    assert events[-1]["total_items"] == 0
    assert events[-1]["match_counts"] == {"title_mismatch": 1}


def test_music_context_builder_keeps_only_music_category():
    """精确音乐搜索只应保留明确标记为音乐分类的站点资源。"""
    chain = SearchChain()
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )
    torrents = [
        TorrentInfo(
            title="Daft Punk - Get Lucky - Random Access Memories FLAC",
            category=MediaType.MUSIC.value,
            site_name="MusicSite",
        ),
        TorrentInfo(
            title="Daft Punk - Discovery FLAC",
            category=MediaType.MUSIC.value,
            site_name="MusicSite",
        ),
        TorrentInfo(
            title="Unrelated Movie",
            category=MediaType.MOVIE.value,
            site_name="VideoSite",
        ),
    ]

    with patch.object(chain, "filter_torrents", side_effect=lambda **kwargs: kwargs["torrent_list"][:1]):
        contexts = chain._build_music_contexts(
            torrents=torrents,
            mediainfo=music,
            rule_groups=["music"],
        )

    assert len(contexts) == 1
    assert contexts[0].media_info is not music
    assert isinstance(contexts[0].meta_info, MetaMusic)
    assert contexts[0].meta_info.media_id is None
    assert contexts[0].meta_info.title == "Get Lucky - Random Access Memories"
    assert contexts[0].media_info.media_id == "recording-1"
    assert contexts[0].torrent_info.category == MediaType.MUSIC.value


def test_music_search_continues_after_unrelated_first_keyword_results():
    """首组关键词只命中其它专辑时应继续尝试后续关键词，不能提前返回空结果。"""
    chain = SearchChain()
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )
    unrelated = TorrentInfo(
        title="Daft Punk - Discovery FLAC",
        category=MediaType.MUSIC.value,
        site_name="MusicSite",
    )
    matched = TorrentInfo(
        title="Daft Punk - Get Lucky FLAC",
        category=MediaType.MUSIC.value,
        site_name="MusicSite",
    )

    with patch.object(
            chain,
            "_SearchChain__search_all_sites",
            side_effect=[[unrelated], [matched]],
    ) as search_sites, patch("app.chain.search.execution.time.sleep"):
        contexts = chain._process_music(music, rule_groups=[])

    assert search_sites.call_count == 2
    assert len(contexts) == 1
    assert contexts[0].torrent_info.title == matched.title


def test_music_search_uses_simplified_keywords_before_original_traditional_keywords():
    """媒体信息含繁体时应先请求简体关键词，未命中后再请求繁体原文。"""
    chain = SearchChain()
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="album-1",
        music_type="album",
        title="永遠是朋友",
        album="永遠是朋友",
        artists=["周華健"],
    )
    matched = TorrentInfo(
        title="周華健 - 永遠是朋友 FLAC",
        category=MediaType.MUSIC.value,
        site_name="MusicSite",
    )

    with patch.object(
            chain,
            "_SearchChain__search_all_sites",
            side_effect=[[], [matched]],
    ) as search_sites, patch("app.chain.search.execution.time.sleep"):
        contexts = chain._process_music(music, rule_groups=[])

    assert [call.kwargs["keyword"] for call in search_sites.call_args_list] == [
        "永远是朋友",
        "永遠是朋友",
    ]
    assert len(contexts) == 1
    assert contexts[0].torrent_info.title == matched.title


def test_music_search_matches_artist_from_resource_description():
    """精确音乐搜索应使用副标题中的艺术家，兼容主标题只有曲名的站点。"""
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    torrent = TorrentInfo(
        title="晴天 FLAC",
        description="周杰伦 - 叶惠美 2003",
        category=MediaType.MUSIC.value,
    )

    assert SearchChain._matching_music_torrents([torrent], music) == [torrent]


def test_music_stream_reports_site_progress_before_final_results(monkeypatch):
    """精确音乐搜索应逐站点输出进度事件，不能等待全部搜索完成后才返回。"""
    chain = SearchChain()
    chain.runtime_config = replace(
        chain.runtime_config,
        search_multiple_name=False,
    )
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    unrelated = TorrentInfo(
        title="其他歌手 - 晴天 FLAC",
        category=MediaType.MUSIC.value,
        site_name="Site A",
    )
    matched = TorrentInfo(
        title="周杰伦 - 晴天 FLAC",
        category=MediaType.MUSIC.value,
        site_name="Site B",
    )

    async def search_stream(**_kwargs):
        """模拟两个站点依次完成并返回各自候选。"""
        yield {
            "type": "append",
            "stage": "searching",
            "value": 50,
            "text": "已完成 1 / 2 个请求",
            "items": [unrelated],
            "finished": 1,
            "total": 2,
        }
        yield {
            "type": "append",
            "stage": "searching",
            "value": 100,
            "text": "已完成 2 / 2 个请求",
            "items": [matched],
            "finished": 2,
            "total": 2,
        }

    async def collect_events():
        """收集音乐精确搜索流事件。"""
        return [
            event
            async for event in chain.async_process_stream(
                mediainfo=music,
                sites=[1, 2],
                rule_groups=[],
            )
        ]

    monkeypatch.setattr(
        chain,
        "_SearchChain__async_search_all_sites_stream",
        search_stream,
    )
    monkeypatch.setattr(
        chain,
        "_SearchChain__async_search_all_sites",
        Mock(side_effect=AssertionError("音乐流式搜索不应回退到非流式站点搜索")),
    )

    events = asyncio.run(collect_events())

    assert [event["value"] for event in events[:2]] == [50, 100]
    assert [event["finished"] for event in events[:2]] == [1, 2]
    assert events[-2]["type"] == "replace"
    assert events[-1]["type"] == "done"
    assert events[-1]["total_items"] == 1
    assert events[-1]["items"][0]["torrent_info"]["title"] == matched.title


def test_search_by_id_routes_music_identity_to_recognize_and_process():
    """MusicBrainz 精确身份搜索应经统一识别入口识别后进入现有搜索处理链。"""
    chain = SearchChain()
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    expected = [Mock()]
    media_chain = Mock()
    media_chain.recognize_media.return_value = music

    with (
        patch("app.chain.search.media.MediaChain", return_value=media_chain),
        patch.object(chain, "process", return_value=expected) as process,
    ):
        result = chain.search_by_id(
            media_source="musicbrainz",
            media_id="recording-1",
            mtype=MediaType.MUSIC,
            music_type="recording",
            sites=[1],
        )

    assert result == expected
    media_chain.recognize_media.assert_called_once_with(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
        mtype=MediaType.MUSIC,
        music_type="recording",
    )
    process.assert_called_once_with(
        mediainfo=music,
        sites=[1],
        area="title",
        no_exists=None,
    )
