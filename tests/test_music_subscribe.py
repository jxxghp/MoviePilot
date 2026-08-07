from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.chain.subscribe import SubscribeChain, build_subscribe_meta
from app.core.context import Context, TorrentInfo
from app.core.music import MusicInfo, MusicMeta
from app.schemas.types import MediaType


def _music_info() -> MusicInfo:
    """构造音乐订阅测试使用的标准目标。"""
    return MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )


def _subscribe() -> SimpleNamespace:
    """构造不依赖数据库的音乐订阅对象。"""
    return SimpleNamespace(
        id=7,
        name="晴天",
        year="2003",
        type=MediaType.MUSIC.value,
        keyword=None,
        media_source="musicbrainz",
        media_id="recording-1",
        season=None,
        episode_group=None,
        tmdbid=None,
        imdbid=None,
        tvdbid=None,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        sites=[],
        filter_groups=[],
        quality=None,
        resolution=None,
        effect=None,
        include=None,
        exclude=None,
        username="admin",
        save_path=None,
        downloader=None,
        custom_words=None,
        media_category=None,
        best_version=0,
        state="R",
        note=None,
    )


def test_build_subscribe_meta_returns_music_meta():
    """音乐订阅应构造 MusicMeta，而不是交给影视标题解析器。"""
    meta = build_subscribe_meta(_subscribe())

    assert isinstance(meta, MusicMeta)
    assert meta.type == MediaType.MUSIC
    assert meta.media_id == "recording-1"


def test_music_subscribe_reuses_search_download_and_finish_flow():
    """音乐订阅应复用站点搜索、批量下载和订阅完成主流程。"""
    subscribe = _subscribe()
    target = _music_info()
    context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            category=MediaType.MUSIC.value,
        )
    )
    search_chain = Mock()
    search_chain.search_by_title.return_value = [context]
    download_chain = Mock()
    download_chain.batch_download.return_value = ([context], None)
    chain = SubscribeChain()
    chain.finish_subscribe_or_not = Mock()

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=target), \
            patch("app.chain.subscribe.SearchChain", return_value=search_chain), \
            patch("app.chain.subscribe.DownloadChain", return_value=download_chain), \
            patch("app.chain.subscribe.SubscribeOper") as subscribe_oper:
        subscribe_oper.return_value.get.return_value = subscribe
        chain._search_music_subscribe(subscribe)

    search_chain.search_by_title.assert_called_once_with(
        title="周杰伦 叶惠美",
        sites=[],
        mtype=MediaType.MUSIC,
        rule_groups=[],
    )
    assert context.media_info is target
    assert isinstance(context.meta_info, MusicMeta)
    assert context.meta_info.org_string == "周杰伦 - 叶惠美 FLAC"
    download_chain.batch_download.assert_called_once()
    chain.finish_subscribe_or_not.assert_called_once()


def test_music_subscribe_ignores_non_music_category():
    """音乐订阅不得自动下载未被站点分类为音乐的资源。"""
    subscribe = _subscribe()
    context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            category=MediaType.MOVIE.value,
        )
    )
    search_chain = Mock()
    search_chain.search_by_title.return_value = [context]
    chain = SubscribeChain()

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=_music_info()), \
            patch("app.chain.subscribe.SearchChain", return_value=search_chain), \
            patch("app.chain.subscribe.DownloadChain") as download_chain:
        chain._search_music_subscribe(subscribe)

    download_chain.assert_not_called()
