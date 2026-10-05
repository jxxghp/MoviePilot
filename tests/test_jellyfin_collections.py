"""Jellyfin 合集成员的存在性查询、统计和媒体库遍历回归。"""

from unittest.mock import Mock, patch

import pytest

from app.modules.jellyfin.jellyfin import Jellyfin
from app.schemas.types import MediaSource


@pytest.fixture
def client():
    """构造跳过远端初始化的 Jellyfin 客户端。"""
    instance = Jellyfin.__new__(Jellyfin)
    instance._host = "http://jellyfin.local/"
    instance._apikey = "api-key"
    instance._playhost = None
    instance._sync_libraries = []
    instance.user = "user-id"
    return instance


def _response(payload: dict) -> Mock:
    """返回带成功状态码的离线 JSON 响应。"""
    response = Mock(status_code=200)
    response.json.return_value = payload
    return response


def _movie(item_id: str, **fields) -> dict:
    """构造保留来源身份及父目录的电影条目。"""
    return {
        "Id": item_id,
        "Type": "Movie",
        "Name": "合集内电影",
        "ProductionYear": 2026,
        "ProviderIds": {"Tmdb": "123"},
        "ParentId": "movies",
        **fields,
    }


@pytest.mark.parametrize("media_source,media_id", [(None, None), (MediaSource.TMDB, "123")])
def test_get_movies_finds_collection_members(client, media_source, media_id):
    """电影查询应展开合集，同时保留标题、年份和来源身份匹配规则。"""
    movies = [
        _movie("matched"),
        _movie("other-title", Name="其他电影"),
        _movie("other-year", ProductionYear=2025),
        _movie("other-id", ProviderIds={"Tmdb": "456"}),
    ]

    def get_res(_url, params):
        """模拟 Jellyfin 按请求参数返回电影或折叠后的合集。"""
        items = movies if params.get("CollapseBoxSetItems") == "false" else [
            {"Id": "collection", "Type": "BoxSet", "Name": "系列合集"}
        ]
        return _response({"Items": items})

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request:
        request.return_value.get_res.side_effect = get_res
        result = client.get_movies("合集内电影", "2026", media_source, media_id)

    expected = ["matched", "other-id"] if media_id is None else ["matched"]
    assert [item.item_id for item in result] == expected
    assert all(item.item_type == "Movie" for item in result)
    params = request.return_value.get_res.call_args.args[1]
    assert params["IncludeItemTypes"] == "Movie"
    assert params["Recursive"] == "true"
    assert params["searchTerm"] == "合集内电影"


@pytest.mark.parametrize("item_types", ["Movie", "Movie,Series", "MusicAlbum"])
def test_get_items_count_disables_collection_collapsing(client, item_types):
    """计数应包含合集成员，并保留调用方指定的媒体类型。"""
    def get_res(_url, params):
        """回放 Issue 中折叠前后 51/136 的计数差异。"""
        count = 136 if params.get("CollapseBoxSetItems") == "false" else 51
        return _response({"TotalRecordCount": count})

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request:
        request.return_value.get_res.side_effect = get_res
        count = client.get_items_count("movies", include_item_types=item_types)

    assert count == 136
    params = request.return_value.get_res.call_args.args[1]
    assert params["ParentId"] == "movies"
    assert params["IncludeItemTypes"] == item_types
    assert params["Recursive"] == "true"
    assert params["Limit"] == 0


def test_library_and_dashboard_counts_include_collection_members(client):
    """媒体库卡片和仪表板应共用展开合集后的电影计数。"""
    def get_res(url, params):
        """模拟一个启用了合集折叠的电影媒体库。"""
        if url.endswith("/Views"):
            return _response({"Items": [{
                "Id": "movies", "Name": "电影", "CollectionType": "movies",
            }]})
        count = 136 if params.get("CollapseBoxSetItems") == "false" else 51
        return _response({"TotalRecordCount": count})

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request:
        request.return_value.get_res.side_effect = get_res
        libraries = client.get_librarys()
        statistics = client.get_medias_count()

    assert libraries[0].item_count == 136
    assert statistics.movie_count == 136


@pytest.mark.parametrize("limit", [None, -1])
def test_get_items_expands_nested_boxsets_and_folders(client, limit):
    """同步应递归读取 Folder 和 BoxSet，保留普通电影、剧集和音乐专辑。"""
    contents = {
        "movies": [
            _movie("standalone"),
            {"Id": "collection", "Type": "BoxSet"},
            {"Id": "series", "Type": "Series", "Name": "剧集"},
            {"Id": "album", "Type": "MusicAlbum", "Name": "专辑"},
        ],
        "collection": [
            _movie("member"),
            {"Id": "folder", "Type": "Folder"},
        ],
        "folder": [{"Id": "nested-collection", "Type": "BoxSet"}],
        "nested-collection": [_movie("nested-member")],
    }

    def get_res(_url, params):
        """按父条目 ID 返回合集和目录的真实层级结构。"""
        return _response({"Items": contents[params["ParentId"]]})

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request:
        request.return_value.get_res.side_effect = get_res
        items = list(client.get_items("movies", limit=limit))

    assert [item.item_id for item in items] == [
        "standalone", "member", "nested-member", "series", "album",
    ]
    assert all(item.item_type != "BoxSet" for item in items)
    assert items[1].media_id == "123"
    assert items[1].library == "movies"
    assert [call.args[1]["ParentId"] for call in request.return_value.get_res.call_args_list] == [
        "movies", "collection", "folder", "nested-collection",
    ]


def test_get_items_pagination_does_not_truncate_collection_members(client):
    """父目录分页参数不能传给合集，否则会漏掉合集成员。"""
    def get_res(_url, params):
        """返回父目录的一页数据和合集中完整的三部电影。"""
        if params["ParentId"] == "movies":
            return _response({"Items": [
                {"Id": "collection", "Type": "BoxSet"}, _movie("standalone"),
            ]})
        return _response({"Items": [_movie(f"member-{index}") for index in range(3)]})

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request:
        request.return_value.get_res.side_effect = get_res
        items = list(client.get_items("movies", start_index=5, limit=2))

    assert [item.item_id for item in items] == [
        "member-0", "member-1", "member-2", "standalone",
    ]
    parent_call, collection_call = request.return_value.get_res.call_args_list
    assert parent_call.args[1]["StartIndex"] == 5
    assert parent_call.args[1]["Limit"] == 2
    assert collection_call.args[1]["ParentId"] == "collection"
    assert "StartIndex" not in collection_call.args[1]
    assert "Limit" not in collection_call.args[1]
