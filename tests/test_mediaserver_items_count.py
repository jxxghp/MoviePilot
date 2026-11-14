"""
验证不同媒体服务器的媒体库条目数量查询。
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.plex.plex import Plex
from app.modules.trimemedia.api import Api as TrimeMediaApi
from app.modules.trimemedia.api import Type as TrimeMediaType
from app.modules.ugreen.ugreen import Ugreen
from app.modules.zspace.zspace import ZSpace


class _FakeResponse:
    """
    模拟媒体服务器 HTTP 响应。
    """

    def __init__(self, payload: dict, status_code: int = 200):
        """
        保存响应数据和状态码。
        """
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        """
        返回模拟的 JSON 数据。
        """
        return self._payload


@pytest.mark.parametrize(
    ("client_class", "request_utils_path", "url_path"),
    [
        (Emby, "app.modules.emby.emby.RequestUtils", "emby/Users/user-id/Items"),
        (Jellyfin, "app.modules.jellyfin.jellyfin.RequestUtils", "Users/user-id/Items"),
    ],
)
def test_emby_compatible_items_count_uses_recursive_type_filter(
    client_class, request_utils_path, url_path
):
    """
    Emby 兼容服务应通过递归类型过滤查询媒体库总数。
    """
    client = client_class.__new__(client_class)
    client._host = "http://media.local/"
    client._apikey = "token"
    client.user = "user-id"

    with patch(request_utils_path) as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            {"TotalRecordCount": 23}
        )
        result = client.get_items_count("library-id")

    assert result == 23
    args = request_utils_cls.return_value.get_res.call_args.args
    assert args[0] == f"http://media.local/{url_path}"
    assert args[1]["Recursive"] == "true"
    assert args[1]["IncludeItemTypes"] == "Movie,Series"
    assert args[1]["Limit"] == 0


def test_zspace_items_count_uses_total_record_count():
    """
    极影视应使用兼容接口返回的 TotalRecordCount。
    """
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._apikey = "token"
    client.user = "user-id"

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            {"TotalRecordCount": 17}
        )
        result = client.get_items_count("library-id")

    assert result == 17
    params = request_utils_cls.return_value.get_res.call_args.kwargs["params"]
    assert params == {
        "ParentId": "library-id",
        "Recursive": "true",
        "Limit": 0,
    }


def test_plex_items_count_uses_section_total_size():
    """
    Plex 应直接读取媒体库分区的条目总数。
    """
    client = Plex.__new__(Plex)
    section = SimpleNamespace(totalSize=31)
    client._plex = SimpleNamespace(
        library=SimpleNamespace(sectionByID=lambda _library_id: section)
    )

    assert client.get_items_count("9") == 31


def test_ugreen_items_count_uses_library_video_count():
    """
    绿联影视应复用媒体库列表中的视频总数。
    """
    client = Ugreen.__new__(Ugreen)
    client._host = "http://ugreen.local"
    client._username = "tester"
    client._password = "secret"
    client._userinfo = {"name": "tester"}
    client._api = SimpleNamespace(token="token")
    client._libraries = {"library-id": {"video_count": 42}}

    assert client.get_items_count("library-id") == 42


def test_trimemedia_item_count_reads_list_total():
    """
    飞牛影视应通过媒体列表接口的 total 字段获取总数。
    """
    api = TrimeMediaApi.__new__(TrimeMediaApi)
    response = SimpleNamespace(success=True, data={"list": [], "total": 19})

    with patch.object(TrimeMediaApi, "request", return_value=response) as request:
        result = api.item_count(
            guid="library-id",
            types=[TrimeMediaType.MOVIE, TrimeMediaType.TV],
        )

    assert result == 19
    request.assert_called_once_with(
        "/item/list",
        data={
            "ancestor_guid": "library-id",
            "tags": {"type": [TrimeMediaType.MOVIE, TrimeMediaType.TV]},
            "exclude_grouped_video": 1,
            "page": 1,
            "page_size": 1,
        },
    )
