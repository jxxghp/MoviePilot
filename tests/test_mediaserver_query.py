"""
验证媒体查询投影确实作用于远端请求，并保持旧调用与递归查询的兼容行为。
"""

from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from xml.etree.ElementTree import fromstring

import pytest
from plexapi.library import MovieSection, MusicSection
from pydantic import ValidationError

from app.application.mediaserver import MEDIA_SERVER_EXISTENCE_QUERY, MediaServerSyncItem
from app.chain.mediaserver import _SYNC_QUERY
from app.modules._base.mediaserver import emby_query_params
from app.modules.emby import EmbyModule
from app.modules.emby.emby import Emby
from app.modules.jellyfin import JellyfinModule
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.navidrome import NavidromeModule
from app.modules.navidrome.navidrome import Navidrome
from app.modules.plex import PlexModule
from app.modules.plex.plex import Plex
from app.modules.zspace import ZSpaceModule
from app.runtime.extensions.module.contracts import compatible_module_kwargs
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions
from app.schemas.types import MediaSource, MediaType


def test_default_query_keeps_all_fields_when_operation_adds_requirements():
    """
    默认查询补充操作必需字段后仍保留全部内容，不能退化为仅取这些字段。
    """
    query = MediaServerQueryOptions()

    enriched = query.with_fields(MediaServerField.IDENTITY)

    assert enriched is query
    assert enriched.fields is None
    assert all(enriched.includes(field) for field in MediaServerField)


def test_query_requirements_do_not_mutate_shared_projection():
    """
    操作补充身份字段不能污染共享选项或调用方传入的集合。
    """
    fields = {MediaServerField.PATH}
    query = MediaServerQueryOptions(fields=fields)

    enriched = query.with_fields(MediaServerField.IDENTITY, MediaServerField.PATH)
    fields.clear()

    assert query.fields == frozenset({MediaServerField.PATH})
    assert not query.includes(MediaServerField.IDENTITY)
    assert enriched.fields == frozenset({MediaServerField.PATH, MediaServerField.IDENTITY})
    assert not enriched.includes(MediaServerField.IMAGES)
    with pytest.raises(ValidationError, match="frozen_instance"):
        query.fields = frozenset()


@pytest.mark.parametrize("payload", [{"fields": ["unknown"]}, {"images": False}])
def test_query_rejects_unknown_content_groups_and_options(payload):
    """
    查询边界拒绝未登记的内容组与配置项，避免拼写错误静默改变请求。

    :param payload: 带未知内容组或配置项的外部输入
    """
    with pytest.raises(ValidationError):
        MediaServerQueryOptions.model_validate(payload)


@pytest.fixture(params=[Emby, Jellyfin], ids=["emby", "jellyfin"])
def emby_client(request):
    """
    构造不登录服务器的客户端与受控传输。

    :param request: 当前客户端类型的 pytest 参数
    :yields: 客户端及其 HTTP 传输替身
    """
    client_type = request.param
    client = client_type.__new__(client_type)
    client._host = "http://mediaserver.invalid/"
    client._apikey = "synthetic-api-key"
    client._playhost = None
    client._sync_libraries = []
    client.user = "user"
    client.serverid = "server"
    client.get_user_library_folders = Mock(return_value=[])
    with patch(f"{client_type.__module__}.RequestUtils") as transport:
        yield client, transport.return_value


def _response(payload):
    """
    返回只包含给定 JSON 的成功 HTTP 响应。

    :param payload: 服务器响应的合成数据
    :return: 可由客户端读取的响应替身
    """
    response = Mock(status_code=200)
    response.json.return_value = payload
    return response


def _movie():
    """
    提供同时含业务身份、路径和播放状态的合成媒体条目。

    :return: 原始服务器条目字典
    """
    return {
        "Id": "movie",
        "Type": "Movie",
        "Name": "示例电影",
        "ParentId": "library",
        "Path": "/library/movie.mkv",
        "ProductionYear": 2026,
        "ProviderIds": {"Tmdb": "123"},
        "UserData": {"Played": True, "PlayCount": 2},
    }


