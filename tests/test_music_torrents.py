import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.application.orchestration.torrents import TorrentsChain
from app.domain.meta.metamusic import MetaMusic
from app.domain.context import Context, MusicInfo, TorrentInfo
from app.modules.indexer.spider import SiteSpider
from app.schemas.types import MediaType


def test_async_browse_passes_music_type_to_indexer():
    """站点浏览应把音乐类型传入现有索引刷新接口。"""
    chain = TorrentsChain()
    chain.async_refresh_torrents = AsyncMock(return_value=[])
    sites_helper = Mock()
    sites_helper.async_get_indexer = AsyncMock(
        return_value={"id": 1, "domain": "example.com"}
    )

    with patch("app.application.orchestration.torrents.SitesHelper", return_value=sites_helper):
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
        patch("app.application.orchestration.torrents.SitesHelper", return_value=sites_helper),
        patch("app.application.orchestration.torrents.MediaChain") as media_chain,
    ):
        result = chain.refresh(stype="spider", sites=[1])

    context = result["example.com"][0]
    assert isinstance(context.meta_info, MetaMusic)
    assert isinstance(context.media_info, MusicInfo)
    assert context.meta_info.artists == ["Daft Punk"]
    assert context.media_info.title == "Get Lucky"
    assert context.candidate_recognized is False
    media_chain.assert_not_called()


def test_rss_sets_music_category_from_site_media_type():
    """RSS 报文不带分类，音乐站点的种子应按站点媒体类型补充音乐分类。"""
    chain = TorrentsChain()
    site = {
        "id": 9,
        "name": "音乐站",
        "domain": "https://music.example.com",
        "media_type": "music",
        "rss": "https://music.example.com/rss.php?passkey=key",
        "proxy": False,
        "timeout": 30,
        "ua": None,
    }
    rss_items = [{
        "title": "Daft Punk - Random Access Memories [FLAC]",
        "enclosure": "https://music.example.com/download.php?id=1",
        "link": "https://music.example.com/details.php?id=1",
        "size": 1024,
        "pubdate": None,
    }]
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = site

    with (
        patch("app.application.orchestration.torrents.SitesHelper", return_value=sites_helper),
        patch("app.application.orchestration.torrents.RssHelper") as rss_helper,
    ):
        rss_helper.return_value.parse.return_value = rss_items
        torrents = chain.rss("music.example.com")

    assert len(torrents) == 1
    assert torrents[0].category == MediaType.MUSIC.value


def test_music_browse_paths_detects_dedicated_entry():
    """站点用 type=music 声明的独立音乐入口应被识别，默认入口已覆盖时不重复抓取。"""
    mixed_site = {
        "search": {
            "paths": [
                {"path": "torrents.php", "type": "all"},
                {"path": "special.php", "type": "music"},
            ]
        }
    }
    assert TorrentsChain._music_browse_paths(mixed_site) == ["special.php"]

    # 音乐站点全站都是音乐，无需额外入口
    music_site = {"media_type": "music", "search": {"paths": [
        {"path": "torrents.php", "type": "all"},
        {"path": "torrents.php", "type": "music"},
    ]}}
    assert TorrentsChain._music_browse_paths(music_site) == []

    # 音乐入口与默认入口相同无需重复抓取
    same_entry_site = {"search": {"paths": [
        {"path": "torrents.php", "type": "all"},
        {"path": "torrents.php", "type": "music"},
    ]}}
    assert TorrentsChain._music_browse_paths(same_entry_site) == []


def test_spider_music_entry_browse_resolves_music_category():
    """音乐入口浏览应请求专用页面，并把音乐分类种子解析为音乐类型。"""
    indexer = {
        "id": "hhanclub",
        "name": "憨憨",
        "domain": "https://hhanclub.net/",
        "search": {
            "paths": [
                {"path": "torrents.php", "type": "all"},
                {"path": "special.php", "type": "music"},
            ],
            "params": {"search": "{keyword}"},
        },
        "category": {
            "param": "cat",
            "movie": [{"id": "401", "cat": "Movies"}],
            "music": [{"id": "410", "cat": "Music"}],
        },
        "torrents": {
            "list": {"selector": "table tr.torrent"},
            "fields": {
                "title": {"selector": "a.t"},
                "category": {
                    "selector": "a.c",
                    "attribute": "href",
                    "filters": [{"name": "querystring", "args": "cat"}],
                },
                "download": {"selector": "a.d", "attribute": "href"},
            },
        },
    }
    html = """
    <table>
      <tr class="torrent">
        <td><a class="c" href="torrents.php?cat=410">Music</a></td>
        <td><a class="t">Daft Punk - Random Access Memories [FLAC]</a></td>
        <td><a class="d" href="download.php?id=9">下载</a></td>
      </tr>
    </table>
    """
    request_utils = Mock()
    request_utils.return_value.get_res.return_value = Mock()
    request_utils.get_decoded_html_content.return_value = html

    with (
        patch("app.modules.indexer.spider.RequestUtils", request_utils),
        patch(
            "app.modules.indexer.spider.rust_accel.parse_indexer_torrents",
            return_value=None,
        ),
    ):
        torrents = SiteSpider(indexer=indexer, mtype=MediaType.MUSIC).get_torrents()

    # 请求必须命中音乐专用入口而不是默认首页
    requested_url = request_utils.return_value.get_res.call_args[0][0]
    assert "/special.php" in requested_url
    assert len(torrents) == 1
    assert torrents[0]["category"] == MediaType.MUSIC.value
    assert torrents[0]["title"] == "Daft Punk - Random Access Memories [FLAC]"


