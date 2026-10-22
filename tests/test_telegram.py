# -*- coding: utf-8 -*-
"""
Telegram 模块单元测试（pytest 原生）。
"""
import json
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.agent.callback import StreamingHandler
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.modules.telegram.botapi import TelegramApiError, TelegramNetworkError
from app.modules.telegram.module import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.schemas.message import Message
from app.schemas.types import MediaType, NotificationChannel


@pytest.fixture
def telegram():
    """
    构造 Bot API 客户端与 ImageHelper 均已打桩的 Telegram 实例。

    空 token 会让 Telegram.__init__ 提前返回、致 send_* 抛错，故用假 bot 让初始化完整、
    消息发送走内存桩；ImageHelper 打桩避免 send_medias/send_msg 按 poster_path 真实下载海报
    （否则对 raw.githubusercontent.com 等外链发起真实 HTTP，外部 IO 不可接受且拖慢用例）。
    with 上下文在 fixture 结束时自动停桩，即使实例化失败也不泄漏 patch。
    """
    with patch("app.modules.telegram.telegram.TelegramBotApi") as mock_telebot_cls, \
            patch("app.modules.telegram.telegram.ImageHelper") as mock_image_cls:
        bot_instance = MagicMock()
        # get_me 用于初始化 bot 用户名，需返回带 username 的对象
        bot_instance.get_me.return_value = {"username": "test_bot"}
        # polling/stop 使用普通函数，避免后台线程执行 MagicMock 时在退出阶段产生锁竞争。
        bot_instance.polling = lambda *args, **kwargs: None
        bot_instance.stop_polling = lambda *args, **kwargs: True
        mock_telebot_cls.return_value = bot_instance
        mock_image_cls.return_value.fetch_image.return_value = b"fake-image-bytes"
        telegram = Telegram(TELEGRAM_TOKEN="fake_token", TELEGRAM_CHAT_ID="fake_chat_id")
        yield telegram
        telegram.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_answer, trailing_tool", [(False, False), (True, False), (True, True)])
async def test_agent_final_edit_waits_during_telegram_edit_interval(
    telegram, monkeypatch, partial_answer, trailing_tool,
):
    """
    真实 Telegram 两秒编辑间隔内收尾时，应等待并把完整答案编辑回原消息。
    """
    now = [100.0]
    waits = []
    monkeypatch.setattr("app.modules.telegram.telegram.time.monotonic", lambda: now[0])

    def advance(seconds):
        """
        记录并推进 Telegram 编辑等待，保持用例离线且无需真实延时。

        :param seconds: 等待秒数
        """
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("app.modules.telegram.telegram.time.sleep", advance)
    telegram.bot.send_rich_message.return_value = {
        "message_id": 101, "chat": {"id": "fake_chat_id"},
    }
    handler = StreamingHandler()
    handler._channel = NotificationChannel.Telegram.value
    handler._source = "bot"
    handler._streaming_enabled = True
    handler._original_message_id = "question"
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    monkeypatch.setattr(module, "get_configs", lambda: {"bot": SimpleNamespace(name="bot")})
    monkeypatch.setattr(module, "get_instance", lambda _name: telegram)
    monkeypatch.setattr(module, "check_message", lambda *_args: True)
    monkeypatch.setattr(module, "get_notification_targets", lambda _message: None)

    async def dispatch(func, *args, **kwargs):
        """
        保留真实 Telegram 发送及限频逻辑，只隔离宿主模块路由和远端 Bot API。
        """
        if func.__name__ == "send_direct_message":
            return module.send_direct_message(args[0])
        if func.__name__ == "edit_message":
            return module.edit_message(**kwargs)
        return True

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", dispatch)
    handler.record_tool_call("execute_command", tool_kwargs={"command": "capture"})
    handler.thinking_started()
    await handler._flush_messages()
    handler.emit("| 剧名 | 简介 |\n| --- | --- |\n" + "| 示例 | 推荐理由 |\n" * 8)
    if partial_answer:
        now[0] += 2.0
        await handler._flush_messages()
    handler.emit("\n其中这些剧集评价最稳。\n\n要继续搜索吗？")
    if trailing_tool:
        handler.record_tool_call("read_file", tool_kwargs={"file_path": "result.txt"})
    handler.flush_pending_tool_summary()
    expected_text = handler._buffer

    all_sent, final_text = await handler.stop_streaming()

    assert all_sent
    assert waits == [2.0]
    assert await handler.take() == ""
    telegram.bot.send_rich_message.assert_called_once()
    assert telegram.bot.edit_message_text.call_count == (2 if partial_answer else 1)
    assert telegram.bot.edit_message_text.call_args.kwargs["message_id"] == 101
    rich_html = telegram.bot.edit_message_text.call_args.kwargs["rich_message"]["html"]
    assert "<table>" in rich_html
    assert rich_html.count("<td>推荐理由</td>") == 8
    assert "要继续搜索吗？" in rich_html
    assert final_text == expected_text
    assert "要继续搜索吗？" in final_text
    assert "思考中" not in final_text


