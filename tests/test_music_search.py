from unittest.mock import Mock, patch

from app.chain.search import SearchChain
from app.core.music import MusicInfo, MusicMeta
from app.schemas.context import TorrentInfo
from app.schemas.types import MediaType


def test_music_context_builder_keeps_only_music_category():
    """精确音乐搜索只应保留明确标记为音乐分类的站点资源。"""
    chain = SearchChain()
    music = MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )
    torrents = [
        TorrentInfo(
            title="Daft Punk - Random Access Memories FLAC",
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
    assert isinstance(contexts[0].meta_info, MusicMeta)
    assert contexts[0].meta_info.media_id == "recording-1"
    assert contexts[0].torrent_info.category == MediaType.MUSIC.value


def test_search_by_id_routes_music_identity_to_music_chain():
    """MusicBrainz 精确身份搜索应使用 MusicChain 识别并进入现有搜索处理链。"""
    chain = SearchChain()
    music = MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    expected = [Mock()]

    with (
        patch("app.chain.search.MusicChain") as music_chain,
        patch.object(chain, "process", return_value=expected) as process,
    ):
        music_chain.return_value.recognize.return_value = music
        result = chain.search_by_id(
            source="musicbrainz",
            mediaid="recording-1",
            mtype=MediaType.MUSIC,
            sites=[1],
        )

    assert result == expected
    music_chain.return_value.recognize.assert_called_once_with(
        source="musicbrainz",
        media_id="recording-1",
    )
    process.assert_called_once_with(
        mediainfo=music,
        sites=[1],
        area="title",
        no_exists=None,
    )
