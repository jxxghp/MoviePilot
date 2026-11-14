"""
独立复核消息修复的真实回调、附件解密和图片说明分片边界。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from Crypto.Cipher import AES
from lxml import etree

from app.agent.skills.registry import SkillInfo
from app.application.messaging.skill import SkillInteractionHandler, skill_interaction_manager
from app.modules.telegram.telegram import Telegram
from app.modules.wechatclawbot.wechatclawbot import ILinkClient, WechatClawBot
from app.schemas.types import NotificationChannel


@pytest.mark.parametrize("plaintext", [b"x" * 15 + b"\x00", b"x" * 14 + b"\x01\x02"])
def test_claw_attachment_rejects_full_length_cipher_with_invalid_padding(monkeypatch, plaintext):
    """
    长度合法但PKCS7尾部非法的密文不得从真实资源引用入口返回。
    """
    key = bytes(range(16))
    client = object.__new__(WechatClawBot)
    ref = ILinkClient._encode_ref_payload("file", {
        "url": "https://files.invalid/review", "aeskey": ILinkClient._encode_media_aes_key(key),
        "encrypt_type": 1, "name": "review.bin",
    })
    ciphertext = AES.new(key, AES.MODE_ECB).encrypt(plaintext)
    monkeypatch.setattr("app.modules.wechatclawbot.wechatclawbot.RequestUtils", lambda **_kwargs: SimpleNamespace(
        get_res=lambda _url: SimpleNamespace(content=ciphertext, headers={}),
    ))
    assert client._download_ref_bytes(ref, "file") is None


@pytest.mark.parametrize("encrypted", [False, True])
def test_claw_attachment_real_reference_preserves_complete_content_and_metadata(monkeypatch, encrypted):
    """
    真实内部引用同时支持未加密文件和协议密文，并完整保留名称及MIME。
    """
    key = bytes(range(16))
    content = bytes(range(64)) + b"\x00\x01\x02"
    client = object.__new__(WechatClawBot)
    ref = ILinkClient._encode_ref_payload("file", {
        "url": "https://files.invalid/review", "name": "review.bin", "mime_type": "application/custom",
        "aeskey": ILinkClient._encode_media_aes_key(key) if encrypted else None,
        "encrypt_type": 1 if encrypted else None,
    })
    downloaded = ILinkClient._encrypt_aes_ecb(content, key) if encrypted else content
    monkeypatch.setattr("app.modules.wechatclawbot.wechatclawbot.RequestUtils", lambda **_kwargs: SimpleNamespace(
        get_res=lambda _url: SimpleNamespace(content=downloaded, headers={}),
    ))
    assert client._download_ref_bytes(ref, "file") == (content, "application/custom", "review.bin")


@pytest.mark.parametrize("market", [False, True])
def test_skill_real_callback_keeps_displayed_identity_after_reordering(market):
    """
    合法展示按钮经过公开回调入口仍定位原第二项，其他用户不能复用该按钮。
    """
    skills = [SkillInfo(id=name, name=name, description="复核技能", removable=True) for name in ("A", "B", "C")]
    catalog = SimpleNamespace(list_local_skills=lambda: list(skills),
        list_market_skills=lambda **_kwargs: list(skills),
        remove_local_skill=Mock(return_value=(True, "已删除")),
        install_market_skill=Mock(return_value=(True, "已安装")))
    handler = SkillInteractionHandler(messenger=Mock(), skill_catalog=catalog)
    request = skill_interaction_manager.create_or_replace("review-owner", NotificationChannel.Telegram,
                                                          "review-source", "review")
    request.view = "market" if market else "installed"
    action = "install" if market else "remove"
    try:
        if market:
            _, _, buttons = handler._build_market_view(request)
        else:
            _, _, buttons = handler._build_installed_view(request)
        callback = next(button["callback_data"] for row in buttons for button in row
                        if f":{action}:2:" in button.get("callback_data", ""))
        assert handler.handle_callback_interaction(callback, NotificationChannel.Telegram,
                                                    "review-source", "other-user", "other")
        catalog.install_market_skill.assert_not_called()
        catalog.remove_local_skill.assert_not_called()
        skills.pop(0)
        assert handler.handle_callback_interaction(callback, NotificationChannel.Telegram,
                                                    "review-source", "review-owner", "review")
        if market:
            selected = catalog.install_market_skill.call_args.args[0]
            assert selected.id == "B" and selected is not skills[0]
        else:
            catalog.remove_local_skill.assert_called_once_with("B")
    finally:
        skill_interaction_manager.clear()


def test_telegram_photo_caption_chunks_preserve_numeric_entities_and_first_keyboard():
    """
    图片首块与后续文本保持合法HTML和UTF16上限，键盘只附在首条。
    """
    client = object.__new__(Telegram)
    client._bot = Mock()
    body = "&#x1F600;&amp;&lt;正文\n" * 500
    markup = {"inline_keyboard": [[{"text": "复核", "callback_data": "review"}]]}
    client._Telegram__send_long_plain_message(image=b"image", caption=f"<b><i>{body}</i></b>",
        caption_limit=1024, chat_id="review", parse_mode="HTML", reply_markup=markup)
    photo = client._api.send_photo.call_args.kwargs
    messages = [call.kwargs for call in client._api.send_message.call_args_list]
    assert messages and photo["reply_markup"] == markup
    assert all(message["reply_markup"] is None for message in messages)
    restored = []
    for chunk in [photo["caption"], *(message["text"] for message in messages)]:
        root = etree.fromstring(("<root>" + chunk + "</root>").encode())
        visible = "".join(root.itertext())
        assert len(visible.encode("utf-16-le")) // 2 <= 1024
        restored.append(visible)
    assert "".join(restored) == "😀&<正文\n" * 500
