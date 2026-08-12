from unittest.mock import Mock, patch

from app.chain.search import SearchChain
from app.core.meta import MetaMusic
from app.core.context import MusicInfo
from app.schemas.context import TorrentInfo
from app.schemas.types import MediaType


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
    ) as search_sites, patch("app.chain.search.time.sleep"):
        contexts = chain._process_music(music, rule_groups=[])

    assert search_sites.call_count == 2
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
        patch("app.chain.search.MediaChain", return_value=media_chain),
        patch.object(chain, "process", return_value=expected) as process,
    ):
        result = chain.search_by_id(
            media_source="musicbrainz",
            mediaid="recording-1",
            mtype=MediaType.MUSIC,
            music_type="recording",
            sites=[1],
        )

    assert result == expected
    media_chain.recognize_media.assert_called_once_with(
        media_source="musicbrainz",
        mediaid="recording-1",
        tmdbid=None,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        mtype=MediaType.MUSIC,
        music_type="recording",
    )
    process.assert_called_once_with(
        mediainfo=music,
        sites=[1],
        area="title",
        no_exists=None,
    )
