"""Issue #6850 媒体服务器缓存隔离与 Plex 回退回归测试。"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import InvalidURL

from app.chain.download import DownloadChain
from app.db.models.mediaserver import MediaServerItem
from app.db.oper.mediaserver import MediaServerOper
from app.modules.plex.plex import Plex
from app.schemas.types import MediaSource


def test_media_server_item_lookup_scopes_identity_and_title_by_server(db):
    """同一媒体身份在多服务器存在时，缓存 ID 查询必须限定目标服务器。"""
    db.add(
        MediaServerItem(
            server="trimemedia",
            item_id="e52ec4f6140645568c1b8d969bcf833f",
            item_type="电视剧",
            title="余红旧事",
            year="2026",
            media_source=MediaSource.TMDB,
            media_id="301494",
            seasoninfo={1: [1]},
        ),
        MediaServerItem(
            server="plex",
            item_id="plex-301494",
            item_type="电视剧",
            title="余红旧事",
            year="2026",
            media_source=MediaSource.TMDB,
            media_id="301494",
            seasoninfo={1: [1]},
        ),
    )

    oper = MediaServerOper(db.session)
    query = {
        "title": "余红旧事",
        "year": "2026",
        "mtype": "电视剧",
        "media_source": MediaSource.TMDB,
        "media_id": "301494",
        "season": 1,
    }

    assert oper.get_item_id(server="trimemedia", **query) == "e52ec4f6140645568c1b8d969bcf833f"
    assert oper.get_item_id(server="plex", **query) == "plex-301494"
    assert oper.get_item_id(
        server="plex",
        title="余红旧事",
        year="2026",
        mtype="电视剧",
        media_source=MediaSource.Douban,
        media_id="douban-301494",
        season=1,
    ) == "plex-301494"


def test_download_existence_passes_cached_id_only_to_its_server():
    """缺集检查逐个服务器查询缓存，避免把一个服务器的 ID 广播给其他服务器。"""
    chain = DownloadChain()
    chain.media_server_repository = Mock()
    chain.media_server_repository.get_item_id.side_effect = [
        "e52ec4f6140645568c1b8d969bcf833f",
        "plex-301494",
    ]
    chain.media_exists = Mock(side_effect=[None, object()])

    with patch(
        "app.chain.download.existence.get_mediaserver_configs",
        return_value=[
            SimpleNamespace(name="trimemedia"),
            SimpleNamespace(name="plex"),
        ],
    ):
        result = chain._media_exists_with_server_cache(
            mediainfo=Mock(),
            query={"title": "余红旧事", "mtype": "电视剧"},
        )

    assert result is not None
    assert chain.media_server_repository.get_item_id.call_args_list[0].kwargs["server"] == "trimemedia"
    assert chain.media_server_repository.get_item_id.call_args_list[1].kwargs["server"] == "plex"
    assert chain.media_exists.call_args_list[0].kwargs["itemid"] == "e52ec4f6140645568c1b8d969bcf833f"
    assert chain.media_exists.call_args_list[0].kwargs["server"] == "trimemedia"
    assert chain.media_exists.call_args_list[1].kwargs["itemid"] == "plex-301494"
    assert chain.media_exists.call_args_list[1].kwargs["server"] == "plex"


def test_plex_invalid_cached_item_id_falls_back_to_title_search():
    """Plex 遇到其他服务器的十六进制缓存 ID 时不得拼接非法 URL。"""
    plex = Plex.__new__(Plex)
    plex._plex = Mock()
    plex._plex.fetchItem.side_effect = InvalidURL(
        "Failed to parse: http://plex:32400e52ec4f6140645568c1b8d969bcf833f"
    )
    show = Mock(key="/library/metadata/200", guids=[{"id": "tmdb://301494"}])
    show.episodes.return_value = [Mock(seasonNumber=1, index=1)]
    plex._plex.library.search.return_value = [show]

    item_id, episodes = plex.get_tv_episodes(
        item_id="e52ec4f6140645568c1b8d969bcf833f",
        title="余红旧事",
        media_source=MediaSource.TMDB,
        media_id="301494",
    )

    assert item_id == "/library/metadata/200"
    assert episodes == {1: [1]}
    plex._plex.fetchItem.assert_not_called()
    plex._plex.library.search.assert_called_once()
