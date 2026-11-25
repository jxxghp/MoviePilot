"""
渠道专用正文应独立渲染，同时保留普通渠道和历史的纯文本。
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.modules.dingtalk import DingTalkModule
from app.modules.discord import DiscordModule
from app.modules.feishu.feishu import Feishu
from app.modules.qqbot.module import QQBotModule
from app.modules.qqbot.qqbot import QQBot
from app.modules.slack import SlackModule
from app.modules.slack.slack import Slack
from app.modules.synologychat import SynologyChatModule
from app.modules.telegram.module import TelegramModule
from app.modules.vocechat import VoceChatModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas.message import Message, NotificationBlock, NotificationContent
from app.schemas.types import NotificationChannel


def _message() -> Message:
    """
    构造一份统一的语义内容，三个渠道由宿主分别渲染。
    """
    return Message(title="站点自动签到", text="原有完整正文", userid="target",
                   rich_content=NotificationContent(title="签到", blocks=[NotificationBlock(title="完成")]))


@pytest.mark.parametrize("module_class,channel", [(TelegramModule, "Telegram"), (SlackModule, "Slack")])
@pytest.mark.parametrize("method", ["post_message", "send_direct_message"])
def test_module_passes_only_own_rich_text(module_class, channel, method):
    """
    渲染依然经过既有配置、目标和开关，原 Message 不被修改。
    """
    module = module_class()
    result = {"success": True, "message_id": 1} if channel == "Telegram" else (True, {"ts": "1"})
    client = SimpleNamespace(send_msg=Mock(return_value=result))
    message = _message()
    with (patch.object(module, "get_configs", return_value={"test": SimpleNamespace(name="test")}),
          patch.object(module, "check_message", return_value=True),
          patch.object(module, "get_instance", return_value=client)):
        getattr(module, method)(message)
    kwargs = client.send_msg.call_args.kwargs
    key = "rich_message" if channel == "Telegram" else "formatted_text"
    assert kwargs[key] == message.channel_text(NotificationChannel(channel))
    assert kwargs["text"] == "原有完整正文"
    assert kwargs["userid"] == "target"
    assert message.text == "原有完整正文"
    assert Message.model_validate_json(message.model_dump_json()).rich_content == message.rich_content


@pytest.mark.parametrize("module_class", [TelegramModule, SlackModule])
def test_rich_text_does_not_bypass_notification_switch(module_class):
    """
    关闭渠道时，专用正文不能绕过 check_message 执行真实发送。
    """
    module = module_class()
    with (patch.object(module, "get_configs", return_value={"test": SimpleNamespace(name="test")}),
          patch.object(module, "check_message", return_value=False),
          patch.object(module, "get_instance") as get_client):
        module.post_message(_message())
    get_client.assert_not_called()


@pytest.mark.parametrize("rich", [True, False])
def test_feishu_uses_card_markdown_and_keeps_original(rich):
    """
    飞书交互卡片选择自身正文，未提供时与原来的发送结构一致。
    """
    client = object.__new__(Feishu)
    client._user_chat_mapping = {}
    client._default_chat_id = "chat"
    message = _message()
    if not rich:
        message.rich_content = None
    with (patch.object(client, "_upload_remote_image", return_value=None),
          patch.object(client, "_send_with_fallback_broadcast", return_value={"success": True}) as send):
        client.send_notification(message)
    assert send.call_args.args[0] == "interactive"
    elements = send.call_args.args[1]["body"]["elements"]
    assert elements[1]["content"] == ("**完成**" if rich else "原有完整正文")
    assert message.text == "原有完整正文"


def test_slack_block_kit_preserves_accessibility_fallback_and_detail_link():
    """
    Slack 使用原文摘要、原生标题及分段正文，保留查看详情按钮。
    """
    client = object.__new__(Slack)
    client._client = SimpleNamespace(chat_postMessage=Mock(return_value={"ok": True}))
    ok, _ = client.send_msg("签到结果", text="纯文本", formatted_text="*统计*\n\n站点详情",
                            userid="target", link="https://example.com/details")
    assert ok
    kwargs = client._client.chat_postMessage.call_args.kwargs
    assert kwargs["text"] == "签到结果\n纯文本"
    blocks = kwargs["blocks"]
    assert blocks[0]["type"] == "header"
    assert [block["text"]["text"] for block in blocks[1:3]] == ["*统计*", "站点详情"]
    assert blocks[-1]["type"] == "actions"


@pytest.mark.parametrize("text", ["x" * 3001])
def test_slack_oversized_rich_text_falls_back_to_existing_notification(text):
    """
    超出 Block Kit 限制时用完整旧通知路径，避免 invalid_blocks。
    """
    assert Slack._formatted_blocks(text) == []


def test_slack_long_report_sends_every_site_in_bounded_messages():
    """
    超过单条 Block Kit 容量时分段，每条保留标题和详情按钮。
    """
    client = object.__new__(Slack)
    client._client = SimpleNamespace(chat_postMessage=Mock(return_value={"ok": True}))
    paragraphs = [f"*站点{index}*\n签到成功" for index in range(130)]
    ok, _ = client.send_msg("签到结果", text="完整纯文本", formatted_text="\n\n".join(paragraphs),
                            userid="target", link="https://example.com/details")
    assert ok
    assert client._client.chat_postMessage.call_count == 3
    sent = []
    for call in client._client.chat_postMessage.call_args_list:
        assert call.kwargs["channel"] == "target"
        blocks = call.kwargs["blocks"]
        assert len(blocks) <= 50
        assert blocks[0]["type"] == "header"
        assert blocks[-1]["type"] == "actions"
        sent.extend(block["text"]["text"] for block in blocks if block["type"] == "section")
    assert sent == paragraphs


def test_slack_conversation_context_does_not_change_new_notification_target():
    """
    没有编辑消息 ID 时，历史会话上下文不能覆盖原有通知收件人。
    """
    client = object.__new__(Slack)
    client._client = SimpleNamespace(chat_postMessage=Mock(return_value={"ok": True}))
    ok, _ = client.send_msg("签到结果", text="纯文本", formatted_text="*成功*", userid="target",
                            original_chat_id="old-chat")
    assert ok
    assert client._client.chat_postMessage.call_args.kwargs["channel"] == "target"


def test_feishu_long_report_sends_every_site_in_bounded_cards():
    """
    飞书按实际卡片字节预算分段，所有站点按顺序保留且目标不变。
    """
    import json

    client = object.__new__(Feishu)
    client._user_chat_mapping = {}
    client._default_chat_id = "chat"
    message = _message()
    message.link = "https://example.com/details"
    message.rich_content = NotificationContent(blocks=[NotificationBlock(title=f"站点{index}",
        text="签到成功，奖励魔力值" * 50) for index in range(70)])
    expected = message.channel_text(NotificationChannel.Feishu)
    with (patch.object(client, "_upload_remote_image", return_value=None),
          patch.object(client, "_send_with_fallback_broadcast", return_value={"success": True}) as send):
        client.send_notification(message, userid="target")
    assert send.call_count > 1
    bodies = []
    for call in send.call_args_list:
        assert call.kwargs["userid"] == "target"
        card = call.args[1]
        assert len(json.dumps(card, ensure_ascii=False).encode()) < 25000
        bodies.append(card["body"]["elements"][1]["content"].split("\n\n[查看详情]")[0])
    assert "\n\n".join(bodies) == expected


def test_plain_message_default_is_unchanged():
    """
    无专用正文时，所有普通消费者仍获取原 text。
    """
    message = Message(text="纯文本", channel=NotificationChannel.Wechat)
    assert message.rich_content is None
    assert message.text == "纯文本"


@pytest.mark.parametrize("module_class,channel", [
    (DingTalkModule, NotificationChannel.DingTalk), (DiscordModule, NotificationChannel.Discord),
    (VoceChatModule, NotificationChannel.VoceChat), (SynologyChatModule, NotificationChannel.SynologyChat),
])
@pytest.mark.parametrize("rich", [True, False])
def test_additional_channels_select_body_and_keep_every_paragraph(module_class, channel, rich):
    """
    所有已支持格式化的渠道接入独立正文，长报告分段但不遗漏站点。
    """
    module = module_class()
    client = SimpleNamespace(send_msg=Mock(return_value=True))
    message = _message()
    message.rich_content = NotificationContent(blocks=[NotificationBlock(title=f"站点{index}",
        text="签到成功" + "奖励" * 50) for index in range(80)]) if rich else None
    expected = message.channel_text(channel) if rich else message.text
    with (patch.object(module, "get_configs", return_value={"test": SimpleNamespace(name="test", type="test", enabled=True)}),
          patch.object(module, "check_message", return_value=True),
          patch.object(module, "get_instance", return_value=client)):
        module.post_message(message)
    sent = "\n\n".join(call.kwargs["text"] for call in client.send_msg.call_args_list)
    assert sent == expected
    if rich:
        assert all(f"站点{index}" in sent for index in range(80))
    assert message.text == "原有完整正文"
    assert all(call.kwargs["userid"] == "target" for call in client.send_msg.call_args_list)


@pytest.mark.parametrize("module_class", [DingTalkModule, DiscordModule, VoceChatModule, SynologyChatModule])
def test_missing_notification_instance_does_not_send(module_class):
    """
    配置存在但客户端未就绪时，不运行分段循环或使用上一配置的正文。
    """
    module = module_class()
    with (patch.object(module, "get_configs", return_value={
        "test": SimpleNamespace(name="test", type="test", enabled=True)}),
          patch.object(module, "check_message", return_value=True),
          patch.object(module, "get_instance", return_value=None)):
        assert module.post_message(_message()) is None


def test_discord_attachment_does_not_enter_rich_text_loop():
    """
    附件通知沿用原文件发送分支，结构化正文不会产生第二条消息。
    """
    module = DiscordModule()
    client = SimpleNamespace(send_file=Mock(return_value=True), send_msg=Mock())
    message = _message()
    message.file_path = "/tmp/test-only-placeholder.txt"
    with (patch.object(module, "get_configs", return_value={
        "test": SimpleNamespace(name="test", type="test", enabled=True)}),
          patch.object(module, "check_message", return_value=True),
          patch.object(module, "get_instance", return_value=client)):
        module.post_message(message)
    client.send_file.assert_called_once()
    client.send_msg.assert_not_called()


def test_wechat_bot_and_application_mode_remain_independent():
    """
    同轮包含机器人和应用配置时，富文本不能流入应用纯文本或原 Message。
    """
    module = WechatModule()
    bot = object.__new__(WeChatBot)
    bot.send_msg = Mock()
    application = SimpleNamespace(send_msg=Mock())
    message = _message()
    message.rich_content = NotificationContent(blocks=[NotificationBlock(title="富文本结果")])
    with (patch.object(module, "get_configs", return_value={
        "bot": SimpleNamespace(name="bot", config={"WECHAT_MODE": "bot"}),
        "app": SimpleNamespace(name="app", config={"WECHAT_MODE": "app"})}),
          patch.object(module, "check_message", return_value=True),
          patch.object(module, "get_instance", side_effect=[bot, application])):
        module.post_message(message)
    assert bot.send_msg.call_args.kwargs["text"] == "**富文本结果**"
    assert application.send_msg.call_args.kwargs["text"] == "原有完整正文"
    assert message.text == "原有完整正文"


@pytest.mark.parametrize("markdown_available", [True, False])
def test_qq_keeps_rich_text_and_falls_back_original_once(markdown_available):
    """
    QQ 有权限时发送全部分段，无权限时仅回退一次完整旧正文。
    """
    client = object.__new__(QQBot)
    client._ready = True
    client._app_id = "id"
    client._app_secret = "test"
    parts = ["**站点一**\n完成", "**站点二**\n完成"]
    with (patch.object(client, "_get_target", return_value=("target", False)),
          patch("app.modules.qqbot.qqbot.get_access_token", return_value="token"),
          patch("app.modules.qqbot.qqbot.send_proactive_c2c_message") as send):
        if not markdown_available:
            send.side_effect = [RuntimeError("11244 markdown 权限不足"), None]
        assert client.send_msg("签到", text="原有全部站点正文", formatted_texts=parts)
    assert send.call_count == 2
    if markdown_available:
        assert "**站点一**" in send.call_args_list[0].args[2]
        assert "**站点二**" in send.call_args_list[1].args[2]
        assert all(call.kwargs["use_markdown"] for call in send.call_args_list)
    else:
        assert send.call_args_list[1].args[2] == "【签到】\n原有全部站点正文"
        assert send.call_args_list[1].kwargs["use_markdown"] is False


def test_qq_module_supplies_chunks_and_preserves_plain_fallback():
    """
    QQ 模块为客户端提供富文本分段，同时保留原 text 用于权限回退。
    """
    module = QQBotModule()
    client = SimpleNamespace(send_msg=Mock())
    message = _message()
    message.rich_content = NotificationContent(blocks=[NotificationBlock(title="站点结果")])
    with (patch.object(module, "get_configs", return_value={"test": SimpleNamespace(name="test")}),
          patch.object(module, "check_message", return_value=True),
          patch.object(module, "get_instance", return_value=client)):
        module.post_message(message)
    assert client.send_msg.call_args.kwargs["formatted_texts"] == ["**站点结果**"]
    assert client.send_msg.call_args.kwargs["text"] == "原有完整正文"


def test_channel_notification_format_obeys_capabilities_and_instance_mode(monkeypatch):
    """
    能力配置决定正文选择，禁用能力以及企业微信应用模式均使用旧正文。
    """
    from dataclasses import replace

    from app.schemas.notification import ChannelCapabilityManager, NotificationTextFormat

    manager = ChannelCapabilityManager
    message = _message()
    message.rich_content = NotificationContent(blocks=[NotificationBlock(title="机器人正文")])
    assert manager.get_notification_text_format(NotificationChannel.Wechat, {}) is None
    assert message.channel_text(NotificationChannel.Wechat, {"WECHAT_MODE": "bot"}) == "**机器人正文**"
    capabilities = manager.get_capabilities(NotificationChannel.Slack)
    monkeypatch.setitem(manager._capabilities, NotificationChannel.Slack,
                        replace(capabilities, notification_text_format=None))
    assert message.channel_text(NotificationChannel.Slack) is None
    assert message.channel_text_chunks(NotificationChannel.Slack, 3000) == []
    assert manager.get_notification_text_format(NotificationChannel.WebPush) is None
    assert manager.get_notification_text_format(NotificationChannel.WechatClawBot) is None
    assert manager.get_notification_text_format(NotificationChannel.DingTalk) == NotificationTextFormat.MARKDOWN


def test_telegram_channel_summary_includes_detail_link_without_changing_legacy_rich_message():
    """
    详情链接经过真实 Markdown 转换仍是链接，原有 Rich Message 不被添加内容。
    """
    from app.modules.telegram.telegram import Telegram

    message = _message()
    message.link = 'https://example.com/?a=1&b="value"'
    rich = TelegramModule._notification_rich_message(message)
    converted = "".join(chunk["html"] for chunk in Telegram._build_rich_message_chunks(rich))
    assert '<a href="https://example.com/?a=1&amp;b=%22value%22">查看详情</a>' in converted
    assert "&lt;a href" not in converted
    message.rich_content = None
    message.rich_message = "原有完整 Rich Message"
    assert TelegramModule._notification_rich_message(message) == message.rich_message


def test_telegram_notification_sections_have_native_list_indentation():
    """
    真实转换产物应包含分节标题和缩进列表，而非全是同级普通段落。
    """
    from app.modules.telegram.telegram import Telegram

    message = Message(rich_content=NotificationContent(title="签到结果", blocks=[
        NotificationBlock(title="统计", text="本次 47"),
        NotificationBlock(title="失败 · 9"),
        NotificationBlock(title="Cookie已失效", text="站点一、站点二"),
        NotificationBlock(title="网络失败", text="站点三"),
        NotificationBlock(title="成功 · 28"),
        NotificationBlock(title="签到成功", text="站点四、站点五"),
    ]))
    body = "".join(chunk["html"] for chunk in Telegram._build_rich_message_chunks(
        TelegramModule._notification_rich_message(message)))
    assert "<h3>失败 · 9</h3>" in body
    assert "<h3>成功 · 28</h3>" in body
    assert body.count("<li>") == 3
    assert "<b>Cookie已失效</b><br/>站点一、站点二" in body
    assert "<b>签到成功</b><br/>站点四、站点五" in body


def test_telegram_long_section_list_splits_without_losing_items():
    """
    原因很多时由真实转换器拆分长列表，各组和对应名单仍完整保留。
    """
    from app.modules.telegram.telegram import Telegram

    blocks = [NotificationBlock(title="访问失败 · 150")]
    blocks.extend(NotificationBlock(title=f"失败原因{index}：" + "网络连接失败" * 20,
                                    text=f"完整站点{index:03d}") for index in range(150))
    message = Message(rich_content=NotificationContent(title="签到结果", blocks=blocks))
    chunks = Telegram._build_rich_message_chunks(TelegramModule._notification_rich_message(message))
    assert len(chunks) > 1
    body = "".join(chunk["html"] for chunk in chunks)
    assert all(body.count(f"完整站点{index:03d}") == 1 for index in range(150))


def test_host_renders_raw_data_safely_for_every_declared_format():
    """
    同一份结构化数据按能力生成语法，站点文本不能注入格式、链接或提醒。
    """
    from app.schemas.notification import NotificationTextFormat

    content = NotificationContent(title="站点自动签到", blocks=[NotificationBlock(
        title="站*点_", text="<!channel> [链接](https://bad.example) **成功**")])
    for text_format in NotificationTextFormat:
        text = content.render(text_format)
        assert "<!channel>" not in text
        assert "\n# " not in text
    assert "*站＊点＿*" in content.render(NotificationTextFormat.MRKDWN)
    assert "&#42;" in content.render(NotificationTextFormat.CARD_MARKDOWN)


def test_host_telegram_native_conversion_preserves_literal_content():
    """
    宿主的 Rich Markdown 转换保留特殊字符原意，不产生注入链接。
    """
    from telegramify_markdown import richify

    from app.schemas.notification import NotificationTextFormat

    content = NotificationContent(title="站点自动签到", blocks=[NotificationBlock(
        title="站*点_", text="<!channel> [链接](https://bad.example)")])
    converted = richify(content.render(NotificationTextFormat.RICH_MARKDOWN), mode="html").html
    assert "<b>站*点_</b>" in converted
    assert "&lt;!channel&gt;" in converted
    assert '<a href="https://bad.example"' not in converted
    assert "<h2>站点自动签到</h2>" in converted