@pytest.mark.parametrize("url", [None, "", "   ", 123, False])
@pytest.mark.parametrize("callback_data", [None, "", 123, "history"])
def test_inline_keyboard_filters_invalid_actions(url, callback_data):
    """
    无效链接仅在具有有效回调时降级，保留其他按钮及其行顺序。
    """
    button = {"text": "整理记录", "url": url, "callback_data": callback_data}
    valid_link = {"text": "打开", "url": "https://example.test/history", "callback_data": "ignored"}
    valid_callback = {"text": "返回", "callback_data": "back"}

    markup = Telegram._create_inline_keyboard([
        [button], [], [valid_link, {"text": "无动作"}, valid_callback],
    ])

    expected = [[{"text": "打开", "url": "https://example.test/history"}, valid_callback]]
    if callback_data == "history":
        expected.insert(0, [{"text": "整理记录", "callback_data": "history"}])
    assert markup == {"inline_keyboard": expected}
    assert button == {"text": "整理记录", "url": url, "callback_data": callback_data}


@pytest.mark.parametrize("poster_fails", [False, True])
def test_send_msg_ignores_null_url_button_with_photo_and_text_fallback(telegram, poster_fails):
    """
    空链接且无回调的按钮不阻断图片通知或图片失败后的文本兜底。
    """
    telegram.bot.send_photo.return_value = {"message_id": 101, "chat": {"id": "fake_chat_id"}}
    telegram.bot.send_message.return_value = {"message_id": 102, "chat": {"id": "fake_chat_id"}}
    if poster_fails:
        telegram.bot.send_photo.side_effect = TelegramApiError("sendPhoto", "Bad Request: wrong file", error_code=400)

    result = telegram.send_msg(
        title="整理完成", text="已入库两个文件", image="https://example.test/poster.jpg",
        buttons=[[{"text": "查看整理记录", "url": None}]],
    )

    assert result and result["success"]
    assert result["message_id"] == (102 if poster_fails else 101)
    telegram.bot.send_photo.assert_called_once()
    assert telegram.bot.send_photo.call_args.kwargs["reply_markup"] == {"inline_keyboard": []}
    if poster_fails:
        telegram.bot.send_message.assert_called_once()
        assert telegram.bot.send_message.call_args.kwargs["reply_markup"] == {"inline_keyboard": []}
    else:
        telegram.bot.send_message.assert_not_called()


def test_send_msg_success(telegram):
    """
    测试发送普通消息成功
    """
    # 调用send_msg方法
    result = telegram.send_msg(
        title="📥 开始下载\n唐朝诡事录 (2022)S03E31-E32",
        text="\n🕒 时间： 2025-11-21 18:14:51\n🎭 类别： 国产剧\n🌐 站点： 天空\n🌟 质量： WEB-DL 2160p\n💾 大小： 1.68G\n⚡️ 促销： 未知\n🚨 H&R： 否\n📛 名称： \nStrange Tales of Tang Dynasty S03E31-E32 2025 2160p WEB-DL DDP5.1 H265-Pure@HDSWEB [唐朝诡事录之长安3 / 唐朝诡事录3 / 唐朝诡事录 第三部 / 唐朝诡事录·长安 / 唐诡3 / Horror Stories of Tang Dynasty Ⅲ / Strange Legend of Tang Dynasty Ⅲ 第3季 第31-32集 | 主演: 杨旭文 杨志刚 郜思雯 [内封简繁英多国软字幕] 【去头尾广告纯享版】[非伪去头] *发现未去净的广告或片头片尾，奖励魔力1W]"
    )

    # 验证返回值：send_msg 失败时返回 {"success": False}（非空字典，仅 truthy 检查会漏判），故显式断言 success
    assert result and result.get("success")


def test_topic_id_is_scoped_to_channel_and_default_chat(tmp_path):
    """
    同一 bot 和群组的不同渠道应各发各的话题，私聊与空配置不带话题参数。
    """
    bots = [MagicMock(), MagicMock(), MagicMock()]
    for bot in bots:
        bot.get_me.return_value = {"username": "test_bot"}
        bot.polling = lambda *args, **kwargs: None
        bot.stop_polling = lambda *args, **kwargs: True
    with patch("app.modules.telegram.telegram.TelegramBotApi", side_effect=bots), patch(
        "app.modules.telegram.telegram.ImageHelper"
    ):
        clients = [
            Telegram(TELEGRAM_TOKEN="same-token", TELEGRAM_CHAT_ID="-1001", TELEGRAM_TOPIC_ID=topic)
            for topic in ("101", "202", "")
        ]
        try:
            for client, bot, topic in zip(clients, bots, (101, 202, None)):
                assert client.send_msg(title="", text="通知")["success"]
                assert bot.send_message.call_args.kwargs.get("message_thread_id") == topic
                assert client.send_msg(title="", text="私聊", userid="123")["success"]
                assert "message_thread_id" not in bot.send_message.call_args.kwargs

            assert clients[0].send_msg(title="", rich_message="# 通知")["success"]
            assert bots[0].send_rich_message.call_args.kwargs["message_thread_id"] == 101

            attachment = tmp_path / "notice.txt"
            attachment.write_text("通知")
            assert clients[0].send_file(file_path=str(attachment))["success"]
            assert bots[0].send_document.call_args.kwargs["message_thread_id"] == 101

            photo = tmp_path / "notice.jpg"
            photo.write_bytes(b"photo")
            assert clients[0].send_file(file_path=str(photo))["success"]
            assert bots[0].send_photo.call_args.kwargs["message_thread_id"] == 101

            voice = tmp_path / "notice.ogg"
            voice.write_bytes(b"voice")
            assert clients[0].send_voice(voice_path=str(voice))["success"]
            assert bots[0].send_voice.call_args.kwargs["message_thread_id"] == 101
        finally:
            for client in clients:
                client.stop()