@pytest.mark.parametrize(
    "query,expected_fields",
    [
        (MediaServerQueryOptions(fields={MediaServerField.YEAR}), {"ProductionYear"}),
        (MEDIA_SERVER_EXISTENCE_QUERY, {"ProviderIds", "ProductionYear"}),
        (_SYNC_QUERY, {"ParentId", "OriginalTitle", "ProductionYear", "ProviderIds", "Path", "ChildCount"}),
    ],
    ids=["year-only", "existence", "sync"],
)
def test_emby_projection_explicitly_requests_year(query, expected_fields):
    """
    年份投影必须显式追加 ProductionYear，不能依赖服务端默认返回。

    :param query: 年份单字段、实际查重或同步使用的投影
    :param expected_fields: 当前投影必须发送的字段集合
    """
    params = emby_query_params(query)

    assert set(params["Fields"].split(",")) == expected_fields
    assert params["EnableImages"] == "false"
    assert params["EnableUserData"] == "false"


@pytest.fixture
def year_projected_client(emby_client):
    """
    模拟只有请求 ProductionYear 才返回年份的 Emby/Jellyfin 服务器。

    :param emby_client: 无网络客户端及传输替身
    :return: 客户端及按请求字段裁剪年份的传输替身
    """
    client, transport = emby_client

    def respond(_url, params):
        """
        按字段请求生成媒体列表响应，涵盖电影和电视剧同步。

        :param _url: 客户端请求地址
        :param params: 实际发往服务端的查询参数
        :return: 根据 Fields 决定是否带有年份的成功响应
        """
        movie = _movie()
        if "ProductionYear" not in params.get("Fields", "").split(","):
            movie.pop("ProductionYear")
        series = {**movie, "Id": "series", "Type": "Series"}
        items = [movie] if params.get("Ids") or params.get("SearchTerm") or params.get("searchTerm") else [movie, series]
        return _response({"Items": items})

    transport.get_res.side_effect = respond
    return client, transport


@pytest.mark.parametrize("year,media_id,expected", [(2026, "123", True), (2025, "123", False), (2026, "999", False)])
def test_existence_projection_preserves_movie_year_matching(year_projected_client, year, media_id, expected):
    """
    实际查重投影能命中同年份同身份电影，并继续排除年份或身份冲突。

    :param year_projected_client: 按请求字段返回年份的客户端及传输替身
    :param year: 查重要求的年份
    :param media_id: 查重要求的 TMDB 标识
    :param expected: 是否应匹配库内电影
    """
    client, _ = year_projected_client

    items = client.get_movies(
        title="示例电影", year=str(year), media_source=MediaSource.TMDB, media_id=media_id,
        query=MEDIA_SERVER_EXISTENCE_QUERY,
    )

    assert bool(items) is expected
    if expected:
        assert items[0].item_id == "movie"
        assert str(items[0].year) == "2026"


def test_existence_projection_preserves_detail_year(year_projected_client):
    """
    缓存条目按 ID 实时复核时也必须取回年份。

    :param year_projected_client: 按请求字段返回年份的客户端及传输替身
    """
    client, transport = year_projected_client

    item = client.get_iteminfo("movie", query=MEDIA_SERVER_EXISTENCE_QUERY)

    assert item.media_id == "123"
    assert str(item.year) == "2026"
    assert transport.get_res.call_args.args[1]["Ids"] == "movie"


def test_sync_projection_preserves_movie_and_series_year(year_projected_client):
    """
    实际同步投影把电影和电视剧年份保留到持久化写端快照。

    :param year_projected_client: 按请求字段返回年份的客户端及传输替身
    """
    client, _ = year_projected_client

    items = list(client.get_items("library", query=_SYNC_QUERY))
    snapshots = [
        MediaServerSyncItem.from_item(item, item_type=item.item_type, seasoninfo={}, sync_time="2026-10-20 12:00:00")
        for item in items
    ]

    assert {item.item_type for item in snapshots} == {"Movie", "Series"}
    assert all(str(item.year) == "2026" for item in snapshots)


@pytest.mark.parametrize("query", [None, MediaServerQueryOptions()])
def test_default_query_preserves_list_request_and_playback_state(emby_client, query):
    """
    未选择投影时保留原字段追加和用户播放状态，不新增图片控制参数。

    :param emby_client: 无网络客户端及传输替身
    :param query: 原调用或显式默认选项
    """
    client, transport = emby_client
    transport.get_res.return_value = _response({"Items": [_movie()]})

    items = list(client.get_items("library", query=query))

    params = transport.get_res.call_args.args[1]
    assert "UserDataPlayCount" in params["Fields"]
    assert "EnableImages" not in params
    assert "EnableUserData" not in params
    assert items[0].user_state.play_count == 2
    assert items[0].path == "/library/movie.mkv"


