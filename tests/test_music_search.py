import asyncio
from dataclasses import replace
from unittest.mock import Mock, patch

from app.application.orchestration.search import SearchChain
from app.domain.meta.metamusic import MetaMusic
from app.domain.context import MusicInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


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

    with patch.object(chain, "filter_torrents", return_value=torrents[:1]):
        contexts = chain._build_music_contexts(
            torrents=torrents,
            mediainfo=music,
            rule_groups=["music"],
        )

    assert len(contexts) == 1
    assert contexts[0].media_info is music
    assert isinstance(contexts[0].meta_info, MetaMusic)
    assert contexts[0].meta_info.media_id == "recording-1"
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
    ) as search_sites, patch("app.application.orchestration.search.time.sleep"):
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
    ) as search_sites, patch("app.application.orchestration.search.time.sleep"):
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
        patch("app.application.orchestration.search.MediaChain", return_value=media_chain),
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
