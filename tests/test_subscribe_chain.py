from types import SimpleNamespace
from unittest import TestCase

from app.chain.subscribe import SubscribeChain
from app.core.metainfo import MetaInfo


class SubscribeChainTest(TestCase):
    def test_is_episode_range_covered(self):
        cases = [
            {
                "title": "Cherry Season S01 2014 2160p 60fps WEB-DL H265 AAC-XXX",
                "subtitle": "",
                "subscribe": {"start_episode": None, "total_episode": 51},
                "expected": True,
            },
            {
                "title": "【爪爪字幕组】★7月新番[欢迎来到实力至上主义的教室 第二季/Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e S2][11][1080p][HEVC][GB][MP4][招募翻译校对]",
                "subtitle": "",
                "subscribe": {"start_episode": None, "total_episode": 13},
                "expected": False,
            },
            {
                "title": "[秋叶原冥途战争][Akiba Maid Sensou][2022][WEB-DL][1080][TV Series][第01话][LeagueWEB]",
                "subtitle": "",
                "subscribe": {"start_episode": None, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Qi Refining for 3000 Years S01E06 2022 1080p B-Blobal WEB-DL X264 AAC-AnimeS@AdWeb",
                "subtitle": "",
                "subscribe": {"start_episode": None, "total_episode": 16},
                "expected": False,
            },
            {
                "title": "The Heart of Genius S01 13-14 2022 1080p WEB-DL H264 AAC",
                "subtitle": "",
                "subscribe": {"start_episode": None, "total_episode": 34},
                "expected": False,
            },
            {
                "title": "[xyx98]传颂之物/Utawarerumono/うたわれるもの[BDrip][1920x1080][TV 01-26 Fin][hevc-yuv420p10 flac_ac3][ENG PGS]",
                "subtitle": "",
                "subscribe": {"start_episode": None, "total_episode": 26},
                "expected": True,
            },
            {
                "title": "I Woke Up a Vampire S02 2023 2160p NF WEB-DL DDP5.1 Atmos H 265-HHWEB",
                "subtitle": "醒来变成吸血鬼 第二季 | 全8集 | 4K | 类型: 喜剧/家庭/奇幻 | 导演: TommyLynch | 主演: NikoCeci/ZebastinBorjeau/安娜·阿劳约/KaileenAngelicChang/KrisSiddiqi",
                "subscribe": {"start_episode": None, "total_episode": 8},
                "expected": True,
            },
            {
                "title": "Shadows of the Void S01 2024 1080p WEB-DL H264 AAC-HHWEB",
                "subtitle": "虚无边境 | 第01-02集 | 1080p | 类型: 动画 | 导演: 巴西 | 主演: 山新/周一菡/皇贞季/Kenz/李佳怡 [内嵌中字]",
                "subscribe": {"start_episode": None, "total_episode": 13},
                "expected": False,
            },
            {
                "title": "Mai Xiang S01 2019 2160p WEB-DL H.265 DDP2.0-HHWEB",
                "subtitle": "麦香 | 全36集 | 4K | 类型:剧情/爱情/家庭 | 主演:傅晶/章呈赫/王伟/沙景昌/何音",
                "subscribe": {"start_episode": None, "total_episode": 36},
                "expected": True,
            },
            {
                "title": "Jigokuraku S01E14-E25 2023 1080p CR WEB-DL x264 AAC-Nest@ADWeb",
                "subtitle": "地狱乐 / 地獄楽 / Hell’s Paradise [14-25Fin] [中日双语字幕]",
                "subscribe": {"start_episode": 14, "total_episode": 25},
                "expected": True,
            },
            {
                "title": "Jigokuraku S01 2023 1080p BluRay Remux AVC FLAC 2.0-AnimeF@ADE",
                "subtitle": "地狱乐/Hell's Paradise: Jigokuraku [01-13Fin] [中日双语字幕]",
                "subscribe": {"start_episode": None, "total_episode": 13},
                "expected": True,
            },
            {
                "title": "Jigokuraku S02E12 2026 1080p NF WEB-DL x264 AAC-ADWeb",
                "subtitle": "地狱乐 第二季 地獄楽 第二期 第12集 | 类型: 动画",
                "subscribe": {"start_episode": None, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Jigokuraku S02E05-E07 2026 1080p NF WEB-DL x264 AAC-ADWeb",
                "subtitle": "地狱乐 第二季 地獄楽 第二期 第05-07集 | 类型: 动画",
                "subscribe": {"start_episode": None, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Bungo Stray Dogs S01 2016 1080p KKTV WEB-DL x264 AAC-ADWeb",
                "subtitle": "文豪野犬 文豪ストレイドッグス 又名: 文豪Stray Dogs 第一季 全12集 | 类型: 剧情 / 动作 / 动画 主演: 上村祐翔 / 宫野真守 / 细谷佳正 *内嵌繁体字幕*",
                "subscribe": {"start_episode": None, "total_episode": 12},
                "expected": True,
            },
            {
                "title": "Bungou Stray Dogs S1+S2+S3+OAD 1080p BDRip HEVC FLAC-Snow-Raws",
                "subtitle": "文豪野犬 第1-3季",
                "subscribe": {"start_episode": None, "total_episode": 36},
                "expected": True,
            },
            {
                "title": "Bungou Stray Dogs S1+S2+S3+OAD 1080p BDRip HEVC FLAC-Snow-Raws",
                "subtitle": "文豪野犬 第1-3季",
                "subscribe": {"start_episode": None, "total_episode": 60},
                "expected": True,  # 识别不到集数全匹配
            },
            {
                "title": "Fu Gui S01 2005 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "福贵 | 全33集 | 4K | 类型: 剧情/家庭 | 导演: 朱正/袁进 | 主演: 陈创/刘敏涛/李丁/张鹰/温玉娟",
                "subscribe": {"start_episode": None, "total_episode": 33},
                "expected": True,
            },
            {
                "title": "The Story of Ming Lan S01 2018 2160p WEB-DL CHDWEB",
                "subtitle": "知否知否应是绿肥红瘦 全78集 | 2160p | 国语/中字 | 60帧高码TV版 | 类型:剧情/爱情/古装 | 主演:赵丽颖/冯绍峰/朱一龙/施诗/张佳宁",
                "subscribe": {"start_episode": None, "total_episode": 78},
                "expected": True,
            },
            {
                "title": "Love Beyond the Grave S01 2026 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "白日提灯 / 慕胥辞 | 第18集 | 4K | 类型: 剧情 | 导演: 秦榛 | 主演: 迪丽热巴/陈飞宇/魏哲鸣/张俪/高鹤元",
                "subscribe": {"start_episode": None, "total_episode": 40},
                "expected": False,
            },
            {
                "title": "The Long Ballad S01 2021 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "长歌行 | 全49集 | 4K | 类型: 剧情/爱情/古装 | 主演: 迪丽热巴/吴磊/刘宇宁/赵露思/方逸伦",
                "subscribe": {"start_episode": None, "total_episode": 49},
                "expected": True,
            },
            {
                "title": "The Long Ballad S01E01-E04 2021 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "长歌行 | 第01-04集 | 4K | 类型: 剧情/爱情/古装 | 主演: 迪丽热巴/吴磊/刘宇宁/赵露思/方逸伦",
                "subscribe": {"start_episode": None, "total_episode": 49},
                "expected": False,
            },
            {
                "title": "Spy x Family S02 2023 1080p Baha WEB-DL x264 AAC-ADWeb",
                "subtitle": "间谍过家家 第二季 / SPY×FAMILY Season 2 [01-12Fin] [简繁内封字幕]",
                "subscribe": {"start_episode": None, "total_episode": 12},
                "expected": True,
            },
            {
                "title": "Spy x Family S02E03-E07 2023 1080p Baha WEB-DL x264 AAC-ADWeb",
                "subtitle": "间谍过家家 第二季 / SPY×FAMILY Season 2 第03-07集 [简繁内封字幕]",
                "subscribe": {"start_episode": None, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Naruto Shippuden S01-S21 Complete 1080p BluRay x264 AAC-ADWeb",
                "subtitle": "火影忍者 疾风传 全500集 [1080p][简中字幕]",
                "subscribe": {"start_episode": None, "total_episode": 500},
                "expected": True,
            },
            {
                "title": "Naruto Shippuden S01-S21 Complete 1080p BluRay x264 AAC-ADWeb",
                "subtitle": "火影忍者 疾风传 第01-500集 [1080p][简中字幕]",
                "subscribe": {"start_episode": 201, "total_episode": 500},
                "expected": True,
            },
        ]

        for case in cases:
            meta = MetaInfo(
                title=case["title"], subtitle=case["subtitle"], custom_words=["#"]
            )
            subscribe = SimpleNamespace(**case["subscribe"])

            self.assertEqual(
                SubscribeChain._is_episode_range_covered(
                    meta=meta,
                    subscribe=subscribe,
                ),
                case["expected"],
            )
