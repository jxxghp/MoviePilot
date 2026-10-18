"""
验证 Emby 元数据查询跳过图片处理，同时保留同步数据和仪表盘封面。
"""

from unittest.mock import Mock

import pytest

from app.modules.emby.emby import Emby
from app.schemas.types import MediaSource


@pytest.fixture
def emby_request(monkeypatch: pytest.MonkeyPatch) -> tuple[Emby, Mock]:
    """
    提供跳过远端初始化的客户端及可撤销的 HTTP 请求替身。

    :param monkeypatch: 在用例结束时恢复请求边界
    :return: Emby 客户端和返回离线响应的请求方法
    """
    client = Emby.__new__(Emby)
    client._host = "http://emby.test/"
    client._playhost = None
    client._apikey = "test-key"
    client._sync_libraries = []
    client.user = "user-id"
    client.serverid = "server-id"
    request = Mock()
    monkeypatch.setattr("app.modules.emby.emby.RequestUtils", Mock(return_value=request))
    return client, request.get_res


@pytest.mark.parametrize("method_name,item_type", [
    ("get_movies", "Movie"),
    ("get_music", "MusicAlbum"),
])
def test_metadata_queries_preserve_identity_path_and_user_state(
    emby_request: tuple[Emby, Mock], method_name: str, item_type: str,
) -> None:
    """
    搜索不请求图片，仍能转换来源身份、路径及用户播放状态。

    :param emby_request: 隔离的客户端和请求方法
    :param method_name: 待验证的元数据读取方法
    :param item_type: 服务端返回的电影或音乐专辑类型
    """
    client, request = emby_request
    item = {
        "Id": "item-id",
        "Type": item_type,
        "Name": "测试媒体",
        "ProductionYear": 2026,
        "ProviderIds": {"Tmdb": "123"},
        "ParentId": "library-id",
        "Path": "/media/test.strm",
        "UserData": {"Played": True, "PlayCount": 2},
    }
    response = Mock(status_code=200)
    response.json.return_value = {"Items": [item]}
    request.return_value = response

    result = getattr(client, method_name)(title="测试媒体")[0]

    assert result.item_id == "item-id"
    assert result.media_source == MediaSource.TMDB
    assert result.media_id == "123"
    assert result.path == "/media/test.strm"
    assert result.user_state.play_count == 2
    assert request.call_args.args[1]["EnableImages"] == "false"


def test_library_sync_skips_images_in_nested_folders(
    emby_request: tuple[Emby, Mock],
) -> None:
    """
    分页同步及递归子目录都跳过图片，并保留原有分页范围和条目转换。

    :param emby_request: 隔离的客户端和请求方法
    """
    client, request = emby_request
    root_response = Mock(status_code=200)
    root_response.json.return_value = {"Items": [{"Id": "folder-id", "Type": "Folder"}]}
    child_response = Mock(status_code=200)
    child_response.json.return_value = {"Items": [{
        "Id": "movie-id",
        "Type": "Movie",
        "Name": "测试电影",
        "ProviderIds": {"Tmdb": "123"},
        "Path": "/media/test.strm",
    }]}
    request.side_effect = [root_response, child_response]

    items = list(client.get_items("library-id", start_index=5, limit=10))

    assert [item.item_id for item in items] == ["movie-id"]
    assert items[0].path == "/media/test.strm"
    assert request.call_count == 2
    root_params, child_params = [call.args[1] for call in request.call_args_list]
    assert root_params["ParentId"] == "library-id"
    assert root_params["StartIndex"] == 5
    assert root_params["Limit"] == 10
    assert child_params["ParentId"] == "folder-id"
    assert "Limit" not in child_params
    assert "StartIndex" not in child_params
    for params in (root_params, child_params):
        assert params["EnableImages"] == "false"
        assert "ProviderIds" in params["Fields"]
        assert "UserDataPlayCount" in params["Fields"]


def test_episode_lookup_skips_images_in_search_and_episode_list(
    emby_request: tuple[Emby, Mock],
) -> None:
    """
    剧集搜索和分集列表跳过图片，并保持单条详情接口、季号过滤和身份校验。

    :param emby_request: 隔离的客户端和请求方法
    """
    client, request = emby_request
    series = {
        "Id": "series-id", "Type": "Series", "Name": "测试剧集",
        "ProductionYear": 2026, "ProviderIds": {"Tmdb": "123"},
    }
    search_response = Mock(status_code=200)
    search_response.json.return_value = {"Items": [series]}
    detail_response = Mock(status_code=200)
    detail_response.json.return_value = series
    episode_response = Mock(status_code=200)
    episode_response.json.return_value = {"Items": [
        {"ParentIndexNumber": 1, "IndexNumber": 2},
        {"ParentIndexNumber": 2, "IndexNumber": 1},
        {"ParentIndexNumber": 1},
    ]}
    request.side_effect = [search_response, detail_response, episode_response]

    result = client.get_tv_episodes(
        title="测试剧集", year=2026, season=1,
        media_source=MediaSource.TMDB, media_id="123",
    )

    assert result == ("series-id", {1: [2]})
    assert request.call_count == 3
    for call in (request.call_args_list[0], request.call_args_list[2]):
        assert call.args[1]["EnableImages"] == "false"
    # 单条详情接口不支持 EnableImages，保留原有读取方式和返回字段。
    assert "EnableImages" not in request.call_args_list[1].args[1]
    assert request.call_args_list[0].args[1]["Limit"] == 10
    assert request.call_args.args[1]["Season"] == 1
    assert request.call_args.args[1]["IsMissing"] == "false"


def test_episode_id_lookup_preserves_episode_mapping(
    emby_request: tuple[Emby, Mock],
) -> None:
    """
    分集标识查询跳过图片，仍过滤缺失集号或标识的条目。

    :param emby_request: 隔离的客户端和请求方法
    """
    client, request = emby_request
    response = Mock(status_code=200)
    response.json.return_value = {"Items": [
        {"Id": "episode-id", "IndexNumber": 2},
        {"Id": "missing-index"},
        {"IndexNumber": 3},
    ]}
    request.return_value = response

    assert client.get_season_episode_ids("series-id", 1) == {2: "episode-id"}
    assert request.call_args.args[1]["EnableImages"] == "false"
    assert request.call_args.args[1]["Season"] == 1


@pytest.mark.parametrize("method_name", ["get_librarys", "get_resume", "get_latest"])
def test_dashboard_queries_keep_primary_image_tags(
    emby_request: tuple[Emby, Mock], monkeypatch: pytest.MonkeyPatch, method_name: str,
) -> None:
    """
    仪表盘媒体库、继续观看和最近入库仍请求图片，并保留封面版本标签。

    :param emby_request: 隔离的客户端和请求方法
    :param monkeypatch: 用于隔离额外的目录和数量查询
    :param method_name: 依赖图片标签的仪表盘查询
    """
    client, request = emby_request
    monkeypatch.setattr(client, "get_user_library_folders", Mock(return_value=[]))
    monkeypatch.setattr(client, "get_items_count", Mock(return_value=0))
    item = {
        "Id": "item-id", "Type": "Movie", "Name": "测试电影",
        "CollectionType": "movies", "ImageTags": {"Primary": "primary-tag"},
    }
    response = Mock(status_code=200)
    response.json.return_value = [item] if method_name == "get_latest" else {"Items": [item]}
    request.return_value = response

    items = getattr(client, method_name)()

    assert len(items) == 1
    assert items[0].image == "http://emby.test/Items/item-id/Images/Primary?tag=primary-tag"
    assert request.call_args.args[1].get("EnableImages") not in (False, "false")
