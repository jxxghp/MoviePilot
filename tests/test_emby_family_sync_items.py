"""
Emby / Jellyfin 媒体库同步的分页、内存释放及失败保护回归。
"""

import weakref
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from requests import Response

from app.chain.mediaserver import _SYNC_QUERY, MediaServerChain
from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin

_REQUEST_TARGETS = {
    Emby: "app.modules.emby.emby.RequestUtils",
    Jellyfin: "app.modules.jellyfin.jellyfin.RequestUtils",
}


class _ItemPage(list):
    """
    允许以弱引用检查已消费的原始条目页是否仍被生成器保留。
    """


def _response(items, total=None):
    """
    构造不会额外缓存响应或条目页的离线 HTTP 回执。
    """
    response = Response()
    response.status_code = 200
    response._content = b"synthetic-response-body"
    response._content_consumed = True
    payload = {"Items": items}
    if total is not None:
        payload["TotalRecordCount"] = total
    response.json = lambda: payload
    return response


def _movie(index):
    """
    生成具有唯一标识的合成电影条目。
    """
    return {"Id": str(index), "Type": "Movie", "Name": f"电影 {index}"}


def _client(client_class: type):
    """
    构造跳过远端初始化的媒体服务器客户端。
    """
    instance = client_class.__new__(client_class)
    instance._host = "http://mediaserver.local/"
    instance._apikey = "api-key"
    instance._playhost = None
    instance._sync_libraries = []
    instance.user = "user-id"
    return instance


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("response", [None, Mock(status_code=500)])
def test_get_items_raises_when_library_read_fails(client_class: type, response):
    """
    列表请求失败必须中止同步，不能产出空结果让同步链删除该服务器的旧缓存。
    """
    client = _client(client_class)
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.return_value = response
        with pytest.raises(ConnectionError):
            list(client.get_items("movies"))


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_get_items_raises_when_nested_folder_read_fails(client_class: type):
    """
    子文件夹读取失败同样中止同步，避免只同步到部分条目。
    """
    client = _client(client_class)
    folder = Mock(status_code=200)
    folder.json.return_value = {"Items": [{"Id": "folder-1", "Type": "Folder"}]}
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = [folder, TimeoutError("read timeout")]
        with pytest.raises(TimeoutError, match="read timeout"):
            list(client.get_items("movies"))


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_get_items_raises_when_server_is_not_connected(client_class: type):
    """
    未连接的服务器不得被同步为空媒体库。
    """
    client = _client(client_class)
    client._apikey = None

    with pytest.raises(ConnectionError):
        list(client.get_items("movies"))


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("limit", [None, -1])
@pytest.mark.parametrize("total", [451, 2001])
def test_unlimited_items_are_lazy_and_release_consumed_pages(client_class, limit, total):
    """
    全量同步必须逐页读取，下一次请求前释放上一页及其原始响应字节。
    """
    client = _client(client_class)
    response_refs = []
    page_refs = []

    def fetch(_url, params):
        """
        按请求范围生成条目，并检查前一页已退出生成器的持有范围。
        """
        assert all(reference() is None for reference in response_refs)
        assert all(reference() is None for reference in page_refs)
        assert 0 < params["Limit"] <= 200
        start = params["StartIndex"]
        page = _ItemPage(_movie(index) for index in range(start, min(start + params["Limit"], total)))
        response = _response(page, total)
        response_refs.append(weakref.ref(response))
        page_refs.append(weakref.ref(page))
        return response

    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = fetch
        items = client.get_items("movies", start_index=7, limit=limit)
        assert request.return_value.get_res.call_count == 0
        first = next(items)
        assert first.item_id == "0"
        assert request.return_value.get_res.call_count == 1
        # 处理当前页期间也不应保留已解析的 HTTP 响应原始字节。
        assert response_refs[0]() is None
        identifiers = [first.item_id, *(item.item_id for item in items)]

    assert identifiers == [str(index) for index in range(total)]
    assert len(page_refs) == (total + 199) // 200
    assert all(reference() is None for reference in page_refs)


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_filtered_page_without_total_does_not_end_enumeration(client_class):
    """
    无总数响应按原始页长度前进，整页都是不可同步视频时仍读取后续电影。
    """
    client = _client(client_class)

    def fetch(_url, params):
        """
        首页只含过滤条目，第二页返回可同步电影。
        """
        assert 0 < params["Limit"] <= 200
        if params["StartIndex"] == 0:
            return _response([{"Id": str(index), "Type": "Video"} for index in range(params["Limit"])])
        assert params["StartIndex"] == params["Limit"]
        return _response([_movie("last")])

    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = fetch
        assert [item.item_id for item in client.get_items("movies")] == ["last"]
        assert request.return_value.get_res.call_count == 2


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_explicit_page_keeps_nested_folder_pagination_and_projection(client_class):
    """
    显式分页只约束本层原始条目，子目录仍分页读全并保留查询投影。
    """
    client = _client(client_class)

    def fetch(_url, params):
        """
        根目录返回一个文件夹，子目录跨两页返回电影、电视剧和音乐专辑。
        """
        assert params["EnableImages"] == "false"
        assert params["EnableUserData"] == "false"
        assert "ProviderIds" in params["Fields"]
        if params["ParentId"] == "movies":
            assert params["StartIndex"] == 7
            assert params["Limit"] == 1
            return _response([{"Id": "folder", "Type": "Folder"}], 9)
        assert params["ParentId"] == "folder"
        assert 0 < params["Limit"] <= 200
        if params["StartIndex"] == 0:
            return _response([_movie(index) for index in range(params["Limit"])], params["Limit"] + 2)
        assert params["StartIndex"] == params["Limit"]
        return _response([
            {"Id": "series", "Type": "Series", "Name": "剧集"},
            {"Id": "album", "Type": "MusicAlbum", "Name": "专辑"},
        ], params["Limit"] + 2)

    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = fetch
        items = list(client.get_items("movies", start_index=7, limit=1, query=_SYNC_QUERY))
        assert request.return_value.get_res.call_count == 3

    assert len(items) == 202
    assert [item.item_type for item in items[-2:]] == ["Series", "MusicAlbum"]


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_total_count_allows_server_to_return_smaller_pages(client_class):
    """
    服务端实际页长小于请求上限时，依据原始总数继续读取，不漏掉后续条目。
    """
    client = _client(client_class)

    def fetch(_url, params):
        """
        模拟服务端把每页限制为两条，完整库有四条。
        """
        assert params["StartIndex"] in {0, 2}
        return _response([_movie(index) for index in range(params["StartIndex"], params["StartIndex"] + 2)], 4)

    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = fetch
        assert [item.item_id for item in client.get_items("movies")] == ["0", "1", "2", "3"]
        assert request.return_value.get_res.call_count == 2


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("failure", [None, TimeoutError("read timeout"), ValueError("invalid JSON")])
def test_later_page_failure_preserves_stale_cache(client_class, failure):
    """
    首个条目写入后发生分页错误，真实同步链必须中止且不得清理未枚举的旧数据。
    """
    client = _client(client_class)
    chain = MediaServerChain.__new__(MediaServerChain)
    chain.items = lambda **kwargs: client.get_items(kwargs["library_id"], query=kwargs["query"])
    repository = Mock()
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = [_response([_movie("new")], 2), failure]
        with pytest.raises((ConnectionError, TimeoutError, ValueError)):
            chain._sync_server_libraries(
                "test", [SimpleNamespace(id="movies", name="电影")], {"movies": 2}, repository,
                "2026-11-04 00:00:00", None, 1, 1, 2, 0,
            )

    repository.upsert.assert_called_once()
    repository.delete_stale.assert_not_called()


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("status", [200, 503])
def test_page_response_is_closed_when_reading_fails(client_class, status):
    """
    HTTP 错误和 JSON 解码失败都关闭响应，异常仍沿既有同步路径上抛。
    """
    client = _client(client_class)
    response = Mock(status_code=status)
    response.json.side_effect = ValueError("invalid JSON")
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.return_value = response
        with pytest.raises(ValueError if status == 200 else ConnectionError):
            list(client.get_items("movies"))
    response.close.assert_called_once()


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_explicit_page_does_not_fill_filtered_results(client_class):
    """
    显式页长表示服务端原始条目数量，不能为了补齐过滤后的结果而额外翻页。
    """
    client = _client(client_class)
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.return_value = _response([
            {"Id": "video", "Type": "Video"}, _movie("movie"),
        ], 10)
        assert [item.item_id for item in client.get_items("movies", start_index=7, limit=2)] == ["movie"]
        request.return_value.get_res.assert_called_once()
        params = request.return_value.get_res.call_args.args[1]
        assert params["StartIndex"] == 7
        assert params["Limit"] == 2


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("total", [None, 0])
def test_empty_page_finishes_enumeration(client_class, total):
    """
    空目录在有无总数时都只读取一页，避免无数据时反复请求。
    """
    client = _client(client_class)
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.return_value = _response([], total)
        assert list(client.get_items("movies")) == []
        request.return_value.get_res.assert_called_once()


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
def test_boxset_expansion_keeps_server_specific_behavior(client_class):
    """
    分页不改变服务差异：Jellyfin 展开合集，Emby 仍按现有规则过滤合集。
    """
    client = _client(client_class)
    with patch(_REQUEST_TARGETS[client_class]) as request:
        request.return_value.get_res.side_effect = [
            _response([{"Id": "boxset", "Type": "BoxSet"}]), _response([_movie("movie")]),
        ]
        identifiers = [item.item_id for item in client.get_items("movies")]
        assert identifiers == (["movie"] if client_class is Jellyfin else [])
        assert request.return_value.get_res.call_count == (2 if client_class is Jellyfin else 1)
