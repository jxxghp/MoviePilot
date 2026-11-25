"""
验证飞书资源选择卡片的种子详情、缺失字段和选择按钮。
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.domain.context import Context
from app.modules.feishu.feishu import Feishu
from app.modules.feishu.openapi import FeishuOpenApi, FeishuResponse
from app.schemas.context import TorrentInfo
from app.schemas.message import Message


def _build_client() -> tuple[Feishu, MagicMock]:
    """
    构造隔离长连接与开放平台请求的客户端，保留真实卡片渲染路径。
    """
    api = MagicMock(spec=FeishuOpenApi)
    api.create_message.return_value = FeishuResponse(
        code=0, msg="success", data={"message_id": "om_test", "chat_id": "oc_test"},
    )
    with (
        patch.object(Feishu, "_build_api_client", return_value=api),
        patch.object(Feishu, "_start_ws_client"),
    ):
        client = Feishu(FEISHU_APP_ID="test_app", FEISHU_APP_SECRET="test_secret")
    return client, api


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"site_name": "站点", "size": 2.5 * 1024 ** 3, "seeders": 35}, "1.【站点】影片 2.5G 35↑"),
        ({"site_name": "站点", "size": 2.5 * 1024 ** 3}, "1.【站点】影片 2.5G"),
        ({"site_name": "站点", "seeders": 35}, "1.【站点】影片 35↑"),
        ({"size": 2.5 * 1024 ** 3, "seeders": 35}, "1. 影片 2.5G 35↑"),
        ({"site_name": "站点"}, "1.【站点】影片"),
        ({"size": 2.5 * 1024 ** 3}, "1. 影片 2.5G"),
        ({"seeders": 35}, "1. 影片 35↑"),
        ({}, "1. 影片"),
        ({"site_name": None, "size": None, "seeders": None}, "1. 影片"),
        ({"site_name": "", "size": 0, "seeders": 0}, "1. 影片"),
        ({"title": None, "site_name": "站点"}, "1.【站点】站点"),
        ({"title": None}, "1. 未知种子"),
    ],
)
def test_torrent_card_displays_available_details(fields: dict, expected: str) -> None:
    """
    实际发送的卡片显示可用站点、大小和做种数，省略空值且不附加促销文案。
    """
    client, api = _build_client()
    torrent = TorrentInfo(**{"title": "影片", "volume_factor": "免费", **fields})

    result = client.send_torrents_message(
        Message(title="请选择下载"), [Context(torrent_info=torrent)], userid="ou_user",
    )

    assert result["success"]
    card = json.loads(api.create_message.call_args.kwargs["content"])
    assert card["body"]["elements"][1]["content"] == expected


def test_torrent_card_keeps_selection_order_and_target() -> None:
    """
    列表仅展示前十项，缺失种子信息仍保留序号，接收会话、链接和下载按钮保持可用。
    """
    client, api = _build_client()
    torrents = [Context()] + [
        Context(torrent_info=TorrentInfo(title=f"影片{index}", seeders=index))
        for index in range(2, 12)
    ]
    result = client.send_torrents_message(
        Message(
            title="请选择下载", link="https://example.com/search",
            buttons=[[{"text": "下载第2项", "callback_data": "2"}]],
        ),
        torrents, chat_id="oc_target", receive_id_type="chat_id",
    )

    assert result["success"]
    request = api.create_message.call_args.kwargs
    assert request["receive_id"] == "oc_target"
    assert request["receive_id_type"] == "chat_id"
    card = json.loads(request["content"])
    content = card["body"]["elements"][1]["content"]
    assert content.splitlines()[:10] == ["1. 未知种子"] + [
        f"{index}. 影片{index} {index}↑" for index in range(2, 11)
    ]
    assert "影片11" not in content
    assert "https://example.com/search" in content
    button = card["body"]["elements"][-1]["columns"][0]["elements"][0]
    assert button["behaviors"] == [{"type": "callback", "value": {"callback_data": "2"}}]
