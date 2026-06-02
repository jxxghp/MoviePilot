# -*- coding: utf-8 -*-
"""
Telegram模块单元测试
"""
import unittest
from unittest.mock import MagicMock, patch

from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.modules.telegram.telegram import Telegram
from app.schemas.types import MediaType


class TestTelegram(unittest.TestCase):

    def setUp(self):
        """测试前准备。

        模拟 telebot.TeleBot 以避免真实 API 调用：空 token 会让 Telegram.__init__ 提前返回、
        属性未初始化导致 send_* 抛错；这里用假 bot 让初始化完整且消息发送走内存桩。
        """
        self.telebot_patcher = patch("app.modules.telegram.telegram.TeleBot")
        mock_telebot_cls = self.telebot_patcher.start()
        self.mock_bot_instance = MagicMock()
        # get_me 用于初始化 bot 用户名，需返回带 username 的对象
        self.mock_bot_instance.get_me.return_value = MagicMock(username="test_bot")
        mock_telebot_cls.return_value = self.mock_bot_instance

        # send_medias/send_msg 发图时会经 ImageHelper().fetch_image 按 poster_path 真实下载海报，
        # 单测必须打桩，否则对 raw.githubusercontent.com 等外链发起真实 HTTP（外部 IO 不可接受且拖慢用例）。
        self.image_patcher = patch("app.modules.telegram.telegram.ImageHelper")
        mock_image_cls = self.image_patcher.start()
        mock_image_cls.return_value.fetch_image.return_value = b"fake-image-bytes"

        self.telegram = Telegram(TELEGRAM_TOKEN="fake_token", TELEGRAM_CHAT_ID="fake_chat_id")

    def tearDown(self):
        """测试后清理：停止 TeleBot 与 ImageHelper 打桩。"""
        self.telebot_patcher.stop()
        self.image_patcher.stop()

    def test_send_msg_success(self):
        """测试发送普通消息成功"""
        # 调用send_msg方法
        result = self.telegram.send_msg(
            title="📥 开始下载\n唐朝诡事录 (2022)S03E31-E32",
            text="\n🕒 时间： 2025-11-21 18:14:51\n🎭 类别： 国产剧\n🌐 站点： 天空\n🌟 质量： WEB-DL 2160p\n💾 大小： 1.68G\n⚡️ 促销： 未知\n🚨 H&R： 否\n📛 名称： \nStrange Tales of Tang Dynasty S03E31-E32 2025 2160p WEB-DL DDP5.1 H265-Pure@HDSWEB [唐朝诡事录之长安3 / 唐朝诡事录3 / 唐朝诡事录 第三部 / 唐朝诡事录·长安 / 唐诡3 / Horror Stories of Tang Dynasty Ⅲ / Strange Legend of Tang Dynasty Ⅲ 第3季 第31-32集 | 主演: 杨旭文 杨志刚 郜思雯 [内封简繁英多国软字幕] 【去头尾广告纯享版】[非伪去头] *发现未去净的广告或片头片尾，奖励魔力1W]"
        )

        # 验证返回值
        self.assertTrue(result)

    def test_send_msg_with_longtext(self):
        """测试发送长消息"""
        result = self.telegram.send_msg(
            title="MoviePilot助手",
            text="好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？好的，为您推荐一些近期热门的电视剧：\n\n*   *怪奇物语 (Stranger Things)* - 2016年，TMDB评分8.6\n*   *小丑回魂：欢迎来到德里镇* - 2025年，TMDB评分8.0\n*   *维京传奇* - 2013年，TMDB评分8.1\n*   *地狱客栈* - 2024年，TMDB评分8.7\n*   *超人回来了* - 2013年，TMDB评分7.7\n\n还有一些经典剧集也一直很受欢迎：\n\n*   *法律与秩序：特殊受害者* - 1999年，TMDB评分7.9\n*   *实习医生格蕾* - 2005年，TMDB评分8.2\n*   *邪恶力量* - 2005年，TMDB评分8.3\n*   *菜鸟老警* - 2018年，TMDB评分8.5\n*   *猎魔人* - 2019年，TMDB评分8.0\n*   *海军罪案调查处* - 2003年，TMDB评分7.6\n*   *塔尔萨之王* - 2022年，TMDB评分8.3\n*   *武士生死斗* - 2025年，TMDB评分8.1\n*   *嗜血法医* - 2006年，TMDB评分8.2\n*   *辛普森一家* - 1989年，TMDB评分8.0\n*   *无耻之徒* - 2011年，TMDB评分8.2\n*   *绝命毒师* - 2008年，TMDB评分8.9\n*   *法律与秩序* - 1990年，TMDB评分7.4\n*   *权力的游戏* - 2011年，TMDB评分8.5\n\n您对哪部剧比较感兴趣，或者想了解更多信息呢？",
        )


    def test_send_medias_msg_success(self):
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

        result = self.telegram.send_medias_msg(
            medias=medias,
            title="推荐媒体列表"
        )

        self.assertTrue(result)

    def test_send_medias_msg_without_vote_average(self):
        """测试发送无评分的媒体列表消息"""
        # 创建模拟的媒体信息列表（无评分）
        media1 = MediaInfo()
        media1.type = MediaType.MOVIE
        media1.title = "测试电影1"
        media1.year = "2023"
        media1.poster_path = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/public/logo.png"
        media1.tmdb_id=123123
        medias = [media1]

        result = self.telegram.send_medias_msg(
            medias=medias,
            title="推荐媒体列表"
        )

        self.assertTrue(result)

    def test_send_medias_msg_with_link_and_buttons(self):
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

        result = self.telegram.send_medias_msg(
            medias=medias,
            title="推荐媒体列表",
            link="http://example.com",
            buttons=buttons
        )

        self.assertTrue(result)



    def test_send_torrents_msg_success(self):
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

        result = self.telegram.send_torrents_msg(
            torrents=torrents,
            title="种子列表"
        )

        self.assertTrue(result)

    def test_send_torrents_msg_with_link_and_buttons(self):
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

        result = self.telegram.send_torrents_msg(
            torrents=torrents,
            title="种子列表",
            link="http://example.com",
            buttons=buttons
        )

        self.assertTrue(result)

    def test_send_msg_with_buttons_and_link(self):
        """测试发送带按钮和链接的消息"""
        buttons = [[
            {"text": "测试按钮", "callback_data": "test_callback"}
        ]]

        result = self.telegram.send_msg(
            title="测试标题",
            text="*测试内容*",
            link="http://example.com",
            buttons=buttons
        )

        # 验证返回值
        self.assertTrue(result)

    def test_send_msg_with_url_buttons(self):
        """测试发送带URL按钮的消息"""
        buttons = [[
            {"text": "URL按钮", "url": "http://example.com"}
        ]]

        result = self.telegram.send_msg(
            title="测试标题",
            text="测试内容",
            buttons=buttons
        )

        # 验证返回值
        self.assertTrue(result)


    def test_send_msg_markdown_escaping(self):
        """测试Markdown特殊字符转义"""
        result = self.telegram.send_msg(
            title="测试标题",
            text="_测试_||内容||"
        )

        # 验证返回值
        self.assertTrue(result)