def test_identity_query_reaches_every_nested_folder_request(emby_client):
    """
    子目录请求不能丢失投影而重新拉取图片和用户数据。

    :param emby_client: 无网络客户端及传输替身
    """
    client, transport = emby_client
    transport.get_res.side_effect = [
        _response({"Items": [{"Id": "folder", "Type": "Folder"}]}),
        _response({"Items": [_movie()]}),
    ]
    query = MediaServerQueryOptions(fields={MediaServerField.IDENTITY, MediaServerField.PATH})

    items = list(client.get_items("library", query=query))

    assert items[0].media_id == "123"
    assert transport.get_res.call_count == 2
    for request_call in transport.get_res.call_args_list:
        params = request_call.args[1]
        assert set(params["Fields"].split(",")) == {"ProviderIds", "Path"}
        assert params["EnableImages"] == "false"
        assert params["EnableUserData"] == "false"
    assert transport.get_res.call_args_list[1].args[1]["ParentId"] == "folder"


def test_projected_detail_uses_filtered_list_instead_of_full_detail(emby_client):
    """
    精简详情必须走服务端可裁剪的按 ID 列表查询，不能先下载完整详情再丢字段。

    :param emby_client: 无网络客户端及传输替身
    """
    client, transport = emby_client
    transport.get_res.return_value = _response({"Items": [_movie()]})

    item = client.get_iteminfo("movie", query=MediaServerQueryOptions(fields={MediaServerField.IDENTITY}))

    url, params = transport.get_res.call_args.args
    assert url.endswith("/Items")
    assert params["Ids"] == "movie"
    assert params["Limit"] == 1
    assert params["Fields"] == "ProviderIds"
    assert params["EnableImages"] == "false"
    assert item.media_id == "123"


def test_library_projection_avoids_unused_count_requests(emby_client):
    """
    同步库目录不需要封面和数量时，不触发逐库统计请求。

    :param emby_client: 无网络客户端及传输替身
    """
    client, _ = emby_client
    lookup = "_Emby__get_emby_librarys" if isinstance(client, Emby) else "_Jellyfin__get_jellyfin_librarys"
    setattr(client, lookup, Mock(return_value=[{"Id": "library", "Name": "电影库", "CollectionType": "movies"}]))
    client.get_items_count = Mock(side_effect=AssertionError("不应查询媒体数量"))

    libraries = client.get_librarys(query=MediaServerQueryOptions(fields=set()))

    assert libraries[0].id == "library"
    assert libraries[0].item_count is None
    assert libraries[0].image is None
    client.get_items_count.assert_not_called()


def test_resume_without_images_does_not_download_or_probe_them(emby_client):
    """
    关闭图片同时作用于远端元数据和本地附加图片 GET。

    :param emby_client: 无网络客户端及传输替身
    """
    client, transport = emby_client
    transport.get_res.return_value = _response({"Items": [_movie()]})

    items = client.get_resume(query=MediaServerQueryOptions(fields={MediaServerField.USER_STATE}))

    assert len(items) == 1
    assert items[0].image is None
    transport.get_res.assert_called_once()
    assert transport.get_res.call_args.args[1]["EnableImages"] == "false"
    # 原有媒体库范围过滤仍然需要路径，不能按展示投影把它裁掉。
    assert transport.get_res.call_args.args[1]["Fields"] == "Path"


@pytest.mark.parametrize("stale", [False, True], ids=["cached", "stale-cache"])
def test_episode_query_keeps_identity_for_matching_and_minimizes_episodes(emby_client, stale):
    """
    同步分集只取季集号，父剧身份校验仍需来源，且不能重复传递 query 关键字。
    """
    client, transport = emby_client
    series = {**_movie(), "Id": "series", "Type": "Series"}
    transport.get_res.side_effect = (
        [
            _response({"Items": []}),
            _response({"Items": [series]}),
        ]
        if stale
        else []
    ) + [
        _response({"Items": [series]}),
        _response({"Items": [{"Id": "episode", "ParentIndexNumber": 1, "IndexNumber": 2}]}),
    ]

    item_id, episodes = client.get_tv_episodes(
        item_id="series",
        title="示例电影",
        media_source=MediaSource.TMDB,
        media_id="123",
        query=MediaServerQueryOptions(fields=set()),
    )

    assert (item_id, episodes) == ("series", {1: [2]})
    assert transport.get_res.call_count == (4 if stale else 2)
    detail, episode = transport.get_res.call_args_list[-2:]
    assert detail.args[1]["Fields"] == "ProviderIds"
    assert episode.args[1]["Fields"] == ""
    assert episode.args[1]["EnableImages"] == "false"
    assert episode.args[1]["EnableUserData"] == "false"