def test_edit_msg_with_rich_message(telegram):
    """
    Telegram 流式编辑应继续使用 Rich Message 协议。
    """
    telegram._bot.edit_message_text.return_value = {"message_id": 101}

    result = telegram.edit_msg(
        chat_id="10001",
        message_id=101,
        text="# 旧回退",
        rich_message="# 流式结果\n\n- **完成**",
    )

    assert result is True
    kwargs = telegram._bot.edit_message_text.call_args.kwargs
    assert kwargs["text"] is None
    assert kwargs["rich_message"].get("html") == (
        '<h1>流式结果</h1><ul><li><b>完成</b></li></ul>'
    )


def test_telegram_parser_preserves_reply_to_message_id():
    """
    Telegram ForceReply 回复应保留来源消息和被回复消息的 message_id。
    """
    module = TelegramModule()
    client_config = SimpleNamespace(name="telegram-test", config={})
    client = SimpleNamespace(bot_username="mp_bot")
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 101,
            "from": {"id": 10001, "username": "tester"},
            "chat": {"id": 10001, "type": "private"},
            "text": "东张西望",
            "reply_to_message": {"message_id": 99, "text": "请输入节目关键词"},
        },
    }

    with patch.object(module, "get_config", return_value=client_config), patch.object(
        module, "get_instance", return_value=client
    ):
        message = module.message_parser(
            source="telegram-test",
            body=json.dumps(payload),
            form=None,
            args={},
        )

    assert message.text == "东张西望"
    assert message.message_id == 101
    assert message.chat_id == "10001"
    assert message.reply_to_message_id == 99


def test_send_msg_with_longtext(telegram):
    """
    测试发送长消息
    """
    result = telegram.send_msg(
        title="MoviePilot助手",
        text="好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？",
    )
    assert result and result.get("success")


def test_send_medias_msg_success(telegram):
    """
    测试发送媒体列表消息成功
    """
    # 创建模拟的媒体信息列表
    media1 = MediaInfo()
    media1.type = MediaType.MOVIE
    media1.title = "测试电影1"
    media1.year = "2023"
    media1.vote_average = 8.5
    media1.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"
    media1.tmdb_id=123123

    media2 = MediaInfo()
    media2.type = MediaType.TV
    media2.title = "测试电视剧1"
    media2.year = "2023"
    media2.vote_average = 9.0
    media2.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"

    medias = [media1, media2]

    result = telegram.send_medias_msg(
        medias=medias,
        title="推荐媒体列表"
    )

    assert result


def test_send_medias_msg_without_vote_average(telegram):
    """
    测试发送无评分的媒体列表消息
    """
    # 创建模拟的媒体信息列表（无评分）
    media1 = MediaInfo()
    media1.type = MediaType.MOVIE
    media1.title = "测试电影1"
    media1.year = "2023"
    media1.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"
    media1.tmdb_id=123123
    medias = [media1]

    result = telegram.send_medias_msg(
        medias=medias,
        title="推荐媒体列表"
    )

    assert result


def test_send_medias_msg_with_link_and_buttons(telegram):
    """
    测试发送带链接和按钮的媒体列表消息
    """
    media1 = MediaInfo()
    media1.type = MediaType.MOVIE
    media1.title = r"测试*-|\.电影1"
    media1.year = "2023"
    media1.vote_average = 8.5
    media1.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"
    media1.tmdb_id=123123

    medias = [media1]

    buttons = [[
        {"text": "测试按钮", "callback_data": "test_callback"}
    ]]

    result = telegram.send_medias_msg(
        medias=medias,
        title="推荐媒体列表",
        link="http://example.com",
        buttons=buttons
    )

    assert result


def test_send_torrents_msg_success(telegram):
    """
    测试发送种子列表消息成功
    """
    # 创建模拟的种子信息
    media_info = MediaInfo()
    media_info.type = MediaType.TV
    media_info.title = "唐朝诡事录"
    media_info.year = "2025"
    media_info.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"

    torrent_info = TorrentInfo()
    torrent_info.site_name = r"测试*-|\.站点"
    torrent_info.title = "唐朝诡事录"
    torrent_info.description = "唐朝诡事录之长安3 / 唐朝诡事录3 / 唐朝诡事录 第三部 / 唐朝诡事录·长安 / 唐诡3 / Horror Stories of Tang Dynasty Ⅲ / Strange Legend of Tang Dynasty Ⅲ 第3季 第31-32集 | 主演: 杨旭文 杨志刚 郜思雯 [内封简繁英多国软字幕] 【去头尾广告纯享版】[非伪去头] *发现未去净的广告或片头片尾，奖励魔力1W"
    torrent_info.page_url = "http://example.com/torrent"
    # 文件大小为 1 GiB。
    torrent_info.size = 1024 * 1024 * 1024
    torrent_info.seeders = 10
    torrent_info.uploadvolumefactor = 1.0
    torrent_info.downloadvolumefactor = 0.0

    meta_info = MetaInfo(title="唐朝诡事录")

    context = Context()
    context.media_info = media_info
    context.torrent_info = torrent_info
    context.meta_info = meta_info

    torrents = [context]

    result = telegram.send_torrents_msg(
        torrents=torrents,
        title="种子列表"
    )

    assert result


