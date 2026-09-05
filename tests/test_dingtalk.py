"""钉钉自定义机器人通知渠道测试。"""

import base64
import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from app.modules.dingtalk import DingTalkModule
from app.modules.dingtalk.dingtalk import DingTalk
from app.schemas.message import Message
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import MessageType, NotificationChannel


def _response(payload: dict, status_code: int = 200) -> Mock:
    """构造带关闭能力的 requests.Response 测试替身。"""
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    return response


def test_signed_webhook_preserves_access_token_and_uses_official_signature() -> None:
    """签名应使用毫秒时间戳、换行分隔串和 HMAC-SHA256。"""
    client = DingTalk(
        DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=token",
        DINGTALK_SECRET="SEC-test",
    )

    signed_url = client.build_request_url(timestamp=1720000000123)
    query = parse_qs(urlsplit(signed_url).query)
    expected = base64.b64encode(
        hmac.new(
            b"SEC-test",
            b"1720000000123\nSEC-test",
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    assert query == {
        "access_token": ["token"],
        "timestamp": ["1720000000123"],
        "sign": [expected],
    }


def test_send_msg_posts_markdown_and_accepts_dingtalk_success() -> None:
    """普通通知应保留标题、正文、图片和详情链接并检查 errcode。"""
    client = DingTalk(
        DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=token"
    )
    response = _response({"errcode": 0, "errmsg": "ok"})
    client._req = Mock()
    client._req.post_res.return_value = response

    assert client.send_msg(
        title="整理完成",
        text="电影已入库",
        image="https://example.com/poster.jpg",
        link="https://example.com/history",
    ) is True

    request = client._req.post_res.call_args.kwargs
    assert request["url"].endswith("access_token=token")
    assert request["json"]["msgtype"] == "markdown"
    assert request["json"]["markdown"]["title"] == "整理完成"
    markdown = request["json"]["markdown"]["text"]
    assert "### 整理完成" in markdown
    assert "电影已入库" in markdown
    assert "![图片](https://example.com/poster.jpg)" in markdown
    assert "[查看详情](https://example.com/history)" in markdown
    response.close.assert_called_once_with()


def test_send_msg_rejects_http_and_business_failures() -> None:
    """HTTP 失败或钉钉业务错误都不能被误判为发送成功。"""
    client = DingTalk(
        DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=token"
    )
    client._req = Mock()
    client._req.post_res.side_effect = [
        _response({}, status_code=500),
        _response({"errcode": 310000, "errmsg": "keywords not in content"}),
    ]

    assert client.send_msg(title="测试") is False
    assert client.send_msg(title="测试") is False


def test_module_routes_matching_notifications_to_dingtalk_client(monkeypatch) -> None:
    """模块应沿通知来源和消息类型契约向对应钉钉实例发送。"""
    module = DingTalkModule()
    module._channel = NotificationChannel.DingTalk
    client = Mock()
    config = SimpleNamespace(name="家庭群", switchs=[MessageType.Organize.value])
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_config", lambda name=None: config)
    monkeypatch.setattr(module, "get_instance", lambda name=None: client)

    module.post_message(
        Message(
            channel=NotificationChannel.DingTalk,
            source="家庭群",
            mtype=MessageType.Organize,
            title="整理完成",
            text="测试内容",
        )
    )

    client.send_msg.assert_called_once()


def test_module_skips_unsupported_command_registration(monkeypatch) -> None:
    """钉钉机器人不支持命令 API 时应跳过基类命令注册流程。"""
    module = DingTalkModule()
    config = SimpleNamespace(name="家庭群", config={})
    get_instance = Mock()
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_instance", get_instance)

    module.register_commands({"/version": {"description": "当前版本"}})

    get_instance.assert_not_called()


def test_dingtalk_channel_declares_markdown_image_and_link_capabilities() -> None:
    """能力表应允许通用消息层保留钉钉支持的富文本字段。"""
    for capability in (
        ChannelCapability.MARKDOWN,
        ChannelCapability.IMAGES,
        ChannelCapability.LINKS,
    ):
        assert ChannelCapabilityManager.supports_capability(
            NotificationChannel.DingTalk, capability
        )