def test_refresh_include_music_fetches_music_entry():
    """存在音乐订阅时，spider 刷新应额外抓取音乐专用入口，并把音乐写入独立缓存。"""
    chain = TorrentsChain()
    default_torrent = TorrentInfo(
        site=1, site_name="Test",
        title="Some.Movie.2026.1080p",
        enclosure="https://example.com/download?id=1",
        category=MediaType.MOVIE.value,
        pubdate="2026-08-07 00:00:00",
    )
    music_torrent = TorrentInfo(
        site=1, site_name="Test",
        title="Daft Punk - Get Lucky [FLAC]",
        enclosure="https://example.com/download?id=2",
        category=MediaType.MUSIC.value,
        pubdate="2026-08-08 00:00:00",
    )
    duplicated_torrent = TorrentInfo(
        site=1, site_name="Test",
        title="Daft Punk - Get Lucky [FLAC]",
        enclosure="https://example.com/download?id=2",
        category=MediaType.MUSIC.value,
        pubdate="2026-08-08 00:00:00",
    )

    def _fake_browse(domain, keyword=None, cat=None, page=None, mtype=None):
        if mtype == MediaType.MUSIC:
            # 第二页返回空，验证分页提前结束；首页返回音乐种子
            return [] if page else [music_torrent, duplicated_torrent]
        return [default_torrent] if not page else []

    sites_helper = Mock()
    sites_helper.get_indexers.return_value = [{
        "id": 1,
        "name": "Test",
        "domain": "https://example.com",
        "search": {
            "paths": [
                {"path": "torrents.php", "type": "all"},
                {"path": "special.php", "type": "music"},
            ]
        },
    }]
    # 保存时对入参做快照，避免后续合并返回值修改同一字典引用
    saved = {}
    save_cache = Mock(side_effect=lambda data, filename: saved.__setitem__(filename, copy.deepcopy(data)))

    with (
        patch.object(chain, "load_cache", return_value=None),
        patch.object(chain, "browse", side_effect=_fake_browse),
        patch.object(chain, "save_cache", save_cache),
        patch("app.application.orchestration.torrents.SitesHelper", return_value=sites_helper),
        patch("app.application.orchestration.torrents.MediaChain"),
    ):
        result = chain.refresh(stype="spider", sites=[1], include_music=True)

    # 返回值供订阅匹配，包含影视与音乐完整候选且去重
    contexts = result["example.com"]
    titles = {context.torrent_info.title for context in contexts}
    assert titles == {"Some.Movie.2026.1080p", "Daft Punk - Get Lucky [FLAC]"}
    music_context = next(
        context for context in contexts
        if context.torrent_info.title.startswith("Daft Punk")
    )
    assert isinstance(music_context.meta_info, MetaMusic)
    assert isinstance(music_context.media_info, MusicInfo)
    # 音乐不应经过影视识别链的媒体回填
    assert music_context.candidate_recognized is False

    # 影视与音乐分别写入各自缓存文件，音乐不占用影视缓存空间
    assert set(saved) == {TorrentsChain._spider_file, TorrentsChain._music_spider_file}
    video_titles = {
        context.torrent_info.title for context in saved[TorrentsChain._spider_file]["example.com"]
    }
    music_titles = {
        context.torrent_info.title for context in saved[TorrentsChain._music_spider_file]["example.com"]
    }
    assert video_titles == {"Some.Movie.2026.1080p"}
    assert music_titles == {"Daft Punk - Get Lucky [FLAC]"}