def test_send_torrents_msg_with_link_and_buttons(telegram):
    """
    测试发送带链接和按钮的种子列表消息
    """
    media_info = MediaInfo()
    media_info.type = MediaType.MOVIE
    media_info.title = "^测试电影~_测试_"
    media_info.year = "2023"
    media_info.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"

    torrent_info = TorrentInfo()
    torrent_info.site_name = "^测试~站点_测试_"
    torrent_info.title = "测试种子标题"
    torrent_info.description = "测试种子描述"
    torrent_info.page_url = "http://example.com/torrent"
    # 文件大小为 1 GiB。
    torrent_info.size = 1024 * 1024 * 1024
    torrent_info.seeders = 10
    torrent_info.uploadvolumefactor = 1.0
    torrent_info.downloadvolumefactor = 0.0

    meta_info = MetaInfo(title="测试种子标题")

    context = Context()
    context.media_info = media_info
    context.torrent_info = torrent_info
    context.meta_info = meta_info

    torrents = [context]

    buttons = [[
        {"text": "测试按钮", "callback_data": "test_callback"}
    ]]

    result = telegram.send_torrents_msg(
        torrents=torrents,
        title="种子列表",
        link="http://example.com",
        buttons=buttons
    )

    assert result


def test_send_msg_with_buttons_and_link(telegram):
    """
    测试发送带按钮和链接的消息
    """
    buttons = [[
        {"text": "测试按钮", "callback_data": "test_callback"}
    ]]

    result = telegram.send_msg(
        title="测试标题",
        text="*测试内容*",
        link="http://example.com",
        buttons=buttons
    )

    # 验证返回值：send_msg 失败时返回 {"success": False}（非空字典），故显式断言 success
    assert result and result.get("success")


def test_send_msg_with_url_buttons(telegram):
    """
    测试发送带URL按钮的消息
    """
    buttons = [[
        {"text": "URL按钮", "url": "http://example.com"}
    ]]

    result = telegram.send_msg(
        title="测试标题",
        text="测试内容",
        buttons=buttons
    )

    # 验证返回值：send_msg 失败时返回 {"success": False}（非空字典），故显式断言 success
    assert result and result.get("success")


def test_send_msg_markdown_escaping(telegram):
    """
    测试Markdown特殊字符转义
    """
    result = telegram.send_msg(
        title="测试标题",
        text="_测试_||内容||"
    )

    # 验证返回值：send_msg 失败时返回 {"success": False}（非空字典），故显式断言 success
    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    assert send_kwargs["parse_mode"] == "MarkdownV2"
    assert send_kwargs["text"].startswith("*测试标题*\n")


@pytest.mark.parametrize("error", [
    TelegramApiError("sendPhoto", "invalid photo", 400),
    TelegramNetworkError("sendPhoto", "timeout"),
])
def test_photo_failure_falls_back_without_losing_notification_context(telegram, error):
    """
    图片被拒绝或网络失败时立即发正文，保留格式、链接、话题与按钮。
    """
    telegram.bot.send_photo.side_effect = error
    telegram.bot.send_message.return_value = {"message_id": 18, "chat": {"id": "fake_chat_id"}}
    telegram._telegram_topic_id = 101
    result = telegram.send_msg(
        title="下载已添加", text="<b>正文</b>", image="https://example.com/poster.jpg",
        link="https://example.com/detail", parse_mode="HTML", disable_web_page_preview=True,
        buttons=[[{"text": "查看", "url": "https://example.com"}]],
    )
    assert result == {"success": True, "message_id": 18, "chat_id": "fake_chat_id"}
    telegram.bot.send_photo.assert_called_once()
    telegram.bot.send_message.assert_called_once()
    photo = telegram.bot.send_photo.call_args.kwargs
    text = telegram.bot.send_message.call_args.kwargs
    assert text["text"] == photo["caption"]
    assert "正文" in text["text"] and "https://example.com/detail" in text["text"]
    for key in ("chat_id", "message_thread_id", "reply_markup", "parse_mode"):
        assert text[key] == photo[key]
    assert text["message_thread_id"] == 101 and text["disable_web_page_preview"] is True


def test_photo_rate_limit_does_not_send_fallback_or_retry(telegram):
    """
    429 必须尊重冷却期，不再用文本请求加重限流。
    """
    telegram.bot.send_photo.side_effect = TelegramApiError(
        "sendPhoto", "Too Many Requests", 429, {"retry_after": 17},
    )
    result = telegram.send_msg(title="通知", image="https://example.com/poster.jpg")
    assert result["success"] is False
    telegram.bot.send_photo.assert_called_once()
    telegram.bot.send_message.assert_not_called()


@pytest.mark.parametrize("parse_mode", ["", "MarkdownV2"])
def test_long_photo_notification_preserves_all_text_after_fallback(telegram, parse_mode):
    """
    长正文首段图片失败时改发文本，其余分段与首段按钮保持完整。
    """
    telegram.bot.send_photo.side_effect = TelegramNetworkError("sendPhoto", "timeout")
    body = "完整通知内容" * 800
    result = telegram.send_msg(
        title="", text=body, parse_mode=parse_mode, image="https://example.com/poster.jpg",
        buttons=[[{"text": "查看", "url": "https://example.com"}]],
    )
    assert result["success"] is True
    telegram.bot.send_photo.assert_called_once()
    chunks = [call.kwargs for call in telegram.bot.send_message.call_args_list]
    assert len(chunks) > 1
    assert "".join(chunk["text"] for chunk in chunks).replace("\n", "") == body
    assert chunks[0]["reply_markup"] is not None
    assert all(chunk["reply_markup"] is None for chunk in chunks[1:])