def test_query_optional_argument_preserves_old_provider_signature():
    """
    旧插件可继续提供原有签名，只有显式登记的新 query 参数被兼容省略。
    """

    def provider(server, library_id, start_index=0, limit=-1):
        """
        模拟未声明投影的旧媒体条目 provider。

        :param server: 服务实例
        :param library_id: 媒体库标识
        :param start_index: 起始偏移
        :param limit: 数量上限
        :return: 合成条目
        """
        return server, library_id, start_index, limit

    kwargs = {"server": "server", "library_id": "library", "query": MediaServerQueryOptions(fields=set())}
    assert provider(**compatible_module_kwargs("mediaserver_items", provider, kwargs)) == ("server", "library", 0, -1)
    assert "unknown" in compatible_module_kwargs("mediaserver_items", provider, {**kwargs, "unknown": True})


@pytest.mark.parametrize("fields", [set(), {MediaServerField.IDENTITY, MediaServerField.PATH}])
def test_plex_projection_reaches_real_sdk_without_metadata_reload(fields):
    """
    真实 SDK 解析精简 XML 时只发送一次列表请求，且不恢复被裁掉的内容。
    """
    server = Mock()
    server.query.return_value = fromstring(
        '<MediaContainer size="1" totalSize="1" librarySectionID="1">'
        '<Video type="movie" key="/library/metadata/1" ratingKey="1" title="Demo">'
        '<Media><Part file="/cloud/Demo.mkv"/></Media><Guid id="tmdb://123"/>'
        "</Video></MediaContainer>"
    )
    section = MovieSection(server, fromstring('<Directory key="1" type="movie" title="Movies"/>'))
    server.library.sectionByID.return_value = section
    client = Plex.__new__(Plex)
    client._plex = server

    items = list(client.get_items("1", query=MediaServerQueryOptions(fields=fields)))

    server.query.assert_called_once()
    request = server.query.call_args
    expected_guids = int(MediaServerField.IDENTITY in fields)
    assert f"includeGuids={expected_guids}" in request.args[0]
    assert request.kwargs["params"]["skipRefresh"] == 1
    assert "thumb" in request.kwargs["params"]["excludeFields"].split(",")
    excluded = request.kwargs["params"]["excludeElements"].split(",")
    assert ("Media" not in excluded) == (MediaServerField.PATH in fields)
    assert ("Guid" not in excluded) == (MediaServerField.IDENTITY in fields)
    assert items[0].path == ("/cloud/Demo.mkv" if fields else None)
    assert items[0].media_id == ("123" if fields else None)
    assert items[0].user_state is None


def test_plex_music_projection_is_http_params_not_search_filters():
    """
    音乐库搜索必须使用 SDK 的实际请求参数入口，不能把裁剪参数解释成搜索条件。
    """
    server = Mock()
    server.query.return_value = fromstring(
        '<MediaContainer size="1" totalSize="1"><Directory type="album" '
        'key="/library/metadata/2" ratingKey="2" title="Album" parentTitle="Artist" leafCount="10"/>'
        "</MediaContainer>"
    )
    section = MusicSection(server, fromstring('<Directory key="8" type="artist" title="Music"/>'))
    server.library.sections.return_value = [section]
    client = Plex.__new__(Plex)
    client._plex = server

    with patch("app.modules.plex.plex.logger.debug") as debug:
        items = client.get_music(album="Album", query=MediaServerQueryOptions(fields=set()))

    assert items, debug.call_args_list

    server.query.assert_called_once()
    request = server.query.call_args
    assert "includeGuids=0" in request.args[0]
    assert "title=Album" in request.args[0]
    assert "excludeFields" in request.kwargs["params"]
    assert items[0].title == "Album"
    assert items[0].note["song_count"] == 10


