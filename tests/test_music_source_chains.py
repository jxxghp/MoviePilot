import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from app.chain.acoustid import AcoustIdChain
from app.chain.douban import DoubanChain
from app.chain.listenbrainz import ListenBrainzChain
from app.chain.lrclib import LrclibChain
from app.chain.musicbrainz import MusicBrainzChain
from app.chain.theaudiodb import TheAudioDbChain
from app.domain.context import MusicAlbumInfo, MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaSource


def test_musicbrainz_chain_fixes_source_on_search(monkeypatch) -> None:
    """MusicBrainz 来源链应固定请求来源并拒绝跨来源候选。"""
    chain = MusicBrainzChain()
    unicast = Mock(return_value=[
        MusicInfo(media_source="musicbrainz", media_id="recording-1", title="One"),
        MusicInfo(media_source="theaudiodb", media_id="track-2", title="Two"),
    ])
    monkeypatch.setattr(chain, "unicast", unicast)

    result = chain.search_music(MetaMusic(title="One"), limit=5)

    assert [item.media_id for item in result] == ["recording-1"]
    assert unicast.call_args.kwargs["media_source"] == MediaSource.MusicBrainz


def test_theaudiodb_chain_exposes_only_theaudiodb_results(monkeypatch) -> None:
    """TheAudioDB 来源链应仅返回自身来源的标准音乐信息。"""
    chain = TheAudioDbChain()
    unicast = Mock(return_value=MusicInfo(
        media_source="theaudiodb",
        media_id="track-1",
        title="Track",
    ))
    monkeypatch.setattr(chain, "unicast", unicast)

    result = chain.recognize_music(media_id="track-1")

    assert result and result.media_source == MediaSource.TheAudioDB
    assert unicast.call_args.kwargs["media_source"] == MediaSource.TheAudioDB


def test_musicbrainz_album_rejects_mismatched_identity(monkeypatch) -> None:
    """显式专辑 ID 查询不应接受另一身份的详情结果。"""
    chain = MusicBrainzChain()
    monkeypatch.setattr(chain, "unicast", Mock(return_value=MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="other-album",
        title="Other",
    )))

    assert chain.get_music_album("requested-album") is None


def test_douban_music_discover_fixes_source(monkeypatch) -> None:
    """豆瓣音乐发现端口应固定 doubanmusic 而非影视 douban 来源。"""
    chain = DoubanChain()
    unicast = Mock(return_value=[
        MusicInfo(media_source="doubanmusic", media_id="album-1", title="Album")
    ])
    monkeypatch.setattr(chain, "unicast", unicast)

    result = chain.music_discover(page=2, count=10)

    assert result[0].media_source == MediaSource.DoubanMusic
    assert unicast.call_args.kwargs["media_source"] == MediaSource.DoubanMusic


def test_listenbrainz_chain_translates_page_to_offset(monkeypatch) -> None:
    """ListenBrainz 来源链应把页码转换为模块使用的偏移量。"""
    chain = ListenBrainzChain()
    unicast = Mock(return_value=[
        MusicInfo(media_source="musicbrainz", media_id="recording-1", title="Track")
    ])
    monkeypatch.setattr(chain, "unicast", unicast)

    result = chain.music_chart("this_week", page=3, count=10)

    assert result[0].media_id == "recording-1"
    assert unicast.call_args.kwargs["offset"] == 20


def test_acoustid_chain_normalizes_fingerprint_result(monkeypatch) -> None:
    """AcoustID 来源链应把指纹结果规范化为 Recording ID 文本。"""
    chain = AcoustIdChain()
    unicast = Mock(return_value="  recording-1  ")
    monkeypatch.setattr(chain, "unicast", unicast)

    result = chain.identify_music_by_fingerprint(Path("track.flac"))

    assert result == "recording-1"


def test_lrclib_chain_converts_dictionary_result(monkeypatch) -> None:
    """LRCLIB 来源链应把字典结果转换为标准歌词对象。"""
    chain = LrclibChain()
    monkeypatch.setattr(chain, "unicast", Mock(return_value={
        "provider": "lrclib",
        "provider_id": "1",
        "synced_lyrics": "[00:01] Track",
    }))

    result = chain.get_music_lyrics(MetaMusic(title="Track", artists=["Artist"]))

    assert isinstance(result, MusicLyrics)
    assert result.extension == ".lrc"


def test_async_source_ports_use_async_dispatch(monkeypatch) -> None:
    """来源链异步端口应通过 async_unicast 分发模块能力。"""
    chain = MusicBrainzChain()
    async_unicast = AsyncMock(return_value=MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Track",
    ))
    monkeypatch.setattr(chain, "async_unicast", async_unicast)

    result = asyncio.run(chain.async_recognize_music(media_id="recording-1"))

    assert result and result.media_id == "recording-1"
    async_unicast.assert_awaited_once()