def test_notification_failure_is_not_acknowledged_by_outbox(telegram, monkeypatch):
    """
    图片和正文最终都失败时，模块抛错使持久消息进入重试而非完成。
    """
    from datetime import datetime, timezone

    from app.application.outbox import ClaimedOutboxMessage, OutboxDispatcher

    telegram.bot.send_photo.side_effect = TelegramNetworkError("sendPhoto", "timeout")
    telegram.bot.send_message.side_effect = TelegramApiError("sendMessage", "forbidden", 403)
    # 只移除测试中的真实退避等待，保留完整重试与模块派发路径。
    monkeypatch.setattr("app.runtime.execution.time.sleep", lambda _seconds: None)
    module = TelegramModule()
    monkeypatch.setattr(module, "get_configs", lambda: {"tg": SimpleNamespace(name="tg")})
    monkeypatch.setattr(module, "check_message", lambda *_args: True)
    monkeypatch.setattr(module, "get_instance", lambda _name: telegram)
    store = MagicMock()
    store.claim.return_value = ClaimedOutboxMessage(1, "download:1", "notification", {}, 1, 1)
    message = Message(title="下载完成", image="https://example.com/poster.jpg")
    dispatcher = OutboxDispatcher(
        store, {"notification": lambda _claimed: module.post_message(message)},
        clock=lambda: datetime(2026, 10, 17, tzinfo=timezone.utc),
    )
    assert dispatcher.dispatch_one()
    store.complete.assert_not_called()
    store.retry.assert_called_once()
    assert store.retry.call_args.kwargs["dead"] is False
    assert "Telegram 通知发送未确认" in store.retry.call_args.kwargs["last_error"]


def test_photo_success_does_not_send_duplicate_text(telegram):
    """
    正常图片通知仍只发一次，文本兜底仅在失败时启用。
    """
    assert telegram.send_msg(title="通知", image="https://example.com/poster.jpg")["success"]
    telegram.bot.send_photo.assert_called_once()
    telegram.bot.send_message.assert_not_called()


def test_telegramify_current_fields_are_used_directly():
    """
    telegramify 对象直接使用当前 MarkdownV2 字段
    """
    from telegramify_markdown.content import ContentTrace, File, Text

    text_item = Text(
        text="已转义_文本",
        entities=[],
        content_trace=ContentTrace(source_type="test"),
    )
    file_item = File(
        file_name="test.txt",
        file_data=b"test",
        caption_text="已转义_说明",
        caption_entities=[],
        content_trace=ContentTrace(source_type="test"),
    )

    with warnings.catch_warnings(record=True) as warning_records:
        warnings.simplefilter("always")
        assert Telegram._telegramify_item_text(text_item) == "已转义\\_文本"
        assert Telegram._telegramify_item_caption(file_item) == "已转义\\_说明"

    assert not warning_records


def test_send_msg_with_html_parse_mode_keeps_html(telegram):
    """
    HTML模式发送时应保留调用方传入的HTML内容
    """
    result = telegram.send_msg(
        title="测试 <标题>",
        text="<blockquote>第一行</blockquote><b>加粗</b>",
        link="https://example.com/?a=1&b=2",
        parse_mode="HTML",
    )

    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    assert send_kwargs["parse_mode"] == "HTML"
    assert send_kwargs["text"] == (
        '<b>测试 &lt;标题&gt;</b>\n'
        '<blockquote>第一行</blockquote><b>加粗</b>\n'
        '<a href="https://example.com/?a=1&amp;b=2">查看详情</a>'
    )


def test_send_msg_uses_rich_message_api(telegram):
    """
    Rich Markdown 应转换后通过 Telegram sendRichMessage 发送。
    """
    telegram.bot.send_rich_message.return_value = {
        "message_id": 101,
        "chat": {"id": 10001},
    }

    result = telegram.send_msg(
        title="",
        rich_message=(
            "# 处理完成\n\n"
            "| 项目 | 结果 |\n"
            "| --- | --- |\n"
            "| 下载 | **成功** |"
        ),
        buttons=[[{"text": "查看", "url": "https://example.com"}]],
    )

    assert result == {"success": True, "message_id": 101, "chat_id": 10001}
    telegram.bot.send_message.assert_not_called()
    send_kwargs = telegram.bot.send_rich_message.call_args.kwargs
    assert send_kwargs["chat_id"] == "fake_chat_id"
    assert send_kwargs["rich_message"].get("markdown") is None
    assert "<h1>处理完成</h1>" in send_kwargs["rich_message"].get("html")
    assert "<table>" in send_kwargs["rich_message"].get("html")
    assert send_kwargs["reply_markup"] is not None


def test_send_msg_edits_rich_message(telegram):
    """
    带原消息定位信息的 Rich Message 应使用富文本编辑接口。
    """
    result = telegram.send_msg(
        title="",
        rich_message="# 更新结果\n\n- 已完成",
        original_message_id=101,
        original_chat_id="10001",
    )

    assert result == {"success": True, "message_id": 101, "chat_id": "10001"}
    edit_kwargs = telegram.bot.edit_message_text.call_args.kwargs
    assert edit_kwargs["text"] is None
    assert edit_kwargs["message_id"] == 101
    assert "<h1>更新结果</h1>" in edit_kwargs["rich_message"].get("html")


def test_telegram_module_passes_parse_mode_to_client():
    """
    模块发送通知时应透传消息指定的parse_mode
    """
    module = TelegramModule()
    client = Mock()

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        module.post_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                title="HTML",
                text="<b>正文</b>",
                parse_mode="HTML",
            )
        )

    client.send_msg.assert_called_once()
    assert client.send_msg.call_args.kwargs["parse_mode"] == "HTML"


