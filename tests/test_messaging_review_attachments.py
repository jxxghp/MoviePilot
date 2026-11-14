"""
通过真实网关回调、附件密码协议及交互入口验证消息修复，隔离所有外部发送。
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from lxml import etree

from app.agent.skills.registry import SkillInfo
from app.application.messaging.media import media_interaction_manager
from app.application.messaging.skill import PendingSkillInteraction, SkillInteractionHandler, skill_interaction_manager
from app.chain.interaction import MediaInteractionChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.modules.qqbot import gateway
from app.modules.telegram.telegram import Telegram
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot.wechatclawbot import ILinkClient, WechatClawBot
from app.schemas.types import NotificationChannel


@pytest.mark.parametrize("event", ["C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"])
@pytest.mark.parametrize("text", ["", "Attached review"])
def test_qq_gateway_preserves_attachments(monkeypatch, event, text):
    """
    两种网关事件均转交纯附件及带文字附件，实际回调处理保留原附件字段。
    """
    stop = threading.Event()
    attachments = [{"url": "https://files.invalid/review.pdf", "content_type": "application/pdf"}]
    received = []
    message = {"op": 0, "t": event, "d": {"id": "M1", "content": text,
        "author": {"user_openid": "U1", "member_openid": "U1"}, "group_openid": "G1",
        "attachments": attachments}}

    def websocket(_url, **callbacks):
        """
        仅把固定事件交回真实网关处理函数，不创建网络连接。
        """
        def receive(**_kwargs):
            """
            将固定附件事件交给网关回调并结束隔离的接收循环。
            """
            callbacks["on_message"](None, json.dumps(message))
            stop.set()
        return SimpleNamespace(run_forever=receive)

    monkeypatch.setattr(gateway.websocket, "WebSocketApp", websocket)
    gateway.run_gateway("id", "secret", "review", lambda *_args: "token",
                        lambda _token: "wss://gateway.invalid", received.append, stop, [])
    assert received[0]["attachments"] == attachments
    assert received[0]["content"] == text


def test_wechat_bot_gateway_forwards_supported_file(monkeypatch):
    """
    企业微信支持的文件报文经过真实网关过滤仍能进入统一入口。
    """
    client = object.__new__(WeChatBot)
    client._config_name = "review"
    client._admins = []
    forwarded = Mock()
    monkeypatch.setattr(client, "_remember_target", Mock())
    monkeypatch.setattr(client, "_forward_to_message_chain", forwarded)
    payload = {"body": {"from": {"userid": "U1"}, "msgtype": "file", "chattype": "single",
        "file": {"download_url": "https://files.invalid/review.pdf", "name": "review.pdf"}}}
    client._handle_callback_message(payload)
    forwarded.assert_called_once_with(payload)


@pytest.mark.parametrize("content,mime", [(b"%PDF-1.7 review", "application/pdf"),
    (b"plain review text", "text/plain"), (b"\x89PNG\r\n\x1a\nreview", "image/png")])
def test_clawbot_roundtrips_protocol_cipher_without_file_header_guess(content, mime):
    """
    真实发送端协议生成的密文，接收端必须完整解密并去填充，未知文件头也支持。
    """
    key = bytes(range(16))
    ciphertext = ILinkClient._encrypt_aes_ecb(content, key)
    assert WechatClawBot._decrypt_if_needed(ciphertext, ILinkClient._encode_media_aes_key(key), mime) == content


def test_clawbot_invalid_cipher_is_not_returned_as_plaintext():
    """
    已声明密钥但长度无效的密文必须拒绝，不能当作合法附件返回。
    """
    with pytest.raises(ValueError):
        WechatClawBot._decrypt_if_needed(b"broken", bytes(range(16)).hex(), "application/pdf")


@pytest.mark.parametrize("limit", [1024, 4096])
def test_telegram_long_html_remains_valid_and_complete(limit):
    """
    跨长文本边界的嵌套标签与实体必须合法配平且不丢失换行。
    """
    client = object.__new__(Telegram)
    client._bot = Mock()
    body = "😀&amp;正文\n" * 1000
    html = '<b><a href="https://example.invalid/?a=1&amp;b=2">' + body + '</a></b>'
    client._Telegram__send_long_plain_message(image=None, caption=html, caption_limit=limit,
                                             chat_id="U1", parse_mode="HTML")
    texts = []
    for call in client._api.send_message.call_args_list:
        root = etree.fromstring(("<root>" + call.kwargs["text"] + "</root>").encode())
        text = "".join(root.itertext())
        assert len(text.encode("utf-16-le")) // 2 <= limit
        texts.append(text)
    assert "".join(texts) == ("😀&正文\n" * 1000)


@pytest.mark.parametrize("market", [False, True])
def test_skill_actions_bind_displayed_item_identity(monkeypatch, market):
    """
    列表显示后删除首项，再按原序号操作第二项，目标仍是展示时的技能。
    """
    skills = [SkillInfo(id=key, name=key, description="review", removable=True) for key in ("A", "B", "C")]
    catalog = SimpleNamespace(list_local_skills=lambda: list(skills), list_market_skills=lambda **_kw: list(skills),
        remove_local_skill=Mock(return_value=(True, "removed")), install_market_skill=Mock(return_value=(True, "installed")))
    handler = SkillInteractionHandler(messenger=Mock(), skill_catalog=catalog)
    request = PendingSkillInteraction("request", "U1", NotificationChannel.Telegram, "review", "user")
    if market:
        handler._build_market_view(request)
    else:
        handler._build_installed_view(request)
    skills.pop(0)
    if market:
        assert handler._install_market_skill(request, 2)[0]
        assert catalog.install_market_skill.call_args.args[0].id == "B"
    else:
        assert handler._remove_local_skill(request, 2)[0]
        catalog.remove_local_skill.assert_called_once_with("B")


def test_stale_media_button_cannot_select_torrent_as_media(monkeypatch):
    """
    已进入资源阶段的旧媒体按钮不得读取新列表或覆盖已选媒体身份。
    """
    request = media_interaction_manager.create_or_replace("U1", NotificationChannel.Telegram, "review", "user",
        action="Search", keyword="Review", items=[])
    request.phase = "torrent"
    selected = MediaInfo(title="Selected")
    request.current_media = selected
    request.items = [Context(torrent_info=TorrentInfo(title="Resource"))]
    chain = object.__new__(MediaInteractionChain)
    monkeypatch.setattr(chain, "post_message", Mock())
    search = Mock()
    monkeypatch.setattr(chain, "_search_media_resources", search)
    try:
        assert chain.handle_callback_interaction(f"media:{request.request_id}:select:1",
            NotificationChannel.Telegram, "review", "U1", "user")
        assert request.current_media is selected
        search.assert_not_called()
    finally:
        media_interaction_manager.clear()


@pytest.mark.parametrize("encrypt_type,key", [(1, None), (2, bytes(range(16)).hex())])
def test_clawbot_declared_encryption_requires_supported_protocol(monkeypatch, encrypt_type, key):
    """
    已声明加密但缺密钥或模式不支持时，真实附件下载入口必须拒绝返回内容。
    """
    client = object.__new__(WechatClawBot)
    payload = {"url": "https://files.invalid/encrypted", "aeskey": key, "encrypt_type": encrypt_type}
    monkeypatch.setattr(client, "_decode_ref_payload", lambda **_kw: payload)
    ciphertext = ILinkClient._encrypt_aes_ecb(b"%PDF review", bytes(range(16)))
    monkeypatch.setattr("app.modules.wechatclawbot.wechatclawbot.RequestUtils", lambda **_kw: SimpleNamespace(
        get_res=lambda _url: SimpleNamespace(content=ciphertext, headers={})))
    assert client._download_ref_bytes("wxclaw://file/review", "file") is None


def test_skill_old_render_button_cannot_operate_new_page():
    """
    同一会话刷新列表后，旧按钮携带的展示标识必须过期。
    """
    catalog = SimpleNamespace(list_local_skills=lambda: [
        SkillInfo(id="A", name="A", description="review", removable=True)], remove_local_skill=Mock())
    handler = SkillInteractionHandler(messenger=Mock(), skill_catalog=catalog)
    request = skill_interaction_manager.create_or_replace("U1", NotificationChannel.Telegram, "review", "user")
    request.view = "installed"
    try:
        _, _, buttons = handler._build_installed_view(request)
        callback = next(button["callback_data"] for row in buttons for button in row if button["text"] == "删除 1")
        assert len(callback.encode()) <= 64
        handler._build_installed_view(request)
        assert handler.handle_callback_interaction(callback, NotificationChannel.Telegram, "review", "U1", "user")
        catalog.remove_local_skill.assert_not_called()
    finally:
        skill_interaction_manager.clear()
