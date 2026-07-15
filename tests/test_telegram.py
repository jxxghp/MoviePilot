# -*- coding: utf-8 -*-
"""
Telegram 模块单元测试（pytest 原生）。
"""
import json
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.modules.telegram import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.schemas import Notification
from app.schemas.types import MessageChannel
from app.schemas.types import MediaType


@pytest.fixture
def telegram():
    """构造 TeleBot 与 ImageHelper 均已打桩的 Telegram 实例。

    空 token 会让 Telegram.__init__ 提前返回、致 send_* 抛错，故用假 bot 让初始化完整、
    消息发送走内存桩；ImageHelper 打桩避免 send_medias/send_msg 按 poster_path 真实下载海报
    （否则对 raw.githubusercontent.com 等外链发起真实 HTTP，外部 IO 不可接受且拖慢用例）。
    with 上下文在 fixture 结束时自动停桩，即使实例化失败也不泄漏 patch。
    """
    with patch("app.modules.telegram.telegram.TeleBot") as mock_telebot_cls, \
            patch("app.modules.telegram.telegram.ImageHelper") as mock_image_cls:
        bot_instance = MagicMock()
        # get_me 用于初始化 bot 用户名，需返回带 username 的对象
        bot_instance.get_me.return_value = MagicMock(username="test_bot")
        # polling/stop 使用普通函数，避免后台线程执行 MagicMock 时在退出阶段产生锁竞争。
        bot_instance.infinity_polling = lambda *args, **kwargs: None
        bot_instance.stop_polling = lambda *args, **kwargs: None
        mock_telebot_cls.return_value = bot_instance
        mock_image_cls.return_value.fetch_image.return_value = b"fake-image-bytes"
        telegram = Telegram(TELEGRAM_TOKEN="fake_token", TELEGRAM_CHAT_ID="fake_chat_id")
        yield telegram
        telegram.stop()


def test_send_msg_success(telegram):
    """测试发送普通消息成功"""
    # 调用send_msg方法
    result = telegram.send_msg(
        title="📥 开始下载\n唐朝诡事录 (2022)S03E31-E32",
        text="\n🕒 时间： 2025-11-21 18:14:51\n🎭 类别： 国产剧\n🌐 站点： 天空\n🌟 质量： WEB-DL 2160p\n💾 大小： 1.68G\n⚡️ 促销： 未知\n🚨 H&R： 否\n📛 名称： \nStrange Tales of Tang Dynasty S03E31-E32 2025 2160p WEB-DL DDP5.1 H265-Pure@HDSWEB [唐朝诡事录之长安3 / 唐朝诡事录3 / 唐朝诡事录 第三部 / 唐朝诡事录·长安 / 唐诡3 / Horror Stories of Tang Dynasty Ⅲ / Strange Legend of Tang Dynasty Ⅲ 第3季 第31-32集 | 主演: 杨旭文 杨志刚 郜思雯 [内封简繁英多国软字幕] 【去头尾广告纯享版】[非伪去头] *发现未去净的广告或片头片尾，奖励魔力1W]"
    )

    # 验证返回值：send_msg 失败时返回 {"success": False}（非空字典，仅 truthy 检查会漏判），故显式断言 success
    assert result and result.get("success")


def test_telegram_parser_preserves_reply_to_message_id():
    """Telegram ForceReply 回复应保留来源消息和被回复消息的 message_id。"""
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
    """测试发送长消息"""
    result = telegram.send_msg(
        title="MoviePilot助手",
        text="好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？",
    )
    assert result and result.get("success")


def test_send_medias_msg_success(telegram):
    """测试发送媒体列表消息成功"""
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
    """测试发送无评分的媒体列表消息"""
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
    """测试发送带链接和按钮的媒体列表消息"""
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
    """测试发送种子列表消息成功"""
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
    torrent_info.size = 1024 * 1024 * 1024  # 1GB
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
    """测试发送带链接和按钮的种子列表消息"""
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
    torrent_info.size = 1024 * 1024 * 1024  # 1GB
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
    """测试发送带按钮和链接的消息"""
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
    """测试发送带URL按钮的消息"""
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
    """测试Markdown特殊字符转义"""
    result = telegram.send_msg(
        title="测试标题",
        text="_测试_||内容||"
    )

    # 验证返回值：send_msg 失败时返回 {"success": False}（非空字典），故显式断言 success
    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    assert send_kwargs["parse_mode"] == "MarkdownV2"
    assert send_kwargs["text"].startswith("*测试标题*\n")