def test_telegram_module_passes_rich_message_to_client():
    """
    Telegram 模块应把消息模型中的 Rich Markdown 透传给客户端。
    """
    module = TelegramModule()
    client = Mock()

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        module.post_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                rich_message="# 智能体回复\n\n- 已完成",
            )
        )

    client.send_msg.assert_called_once()
    assert client.send_msg.call_args.kwargs["rich_message"].startswith("# 智能体回复")


def test_telegram_module_passes_streaming_rich_message_to_edit_client():
    """
    Telegram 模块应把流式 Rich Markdown 元数据透传给编辑客户端。
    """
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    client = Mock()
    client.edit_msg.return_value = True

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        result = module.edit_message(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            message_id=101,
            chat_id="10001",
            text="# 普通回退",
            metadata={"telegram_rich_message": "# 流式富文本"},
        )

    assert result is True
    assert client.edit_msg.call_args.kwargs["rich_message"] == "# 流式富文本"


def test_telegram_module_plain_post_message_keeps_chat_without_editing_source_message():
    """
    普通通知应保留原会话目标，同时避免把来源消息 ID 当成编辑目标。
    """
    module = TelegramModule()
    client = Mock()

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        module.post_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                title="Agent 回复",
                text="处理完成",
                original_message_id=123,
                original_chat_id="chat-a",
            )
        )

    client.send_msg.assert_called_once()
    kwargs = client.send_msg.call_args.kwargs
    assert kwargs["original_message_id"] is None
    assert kwargs["original_chat_id"] == "chat-a"


def test_telegram_module_passes_force_reply_to_client():
    """
    模块发送通知时应透传交互消息参数
    """
    module = TelegramModule()
    client = Mock()
    buttons = [[{"text": "取消", "callback_data": "cancel"}]]

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        module.post_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                title="请输入目录",
                text="回复目录路径",
                force_reply=True,
                buttons=buttons,
                original_message_id=123,
                original_chat_id="chat-a",
            )
        )

    client.send_msg.assert_called_once()
    kwargs = client.send_msg.call_args.kwargs
    assert kwargs["force_reply"] is True
    assert kwargs["buttons"] == buttons
    assert kwargs["original_message_id"] == 123
    assert kwargs["original_chat_id"] == "chat-a"


def test_telegram_module_force_reply_sends_new_prompt_message():
    """
    无按钮 ForceReply 应保留原消息 ID，让 client 发新提示并 reply_to 原消息。
    """
    module = TelegramModule()
    client = Mock()

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        module.post_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                title="请输入目录",
                text="回复目录路径",
                force_reply=True,
                original_message_id=123,
                original_chat_id="chat-a",
            )
        )

    client.send_msg.assert_called_once()
    kwargs = client.send_msg.call_args.kwargs
    assert kwargs["force_reply"] is True
    assert kwargs["buttons"] is None
    assert kwargs["original_message_id"] == 123
    assert kwargs["original_chat_id"] == "chat-a"


def test_telegram_module_direct_force_reply_sends_new_prompt_message():
    """
    direct message 的无按钮 ForceReply 同样保留原消息 ID，交给 client 发送新提示。
    """
    module = TelegramModule()
    client = Mock()
    client.send_msg.return_value = {
        "success": True,
        "message_id": 456,
        "chat_id": "chat-a",
    }

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        response = module.send_direct_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                title="请输入目录",
                text="回复目录路径",
                force_reply=True,
                original_message_id=123,
                original_chat_id="chat-a",
            )
        )

    client.send_msg.assert_called_once()
    kwargs = client.send_msg.call_args.kwargs
    assert kwargs["force_reply"] is True
    assert "buttons" not in kwargs
    assert kwargs["original_message_id"] == 123
    assert kwargs["original_chat_id"] == "chat-a"
    assert response.message_id == 456


def test_telegram_module_direct_buttons_keep_new_message_behavior():
    """
    direct message 不透传原消息上下文，避免从发新消息变成编辑旧消息。
    """
    module = TelegramModule()
    client = Mock()
    buttons = [[{"text": "确认", "callback_data": "confirm"}]]
    client.send_msg.return_value = {
        "success": True,
        "message_id": 456,
        "chat_id": "chat-a",
    }

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        response = module.send_direct_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                title="请选择",
                text="请选择一个操作",
                buttons=buttons,
                original_message_id=123,
                original_chat_id="chat-a",
            )
        )

    client.send_msg.assert_called_once()
    kwargs = client.send_msg.call_args.kwargs
    assert "buttons" not in kwargs
    assert kwargs["original_message_id"] is None
    assert kwargs["original_chat_id"] is None
    assert response.message_id == 456


def test_telegram_module_plain_direct_message_keeps_userid_target():
    """
    普通 direct message 不使用 original_chat_id，避免把私聊消息发回原群聊。
    """
    module = TelegramModule()
    client = Mock()
    client.send_msg.return_value = {
        "success": True,
        "message_id": 456,
        "chat_id": "10001",
    }

    with patch.object(
        module,
        "get_configs",
        return_value={"telegram-test": SimpleNamespace(name="telegram-test")},
    ), patch.object(
        module, "check_message", return_value=True
    ), patch.object(
        module, "get_instance", return_value=client
    ):
        response = module.send_direct_message(
            Message(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                userid="10001",
                title="普通通知",
                text="只发给用户",
                original_chat_id="group-1",
            )
        )

    client.send_msg.assert_called_once()
    kwargs = client.send_msg.call_args.kwargs
    assert kwargs["userid"] == "10001"
    assert kwargs["original_message_id"] is None
    assert kwargs["original_chat_id"] is None
    assert response.message_id == 456


