import unittest
from typing import Any
from unittest.mock import Mock, patch

from app import schemas
from app.api.endpoints.mediaserver import play_item
from app.modules.emby.emby import Emby


class _FakeResponse:
    """提供 Emby 接口响应的最小 json 封装。"""

    def __init__(self, payload: Any):
        """保存测试预置的响应体。"""
        self._payload = payload

    def json(self) -> Any:
        """返回测试预置的响应体。"""
        return self._payload


class EmbyDashboardLinksTest(unittest.TestCase):
    """验证 Emby 仪表盘条目使用真实媒体服务器标识生成跳转链接。"""

    @staticmethod
    def _build_client() -> Emby:
        """构造绕过真实初始化的 Emby 实例。"""
        client = Emby.__new__(Emby)
        client._host = "http://emby.local/"
        client._playhost = None
        client._apikey = "api-key"
        client._sync_libraries = []
        client.user = "user-id"
        client.serverid = "server-id"
        return client

    def test_get_server_id_falls_back_to_emby_prefixed_system_info(self):
        """
        兼容 Emby 反代只暴露 /emby/System/Info 的场景，避免生成 serverId=None。
        """
        client = self._build_client()
        client.serverid = None

        with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.side_effect = [
                None,
                _FakeResponse({"Id": "server-id"}),
            ]

            server_id = client.get_server_id()

        self.assertEqual(server_id, "server-id")
        self.assertEqual(
            request_utils_cls.return_value.get_res.call_args_list[0].args[0],
            "http://emby.local/System/Info",
        )
        self.assertEqual(
            request_utils_cls.return_value.get_res.call_args_list[1].args[0],
            "http://emby.local/emby/System/Info",
        )

    def test_get_play_url_omits_missing_server_id(self):
        """serverId 为空时不应把 None 字符串拼入播放链接。"""
        client = self._build_client()
        client.serverid = None

        play_url = client.get_play_url("item-id")

        self.assertEqual(
            play_url,
            "http://emby.local/web/index.html#!/item?id=item-id&context=home",
        )

    def test_get_latest_returns_item_and_server_ids(self):
        """最近入库条目需要显式返回 Emby item_id 和 server_id 供前端纠偏链接。"""
        client = self._build_client()
        client.get_user_library_folders = Mock(return_value=[])

        with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
            request_utils_cls.return_value.get_res.return_value = _FakeResponse([
                {
                    "Id": "emby-item-id",
                    "ServerId": "item-server-id",
                    "Name": "测试电影",
                    "Type": "Movie",
                    "ProductionYear": 2026,
                }
            ])

            items = client.get_latest()

        self.assertEqual(items[0].id, "emby-item-id")
        self.assertEqual(items[0].item_id, "emby-item-id")
        self.assertEqual(items[0].server_id, "item-server-id")
        self.assertIn("id=emby-item-id", items[0].link)
        self.assertIn("serverId=item-server-id", items[0].link)

    def test_get_librarys_returns_item_and_server_ids(self):
        """媒体库卡片需要返回 Emby parentId 和 server_id 供前端生成 App 跳转。"""
        client = self._build_client()

        with (
            patch.object(client, "_Emby__get_emby_librarys") as librarys,
            patch.object(client, "_Emby__get_local_image_by_id") as image_by_id,
        ):
            librarys.return_value = [
                {
                    "Id": "library-id",
                    "ServerId": "library-server-id",
                    "Name": "电影库",
                    "CollectionType": "movies",
                }
            ]
            image_by_id.return_value = "http://emby.local/image"

            items = client.get_librarys()

        self.assertEqual(items[0].id, "library-id")
        self.assertEqual(items[0].item_id, "library-id")
        self.assertEqual(items[0].server_id, "library-server-id")
        self.assertIn("parentId=library-id", items[0].link)
        self.assertIn("serverId=library-server-id", items[0].link)

    def test_play_item_returns_server_type(self):
        """播放地址接口需要返回 server_type，供前端跳转时选择正确媒体服务器类型。"""
        item = schemas.MediaServerItem(server="emby", item_id="emby-item-id", server_id="server-id")

        with (
            patch("app.api.endpoints.mediaserver.MediaServerHelper") as helper_cls,
            patch("app.api.endpoints.mediaserver.MediaServerChain") as chain_cls,
        ):
            helper_cls.return_value.get_configs.return_value = {"Emby": object()}
            chain = chain_cls.return_value
            chain.iteminfo.return_value = item
            chain.get_play_url.return_value = "http://emby.local/web/index.html#!/item?id=emby-item-id"

            response = play_item("emby-item-id")

        self.assertTrue(response.success)
        self.assertEqual(response.data["url"], "http://emby.local/web/index.html#!/item?id=emby-item-id")
        self.assertEqual(response.data["item_id"], "emby-item-id")
        self.assertEqual(response.data["server_id"], "server-id")
        self.assertEqual(response.data["server_type"], "emby")