@pytest.mark.parametrize("module_type", [EmbyModule, JellyfinModule, PlexModule, NavidromeModule])
@pytest.mark.parametrize("query", [None, MediaServerQueryOptions(), MediaServerQueryOptions(fields=set())])
def test_module_forwards_projection_and_preserves_legacy_calls(module_type, query):
    """
    模块包装按原样转发选项，未传选项时保留旧客户端的位置参数与关键字。
    """
    module = object.__new__(module_type)
    client = Mock()
    module.get_instance = Mock(return_value=client)

    result = module.mediaserver_items("main", "library", 10, 20, query=query)

    args = (10, 20) if module_type is NavidromeModule else ("library", 10, 20)
    client.get_items.assert_called_once_with(*args, **({"query": query} if query is not None else {}))
    assert result is client.get_items.return_value


def test_skipped_provider_inherited_existence_does_not_receive_projection():
    """
    暂未改造的适配器使用共享基类查重时仍按旧客户端签名调用。
    """
    module = object.__new__(ZSpaceModule)
    client = Mock()
    client.get_movies.return_value = [SimpleNamespace(item_id="movie")]
    module.get_instance = Mock(return_value=client)
    media = SimpleNamespace(
        type=MediaType.MOVIE, title="Demo", year=2026, media_source=MediaSource.TMDB, media_id="123"
    )

    result = module.media_exists(media, server="main", query=MediaServerQueryOptions(fields=set()))

    assert result.itemid == "movie"
    client.get_movies.assert_called_once_with(title="Demo", year=2026, media_source=MediaSource.TMDB, media_id="123")


def test_navidrome_projected_pagination_does_not_enumerate_library():
    """
    显式精简分页直接发送服务端偏移与数量，不先读取全库后切片。
    """
    client = Navidrome.__new__(Navidrome)
    client._albums = Mock(side_effect=AssertionError("不能枚举全库"))
    client._call = Mock(
        side_effect=[
            {"albumList2": {"album": [{"id": str(i), "name": "Album"} for i in range(500)]}},
            {"albumList2": {"album": [{"id": "last", "name": "Last"}]}},
        ]
    )

    items = list(client.get_items(30, 501, query=MediaServerQueryOptions(fields=set())))

    assert len(items) == 501
    assert items[-1].item_id == "last"
    assert client._call.call_args_list == [
        call("getAlbumList2", type="alphabeticalByName", size=500, offset=30),
        call("getAlbumList2", type="alphabeticalByName", size=1, offset=530),
    ]
    client._albums.assert_not_called()


def test_navidrome_selected_music_library_can_skip_count():
    """
    显式选中的虚拟音乐库可用于同步，精简目录无需枚举专辑统计。
    """
    client = Navidrome.__new__(Navidrome)
    client.user = "user"
    client._sync_libraries = ["music"]
    client.get_items_count = Mock(side_effect=AssertionError("无需统计"))

    libraries = client.get_librarys(hidden=True, query=MediaServerQueryOptions(fields=set()))

    assert libraries[0].id == "music"
    assert libraries[0].item_count is None
    client.get_items_count.assert_not_called()


@pytest.mark.parametrize("query", [None, MediaServerQueryOptions(), MediaServerQueryOptions(fields=set())])
def test_navidrome_latest_limits_remote_work_only_for_explicit_projection(query):
    """
    首页精简查询只读取所需新专辑，默认选项仍保留原有枚举与图片行为。
    """
    client = Navidrome.__new__(Navidrome)
    album = {"id": "album", "name": "Album"}
    client._albums = Mock(return_value=[album])
    client._call = Mock(return_value={"albumList2": {"album": [album]}})
    client._album_cover = Mock(return_value="https://mediaserver.invalid/cover")
    client._play_host = "https://mediaserver.invalid/"

    items = client.get_latest(3, query=query)

    assert items[0].item_id == "album"
    if query is not None and query.fields is not None:
        client._albums.assert_not_called()
        client._call.assert_called_once_with("getAlbumList2", type="newest", size=3, offset=0)
        client._album_cover.assert_not_called()
        assert items[0].image is None
        assert items[0].link is None
    else:
        client._albums.assert_called_once_with("newest")
        client._call.assert_not_called()
        assert items[0].image == "https://mediaserver.invalid/cover"