def test_telegram_private_delivery_bypasses_group_chat_mapping(telegram):
    """
    私聊投递必须直接使用用户 ID，不能沿用该用户最近发言的群聊。
    """
    telegram._user_chat_mapping["10001"] = "group-1"

    result = telegram.send_msg(
        title="",
        text="受保护消息",
        userid="10001",
        private_delivery=True,
    )

    assert result and result.get("success")
    assert telegram.bot.send_message.call_args.kwargs["chat_id"] == "10001"


def test_telegram_plain_private_delivery_keeps_literal_text(telegram):
    """
    纯文本私聊不得解释或改写密钥中可能出现的 Markdown/HTML 字符。
    """
    literal_text = "G2A1_PROTECTED_MARKER_20260812\n**literal markdown**\n<img src=x>"

    result = telegram.send_msg(
        title="",
        text=literal_text,
        userid="10001",
        private_delivery=True,
        parse_mode="plain",
    )

    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    assert send_kwargs["text"] == literal_text
    assert send_kwargs["parse_mode"] == ""


def test_send_msg_with_force_reply_uses_force_reply_when_no_buttons(telegram):
    """
    无按钮时force_reply应生成Telegram ForceReply标记
    """
    result = telegram.send_msg(
        title="请输入目录",
        text="回复目录路径",
        force_reply=True,
    )

    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    reply_markup = send_kwargs["reply_markup"]
    assert reply_markup["force_reply"] is True
    assert reply_markup["selective"] is True


def test_send_msg_with_force_reply_keeps_inline_keyboard_when_buttons_exist(telegram):
    """
    按钮存在时force_reply不能覆盖InlineKeyboardMarkup
    """
    result = telegram.send_msg(
        title="请选择目录",
        text="点击按钮选择",
        buttons=[[{"text": "默认", "callback_data": "default"}]],
        force_reply=True,
    )

    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    reply_markup = send_kwargs["reply_markup"]
    assert reply_markup == {
        "inline_keyboard": [[{"text": "默认", "callback_data": "default"}]]
    }


def test_send_msg_with_force_reply_and_original_message_sends_new_prompt(telegram):
    """
    编辑消息场景不能带ForceReply，应改为发送新的回复提示。
    """
    result = telegram.send_msg(
        title="请输入关键词",
        text="回复节目关键词",
        force_reply=True,
        original_message_id=123,
        original_chat_id="group-1",
    )

    assert result and result.get("success")
    telegram.bot.edit_message_text.assert_not_called()
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    assert send_kwargs["chat_id"] == "group-1"
    assert send_kwargs["reply_to_message_id"] == 123
    assert send_kwargs["reply_markup"]["force_reply"] is True


def test_send_msg_new_direct_context_message_prefers_original_chat(telegram):
    """
    不编辑旧消息时，original_chat_id 仍用于把新消息发回原交互会话。
    """
    result = telegram.send_msg(
        title="请输入关键词",
        text="回复节目关键词",
        userid="10001",
        original_chat_id="group-1",
    )

    assert result and result.get("success")
    telegram.bot.edit_message_text.assert_not_called()
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    assert send_kwargs["chat_id"] == "group-1"
    assert "reply_to_message_id" not in send_kwargs


def test_edit_msg_falls_back_to_caption_when_original_message_has_no_text(telegram):
    """
    编辑图片消息时应在文本编辑失败后回退为 caption 编辑。
    """
    telegram.bot.edit_message_text.side_effect = Exception(
        "Bad Request: there is no text in the message to edit"
    )

    result = telegram.edit_msg(
        chat_id="1051253579",
        message_id="110502",
        title="【真人快打2】请选择下载目录",
        text="1. 默认目录 (/volume1/Download/)\n2. 目录监控 (/volume1/Download/目录监控/)",
        buttons=[
            [
                {"text": "1", "callback_data": "media:f64b855a2be7:download-dir:1"},
                {"text": "2", "callback_data": "media:f64b855a2be7:download-dir:2"},
            ]
        ],
    )

    assert result is True
    telegram.bot.edit_message_text.assert_called_once()
    telegram.bot.edit_message_caption.assert_called_once()
    caption_kwargs = telegram.bot.edit_message_caption.call_args.kwargs
    assert caption_kwargs["chat_id"] == "1051253579"
    assert caption_kwargs["message_id"] == 110502
    assert "请选择下载目录" in caption_kwargs["caption"]
    assert caption_kwargs["reply_markup"] is not None


def test_edit_msg_with_html_parse_mode_keeps_html(telegram):
    """
    HTML模式编辑消息时应保留HTML内容
    """
    result = telegram.edit_msg(
        chat_id="1051253579",
        message_id="110502",
        title="标题",
        text="<blockquote>请选择</blockquote>",
        parse_mode="HTML",
    )

    assert result is True
    edit_kwargs = telegram.bot.edit_message_text.call_args.kwargs
    assert edit_kwargs["parse_mode"] == "HTML"
    assert edit_kwargs["text"] == "<b>标题</b>\n<blockquote>请选择</blockquote>"


def test_edit_msg_treats_message_not_modified_as_success(telegram):
    """
    重复编辑相同内容时应视为成功，避免记录错误日志。
    """
    telegram.bot.edit_message_text.side_effect = Exception(
        "Bad Request: message is not modified"
    )

    result = telegram.edit_msg(
        chat_id="1051253579",
        message_id="110502",
        title="测试标题",
        text="测试内容",
    )

    assert result is True
    telegram.bot.edit_message_text.assert_called_once()
    telegram.bot.edit_message_caption.assert_not_called()


