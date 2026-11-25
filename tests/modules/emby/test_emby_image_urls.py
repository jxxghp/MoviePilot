"""
验证 Emby 图片版本标签参与图片 URL 构造。
"""

from unittest.mock import Mock, patch

from app.modules.emby.emby import Emby


class _FakeResponse:
    """
    提供 Emby 接口响应的最小 JSON 封装。
    """

    def __init__(self, payload):
        """
        保存测试响应体。
        """
        self._payload = payload

    def json(self):
        """
        返回测试响应体。
        """
        return self._payload


def _client() -> Emby:
    """
    构造跳过远端初始化的 Emby 客户端。
    """
    client = Emby.__new__(Emby)
    client._host = "http://emby.local/"
    client._playhost = None
    client._apikey = "api-key"
    client._sync_libraries = []
    client.user = "user-id"
    client.serverid = "server-id"
    return client


def test_local_primary_image_url_includes_image_tag() -> None:
    """
    封面标签应成为 URL 的版本参数，使缓存键随原地换图变化。
    """
    client = _client()

    assert client._Emby__get_local_image_by_id("item-id", "primary-tag") == (
        "http://emby.local/Items/item-id/Images/Primary?tag=primary-tag"
    )
    assert client._Emby__get_local_image_by_id("item-id") == (
        "http://emby.local/Items/item-id/Images/Primary"
    )


def test_get_librarys_uses_primary_image_tag() -> None:
    """
    媒体库封面应使用 Views 返回的 Primary 标签。
    """
    client = _client()
    client.get_items_count = Mock(return_value=0)

    with patch.object(client, "_Emby__get_emby_librarys") as librarys:
        librarys.return_value = [{
            "Id": "library-id",
            "Name": "电影库",
            "CollectionType": "movies",
            "ImageTags": {"Primary": "library-tag"},
        }]

        result = client.get_librarys()

    assert result[0].image == (
        "http://emby.local/Items/library-id/Images/Primary?tag=library-tag"
    )


def test_get_resume_uses_primary_image_tag_for_movie_fallback() -> None:
    """
    继续观看电影没有背景图时应使用电影主图标签。
    """
    client = _client()
    client.get_user_library_folders = Mock(return_value=[])

    with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse({
            "Items": [{
                "Id": "movie-id",
                "Type": "Movie",
                "Name": "测试电影",
                "ImageTags": {"Primary": "movie-tag"},
                "BackdropImageTags": [],
            }]
        })

        result = client.get_resume()

    assert result[0].image == "http://emby.local/Items/movie-id/Images/Primary?tag=movie-tag"
    assert request_utils_cls.return_value.get_res.call_args.args[1]["Fields"] == (
        "ProductionYear,Path,ImageTags"
    )


def test_get_latest_uses_primary_image_tag() -> None:
    """
    最近入库条目的主图 URL 应携带图片标签。
    """
    client = _client()
    client.get_user_library_folders = Mock(return_value=[])

    with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse([{
            "Id": "movie-id",
            "Type": "Movie",
            "Name": "测试电影",
            "ImageTags": {"Primary": "movie-tag"},
        }])

        result = client.get_latest()

    assert result[0].image == "http://emby.local/Items/movie-id/Images/Primary?tag=movie-tag"
    assert request_utils_cls.return_value.get_res.call_args.args[1]["Fields"] == (
        "ProductionYear,Path,BackdropImageTags,ImageTags"
    )