def test_telegramify_current_fields_are_used_directly():
    """telegramify 对象直接使用当前 MarkdownV2 字段"""
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
    """HTML模式发送时应保留调用方传入的HTML内容"""
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


def test_telegram_module_passes_parse_mode_to_client():
    """模块发送通知时应透传消息指定的parse_mode"""
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
            Notification(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                title="HTML",
                text="<b>正文</b>",
                parse_mode="HTML",
            )
        )

    client.send_msg.assert_called_once()
    assert client.send_msg.call_args.kwargs["parse_mode"] == "HTML"


def test_telegram_module_plain_post_message_keeps_chat_without_editing_source_message():
    """普通通知应保留原会话目标，同时避免把来源消息 ID 当成编辑目标。"""
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
            Notification(
                channel=MessageChannel.Telegram,
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
    """模块发送通知时应透传交互消息参数"""
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
            Notification(
                channel=MessageChannel.Telegram,
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
    """无按钮 ForceReply 应保留原消息 ID，让 client 发新提示并 reply_to 原消息。"""
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
            Notification(
                channel=MessageChannel.Telegram,
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
    """direct message 的无按钮 ForceReply 同样保留原消息 ID，交给 client 发送新提示。"""
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
            Notification(
                channel=MessageChannel.Telegram,
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
    """direct message 不透传原消息上下文，避免从发新消息变成编辑旧消息。"""
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
            Notification(
                channel=MessageChannel.Telegram,
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
    """普通 direct message 不使用 original_chat_id，避免把私聊消息发回原群聊。"""
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
            Notification(
                channel=MessageChannel.Telegram,
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


def test_send_msg_with_force_reply_uses_force_reply_when_no_buttons(telegram):
    """无按钮时force_reply应生成Telegram ForceReply标记"""
    result = telegram.send_msg(
        title="请输入目录",
        text="回复目录路径",
        force_reply=True,
    )

    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    reply_markup = send_kwargs["reply_markup"]
    assert reply_markup.__class__.__name__ == "ForceReply"
    if hasattr(reply_markup, "to_dict"):
        assert reply_markup.to_dict()["force_reply"] is True
        assert reply_markup.to_dict().get("selective") is True
    else:
        assert getattr(reply_markup, "selective", None) is True


def test_send_msg_with_force_reply_keeps_inline_keyboard_when_buttons_exist(telegram):
    """按钮存在时force_reply不能覆盖InlineKeyboardMarkup"""
    result = telegram.send_msg(
        title="请选择目录",
        text="点击按钮选择",
        buttons=[[{"text": "默认", "callback_data": "default"}]],
        force_reply=True,
    )

    assert result and result.get("success")
    send_kwargs = telegram.bot.send_message.call_args.kwargs
    reply_markup = send_kwargs["reply_markup"]
    assert reply_markup.__class__.__name__ == "InlineKeyboardMarkup"


def test_send_msg_with_force_reply_and_original_message_sends_new_prompt(telegram):
    """编辑消息场景不能带ForceReply，应改为发送新的回复提示。"""
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
    assert send_kwargs["reply_markup"].__class__.__name__ == "ForceReply"


def test_send_msg_new_direct_context_message_prefers_original_chat(telegram):
    """不编辑旧消息时，original_chat_id 仍用于把新消息发回原交互会话。"""
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
    """编辑图片消息时应在文本编辑失败后回退为 caption 编辑。"""
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
    """HTML模式编辑消息时应保留HTML内容"""
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
    """重复编辑相同内容时应视为成功，避免记录错误日志。"""
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
    """编辑图片消息失败时应去掉图片并降级为文本编辑。"""
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