def test_send_msg_edit_with_image_falls_back_to_text_when_image_url_unavailable(telegram):
    """
    编辑图片消息失败时应去掉图片并降级为文本编辑。
    """
    telegram.bot.edit_message_media.side_effect = Exception(
        "Bad Request: failed to get HTTP URL content"
    )

    result = telegram.send_msg(
        title="测试标题",
        text="测试内容",
        image="https://example.com/poster.jpg",
        buttons=[[{"text": "确认", "callback_data": "confirm"}]],
        original_chat_id="1051253579",
        original_message_id=110502,
    )

    assert result == {
        "success": True,
        "message_id": 110502,
        "chat_id": "1051253579",
    }
    telegram.bot.edit_message_media.assert_called_once()
    telegram.bot.edit_message_text.assert_called_once()
    edit_kwargs = telegram.bot.edit_message_text.call_args.kwargs
    assert edit_kwargs["chat_id"] == "1051253579"
    assert edit_kwargs["message_id"] == 110502
    assert "测试标题" in edit_kwargs["text"]
    assert "测试内容" in edit_kwargs["text"]
    assert edit_kwargs["reply_markup"] is not None


def _group_message(text, **extra):
    """
    构造群聊文本消息的 Bot API 结构。
    """
    message = {
        "message_id": 5,
        "from": {"id": 42, "is_bot": False, "first_name": "u"},
        "chat": {"id": -1001, "type": "supergroup"},
        "text": text,
    }
    message.update(extra)
    return message


def test_handle_update_forwards_raw_message_dict(telegram):
    """
    普通消息以 Bot API 原始 dict 转发主程序，并记录用户最近会话。
    """
    message = {
        "message_id": 1,
        "from": {"id": 42, "is_bot": False, "first_name": "u"},
        "chat": {"id": 42, "type": "private"},
        "photo": [{"file_id": "image-1"}],
        "caption": "看看",
    }
    with patch.object(telegram, "_forward_to_message_chain", return_value=True) as forward:
        telegram._handle_update({"update_id": 1, "message": message})

    forward.assert_called_once_with(message)
    assert telegram._get_user_chat_id("42") == "42"


@pytest.mark.parametrize("text", ["/start", "/help@test_bot", "/start@other_bot payload"])
def test_handle_update_replies_welcome_without_forwarding(telegram, text):
    """
    /start、/help 只回复使用提示，与原 SDK 一样忽略 @bot 后缀和参数。
    """
    message = _group_message(text)
    with patch.object(telegram, "_forward_to_message_chain") as forward:
        telegram._handle_update({"update_id": 1, "message": message})

    forward.assert_not_called()
    kwargs = telegram.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == -1001
    assert kwargs["reply_to_message_id"] == 5


def test_handle_update_ignores_unsupported_content(telegram):
    """
    入群通知等非转发内容类型不进入主程序。
    """
    message = _group_message(None)
    del message["text"]
    message["new_chat_members"] = [{"id": 7}]
    with patch.object(telegram, "_forward_to_message_chain") as forward:
        telegram._handle_update({"update_id": 1, "message": message})
    forward.assert_not_called()


def test_group_message_requires_mention_entity(telegram):
    """
    群聊消息只在 @bot 时处理，实体偏移按 UTF-16 计算。
    """
    assert telegram._should_process_message(_group_message("随便聊聊")) is False
    text = "😀 @test_bot 搜索"
    mentioned = _group_message(
        text, entities=[{"type": "mention", "offset": 3, "length": 9}]
    )
    assert telegram._should_process_message(mentioned) is True
    caption_message = _group_message(None, caption="/search 片名")
    assert telegram._should_process_message(caption_message) is True


def test_handle_callback_query_answers_then_forwards(telegram):
    """
    按钮回调先应答结束加载状态，再以精简结构转发主程序。
    """
    call = {
        "id": "cb-1",
        "from": {"id": 42, "is_bot": False, "first_name": "u"},
        "message": {"message_id": 9, "chat": {"id": -1001, "type": "supergroup"}},
        "data": "page:2",
    }
    with patch.object(telegram, "_forward_to_message_chain", return_value=True) as forward:
        telegram._handle_update({"update_id": 2, "callback_query": call})

    telegram.bot.answer_callback_query.assert_called_once_with("cb-1")
    payload = forward.call_args.args[0]["callback_query"]
    assert payload["from"]["id"] == 42
    assert payload["message"] == {"message_id": 9, "chat": {"id": -1001}}
    assert payload["data"] == "page:2"
    assert telegram._get_user_chat_id("42") == "-1001"


def test_send_file_returns_message_identity(telegram, tmp_path):
    """
    发送结果从 Bot API Message dict 读取 message_id 和 chat id。
    """
    file_path = tmp_path / "report.txt"
    file_path.write_text("ok", encoding="utf-8")
    telegram.bot.send_document.return_value = {"message_id": 88, "chat": {"id": 10001}}

    result = telegram.send_file(str(file_path), userid="10001")

    assert result == {"success": True, "message_id": 88, "chat_id": 10001}
    name, _ = telegram.bot.send_document.call_args.kwargs["document"]
    assert name == "report.txt"


def test_register_commands_uses_bot_command_dicts(telegram):
    """
    菜单命令去掉前导斜杠，按 BotCommand 结构提交。
    """
    telegram.register_commands({"/search": {"description": "搜索"}})
    telegram.bot.delete_my_commands.assert_called_once_with()
    telegram.bot.set_my_commands.assert_called_once_with(
        commands=[{"command": "search", "description": "搜索"}]
    )
