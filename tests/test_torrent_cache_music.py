"""种子缓存音乐实体识别测试。"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.api.endpoints.torrent import reidentify_cache, torrents_cache
from app.chain.music import MusicChain
from app.core.context import MUSIC_ENTITY_ALBUM, Context, MusicInfo, TorrentInfo
from app.schemas.types import MediaType
from app.utils.crypto import HashUtils


def _album_context() -> Context:
    """构造带完整专辑身份的缓存上下文。"""
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        artists=["周杰伦"],
    )
    return Context(
        meta_info=MusicChain.to_meta(album),
        media_info=album,
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            description="11 tracks",
            category=MediaType.MUSIC.value,
            site_name="Music Site",
        ),
    )


def _mock_torrents_chain(context: Context) -> Mock:
    """构造可记录缓存读写的种子链替身。"""
    chain = Mock()
    chain.async_get_torrents = AsyncMock(return_value={"music.example": [context]})
    chain.split_cache_contexts.return_value = ({}, {"music.example": [context]})
    chain.cache_files.return_value = ("video-cache", "music-cache")
    chain.async_save_cache = AsyncMock()
    return chain


def test_torrent_cache_exposes_music_identity_namespace():
    """缓存列表应返回重识别所需的数据源、原生 ID 和实体类型。"""
    context = _album_context()
    torrents_chain = _mock_torrents_chain(context)

    with patch(
        "app.api.endpoints.torrent.TorrentsChain",
        return_value=torrents_chain,
    ):
        response = asyncio.run(torrents_cache(_=Mock()))

    item = response.data["data"][0]
    assert item["media_source"] == "musicbrainz"
    assert item["media_id"] == "release-group-1"
    assert item["music_type"] == MUSIC_ENTITY_ALBUM


def test_torrent_cache_exact_reidentify_forwards_album_namespace():
    """缓存按专辑 ID 重识别时必须在进入 MediaChain 前绑定 album。"""
    context = _album_context()
    torrents_chain = _mock_torrents_chain(context)
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=context.media_info)
    torrent_hash = HashUtils.md5(
        f"{context.torrent_info.title}{context.torrent_info.description}"
    )

    with patch(
        "app.api.endpoints.torrent.TorrentsChain",
        return_value=torrents_chain,
    ), patch("app.api.endpoints.torrent.MediaChain", return_value=media_chain):
        response = asyncio.run(
            reidentify_cache(
                domain="music.example",
                torrent_hash=torrent_hash,
                media_source="musicbrainz",
                media_id="release-group-1",
                music_type="album",
                _=Mock(),
            )
        )

    assert response.success is True
    recognize_kwargs = media_chain.async_recognize_media.await_args.kwargs
    assert recognize_kwargs["mtype"] == MediaType.MUSIC
    assert recognize_kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    assert recognize_kwargs["mediaid"] == "release-group-1"


def test_torrent_cache_auto_reidentify_keeps_music_meta_and_entity():
    """不指定 ID 的专辑重识别应沿用 MetaMusic 和原有 album 命名空间。"""
    context = _album_context()
    torrents_chain = _mock_torrents_chain(context)
    media_chain = Mock()
    media_chain.async_recognize_by_meta = AsyncMock(return_value=context.media_info)
    torrent_hash = HashUtils.md5(
        f"{context.torrent_info.title}{context.torrent_info.description}"
    )

    with patch(
        "app.api.endpoints.torrent.TorrentsChain",
        return_value=torrents_chain,
    ), patch("app.api.endpoints.torrent.MediaChain", return_value=media_chain):
        response = asyncio.run(
            reidentify_cache(
                domain="music.example",
                torrent_hash=torrent_hash,
                _=Mock(),
            )
        )

    assert response.success is True
    meta = media_chain.async_recognize_by_meta.await_args.args[0]
    recognize_kwargs = media_chain.async_recognize_by_meta.await_args.kwargs
    assert meta.type == MediaType.MUSIC
    assert recognize_kwargs["mtype"] == MediaType.MUSIC
    assert recognize_kwargs["music_type"] == MUSIC_ENTITY_ALBUM