def test_rss_refresh_include_music_fetches_dedicated_entry():
    """RSS 模式有音乐订阅时也应补抓独立音乐入口，并写入音乐缓存。"""
    chain = TorrentsChain()
    video_torrent = TorrentInfo(
        site=1,
        site_name="Test",
        title="Some.Movie.2026.1080p",
        enclosure="https://example.com/download?id=1",
        category=MediaType.MOVIE.value,
        pubdate="2026-08-07 00:00:00",
    )
    music_torrent = TorrentInfo(
        site=1,
        site_name="Test",
        title="Daft Punk - Get Lucky [FLAC]",
        enclosure="https://example.com/download?id=2",
        category=MediaType.MUSIC.value,
        pubdate="2026-08-08 00:00:00",
    )

    def _fake_browse(domain, keyword=None, cat=None, page=None, mtype=None):
        """模拟音乐专用入口的两页抓取结果。"""
        assert mtype == MediaType.MUSIC
        return [music_torrent] if not page else []

    sites_helper = Mock()
    sites_helper.get_indexers.return_value = [{
        "id": 1,
        "name": "Test",
        "domain": "https://example.com",
        "search": {
            "paths": [
                {"path": "torrents.php", "type": "all"},
                {"path": "music.php", "type": "music"},
            ]
        },
    }]
    saved = {}

    with (
        patch.object(chain, "load_cache", return_value=None),
        patch.object(chain, "rss", return_value=[video_torrent]),
        patch.object(chain, "browse", side_effect=_fake_browse) as browse,
        patch.object(
            chain,
            "save_cache",
            side_effect=lambda data, filename: saved.__setitem__(filename, copy.deepcopy(data)),
        ),
        patch("app.application.orchestration.torrents.SitesHelper", return_value=sites_helper),
        patch("app.application.orchestration.torrents.MediaChain"),
    ):
        result = chain.refresh(stype="rss", sites=[1], include_music=True)

    browse.assert_called()
    assert {
        context.torrent_info.title for context in result["example.com"]
    } == {video_torrent.title, music_torrent.title}
    assert {
        context.torrent_info.title
        for context in saved[TorrentsChain._music_rss_file]["example.com"]
    } == {music_torrent.title}


def test_music_cache_not_evicted_by_video_torrents():
    """影视缓存按配额裁剪时，音乐独立缓存中的资源不应被挤出。"""
    chain = TorrentsChain()

    def _music_context(title, enclosure, pubdate):
        torrent = TorrentInfo(
            site=1, site_name="Test", title=title, enclosure=enclosure,
            category=MediaType.MUSIC.value, pubdate=pubdate,
        )
        return Context(
            meta_info=MetaMusic(org_string=title, title=title),
            media_info=MusicInfo(title=title),
            torrent_info=torrent,
        )

    existing_music = [
        _music_context(f"Album {i}", f"https://example.com/download?id=m{i}",
                       f"2026-08-0{i + 1} 00:00:00")
        for i in range(2)
    ]
    video_torrents = [
        TorrentInfo(
            site=1, site_name="Test", title=f"Movie.{i}.1080p",
            enclosure=f"https://example.com/download?id=v{i}",
            category=MediaType.MOVIE.value, pubdate=f"2026-08-1{i} 00:00:00",
        )
        for i in range(3)
    ]

    def _fake_load(filename):
        if filename == TorrentsChain._music_spider_file:
            return {"example.com": existing_music}
        return None

    def _fake_browse(domain, keyword=None, cat=None, page=None, mtype=None):
        return video_torrents if not page else []

    sites_helper = Mock()
    sites_helper.get_indexers.return_value = [{
        "id": 1, "name": "Test", "domain": "https://example.com",
    }]
    saved = {}
    save_cache = Mock(side_effect=lambda data, filename: saved.__setitem__(filename, copy.deepcopy(data)))
    fake_settings = Mock()
    # 公共参数：缓存上限 2，刷新配额 5，音乐与影视各自独立计算
    fake_settings.CONF = SimpleNamespace(torrents=2, refresh=5)
    fake_settings.NO_CACHE_SITE_KEY = "no-cache-site.invalid"

    chain.runtime_config = SimpleNamespace(
        torrent_cache_size=fake_settings.CONF.torrents,
        refresh_batch_size=fake_settings.CONF.refresh,
        no_cache_site_key=fake_settings.NO_CACHE_SITE_KEY,
    )
    with (
        patch.object(chain, "load_cache", side_effect=_fake_load),
        patch.object(chain, "browse", side_effect=_fake_browse),
        patch.object(chain, "save_cache", save_cache),
        patch("app.application.orchestration.torrents.SitesHelper", return_value=sites_helper),
        patch("app.application.orchestration.torrents.MediaChain"),
    ):
        chain.refresh(stype="spider", sites=[1])

    # 影视缓存独立按配额裁剪，仅保留最新的两条
    assert len(saved[TorrentsChain._spider_file]["example.com"]) == 2
    # 音乐独立缓存不受影视种子大量涌入影响，既有的两条音乐完整保留
    music_titles = {
        context.torrent_info.title
        for context in saved[TorrentsChain._music_spider_file]["example.com"]
    }
    assert music_titles == {"Album 0", "Album 1"}
