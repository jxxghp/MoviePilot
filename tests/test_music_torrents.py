import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.chain.torrents import TorrentsChain
from app.core.music import MusicInfo, MusicMeta
from app.schemas.types import MediaType


def test_async_browse_passes_music_type_to_indexer():
    """站点浏览应把音乐类型传入现有索引刷新接口。"""
    chain = TorrentsChain()
    chain.async_refresh_torrents = AsyncMock(return_value=[])
    sites_helper = Mock()
    sites_helper.async_get_indexer = AsyncMock(
        return_value={"id": 1, "domain": "example.com"}
    )

    with patch("app.chain.torrents.SitesHelper", return_value=sites_helper):
        asyncio.run(
            chain.async_browse(
                domain="example.com",
                keyword="Daft Punk",
                mtype=MediaType.MUSIC,
            )
        )

    chain.async_refresh_torrents.assert_awaited_once_with(
        site={"id": 1, "domain": "example.com"},
        keyword="Daft Punk",
        cat=None,
        page=0,
        mtype=MediaType.MUSIC,
    )


def test_music_cache_context_uses_music_models():
    """站点缓存识别到音乐分类时不应进入影视识别链。"""
    chain = TorrentsChain()
    torrent = Mock(
        title="Daft Punk - Get Lucky",
        description=None,
        enclosure="https://example.com/download?id=1",
        category=MediaType.MUSIC.value,
        pubdate="2026-08-07 00:00:00",
    )
    sites_helper = Mock()
    sites_helper.get_indexers.return_value = [{
        "id": 1,
        "name": "Test",
        "domain": "https://example.com",
    }]

    with (
        patch.object(chain, "get_torrents", return_value={}),
        patch.object(chain, "browse", return_value=[torrent]),
        patch.object(chain, "save_cache"),
        patch("app.chain.torrents.SitesHelper", return_value=sites_helper),
        patch("app.chain.torrents.MediaChain") as media_chain,
    ):
        result = chain.refresh(stype="spider", sites=[1])

    context = result["example.com"][0]
    assert isinstance(context.meta_info, MusicMeta)
    assert isinstance(context.media_info, MusicInfo)
    assert context.meta_info.artists == ["Daft Punk"]
    assert context.media_info.title == "Get Lucky"
    assert context.candidate_recognized is False
    media_chain.assert_not_called()
