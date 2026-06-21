# -*- coding: utf-8 -*-
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.core.metainfo import MetaInfo, MetaInfoPath, find_metainfo
from tests.cases.meta import meta_cases


class MetaInfoTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_metainfo(self):
        for info in meta_cases:
            if info.get("path"):
                meta_info = MetaInfoPath(path=Path(info.get("path")))
            else:
                meta_info = MetaInfo(
                    title=info.get("title"),
                    subtitle=info.get("subtitle"),
                    custom_words=["#"],
                )
            target = {
                "type": meta_info.type.value,
                "cn_name": meta_info.cn_name or "",
                "en_name": meta_info.en_name or "",
                "year": meta_info.year or "",
                "part": meta_info.part or "",
                "season": meta_info.season,
                "episode": meta_info.episode,
                "restype": meta_info.edition,
                "pix": meta_info.resource_pix or "",
                "video_codec": meta_info.video_encode or "",
                "audio_codec": meta_info.audio_encode or "",
                "fps": meta_info.fps or None,
            }

            # 检查tmdbid
            if info.get("target").get("tmdbid"):
                target["tmdbid"] = meta_info.tmdbid

            expected = info.get("target")
            if "fps" not in expected:
                target.pop("fps", None)
            self.assertEqual(target, expected)

    def test_emby_format_ids(self):
        """
        测试Emby格式ID识别
        """
        # 测试文件路径
        test_paths = [
            # 文件名中包含tmdbid
            (
                "/movies/The Vampire Diaries (2009) [tmdbid=18165]/The.Vampire.Diaries.S01E01.1080p.mkv",
                18165,
            ),
            # 目录名中包含tmdbid
            ("/movies/Inception (2010) [tmdbid-27205]/Inception.2010.1080p.mkv", 27205),
            # 父目录名中包含tmdbid
            (
                "/movies/Breaking Bad (2008) [tmdb=1396]/Season 1/Breaking.Bad.S01E01.1080p.mkv",
                1396,
            ),
            # 祖父目录名中包含tmdbid
            (
                "/tv/Game of Thrones (2011) {tmdb=1399}/Season 1/Game.of.Thrones.S01E01.1080p.mkv",
                1399,
            ),
            # 测试{tmdb-xxx}格式
            ("/movies/Avatar (2009) {tmdb-19995}/Avatar.2009.1080p.mkv", 19995),
        ]

        for path_str, expected_tmdbid in test_paths:
            meta = MetaInfoPath(Path(path_str))
            self.assertEqual(
                meta.tmdbid,
                expected_tmdbid,
                f"路径 {path_str} 期望的tmdbid为 {expected_tmdbid}，实际识别为 {meta.tmdbid}",
            )

    def test_metainfopath_with_custom_words(self):
        """测试 MetaInfoPath 使用自定义识别词"""
        # 测试替换词：将"测试替换"替换为空
        custom_words = ["测试替换 => "]
        path = Path("/movies/电影测试替换名称 (2024)/movie.mkv")
        meta = MetaInfoPath(path, custom_words=custom_words)
        # 验证替换生效：cn_name 不应包含"测试替换"
        if meta.cn_name:
            self.assertNotIn("测试替换", meta.cn_name)

    def test_metainfopath_without_custom_words(self):
        """测试 MetaInfoPath 不传入自定义识别词"""
        path = Path("/movies/Normal Movie (2024)/movie.mkv")
        meta = MetaInfoPath(path)
        # 验证正常识别，不报错
        self.assertIsNotNone(meta)

    def test_metainfopath_with_empty_custom_words(self):
        """测试 MetaInfoPath 传入空的自定义识别词"""
        path = Path("/movies/Test Movie (2024)/movie.mkv")
        meta = MetaInfoPath(path, custom_words=[])
        # 验证不报错，正常识别
        self.assertIsNotNone(meta)

    def test_custom_words_apply_words_recording(self):
        """测试 apply_words 记录功能"""
        custom_words = ["替换词 => 新词"]
        title = "电影替换词.2024.mkv"
        meta = MetaInfo(title=title, custom_words=custom_words)
        # 验证 apply_words 属性存在
        self.assertTrue(hasattr(meta, "apply_words"))
        # 如果替换词被应用，应该记录在 apply_words 中
        if meta.apply_words:
            self.assertIn("替换词 => 新词", meta.apply_words)

    def test_metainfo_preserves_original_name_when_custom_words_applied(self):
        """测试应用识别词后仍保留未应用识别词时识别出的名称"""
        custom_words = ["测试替换 => "]
        meta = MetaInfo(title="电影测试替换名称 (2024)", custom_words=custom_words)
        self.assertEqual(meta.name, "电影名称")
        self.assertEqual(meta.original_name, "电影测试替换名称")

    def test_custom_words_replace_then_episode_offset(self):
        """测试复杂识别词仍按先替换、后集数偏移的顺序处理"""
        custom_words = ["旧名 => 新名 && 第 <> 集 >> EP+1"]
        meta = MetaInfo(title="旧名 第03集", custom_words=custom_words)
        self.assertEqual(meta.name, "新名")
        self.assertEqual(meta.episode, "E04")
        self.assertEqual(meta.apply_words, custom_words)

    def test_custom_words_support_episode_group_parameter(self):
        """测试自定义识别词替换结果中的 g 参数会写入剧集组"""
        group_id = "5ad0ec240e0a26303f00d84d"
        custom_words = [
            f"Bakemonogatari => 物语系列 {{[tmdbid=46195;type=tv;g={group_id};s=1]}}"
        ]
        meta = MetaInfo(title="Bakemonogatari 01", custom_words=custom_words)
        self.assertEqual(meta.tmdbid, 46195)
        self.assertEqual(meta.type.value, "电视剧")
        self.assertEqual(meta.begin_season, 1)
        self.assertEqual(meta.episode_group, group_id)
        self.assertEqual(meta.apply_words, custom_words)

    def test_custom_words_support_special_season_zero_parameter(self):
        """显式媒体标签中的 s=0 应作为特别季写入元数据。"""
        custom_words = [
            "Test Show => 测试剧 {[tmdbid=12345;type=tv;s=0]}"
        ]

        with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
            meta = MetaInfo(title="Test Show 01", custom_words=custom_words)

        self.assertEqual(meta.tmdbid, 12345)
        self.assertEqual(meta.type.value, "电视剧")
        self.assertEqual(meta.begin_season, 0)

    def test_find_metainfo_supports_episode_group_parameter(self):
        """测试显式媒体标签支持 g 剧集组参数"""
        group_id = "5ad0ec240e0a26303f00d84d"
        title, metainfo = find_metainfo(f"物语系列 {{[tmdbid=46195;type=tv;g={group_id};s=1]}}")
        self.assertEqual(metainfo["episode_group"], group_id)
        self.assertNotIn("g=", title)

    def test_find_metainfo_does_not_support_episode_group_alias(self):
        """测试 e_group 不会被当作剧集组参数识别"""
        group_id = "5ad0ec240e0a26303f00d84d"
        with patch("app.core.metainfo.rust_accel.find_metainfo", return_value=None):
            _, metainfo = find_metainfo(f"物语系列 {{[tmdbid=46195;type=tv;e_group={group_id};s=1]}}")
        self.assertIsNone(metainfo["episode_group"])

    def test_video_bit_extracted_for_video_title(self):
        """测试普通影视标题中的视频位深可单独识别"""
        meta = MetaInfo(title="The 355 2022 BluRay 1080p DTS-HD MA5.1 X265.10bit-BeiTai")
        self.assertEqual(meta.video_encode, "x265 10bit")
        self.assertEqual(meta.video_bit, "10bit")

    def test_video_bit_extracted_for_anime_title(self):
        """测试动漫标题中的视频位深可单独识别"""
        meta = MetaInfo(
            title="[云歌字幕组][7月新番][欢迎来到实力至上主义的教室 第二季][01]"
                  "[X264 10bit][1080p][简体中文].mp4"
        )
        self.assertEqual(meta.video_encode, "X264")
        self.assertEqual(meta.video_bit, "10bit")

    def test_streaming_platform_word_kept_in_movie_title(self):
        """测试正式片名中的流媒体平台词不会被预置清理规则移除"""
        with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
            meta = MetaInfo(title="Amazon Forever 2004 1080p WEB-DL")
        self.assertEqual(meta.name, "Amazon Forever")
        self.assertEqual(meta.year, "2004")

    def test_emby_tmdbid_overrides_braced_metainfo_tmdbid(self):
        """
        同时存在内嵌元信息和 Emby [tmdbid] 标签时，保持历史上的 [tmdbid] 优先级。
        """
        title, metainfo = find_metainfo("Movie {[tmdbid=111;type=movies]} [tmdbid=222]")
        self.assertEqual(metainfo["tmdbid"], "222")
        self.assertNotIn("[tmdbid=222]", title)

    def test_metainfopath_auxiliary_chinese_stem_uses_parent_title(self):
        """
        文件名为简英双语/特效等压制标签、父目录为拉丁片名时，应合并父目录标题与年份。
        """
        path = Path(
            "/Marty Supreme 2025 2160p DoVi HDR Atmos TrueHD 7.1 x265-PbK/简英双语特效.mp4"
        )
        meta = MetaInfoPath(path)
        self.assertEqual(meta.en_name, "Marty Supreme")
        self.assertEqual(meta.year, "2025")
        self.assertEqual(meta.original_name, "Marty Supreme")

    def test_metainfopath_chinese_parent_not_replaced_by_auxiliary_rule(self):
        """
        纯中文父目录（无拉丁字母）时不触发辅助文件名规则，避免误伤。
        """
        path = Path("/movies/流浪地球 (2023)/简体中字.mkv")
        meta = MetaInfoPath(path)
        self.assertTrue(meta.cn_name)
        self.assertIn("简体", meta.cn_name)

    def test_metainfopath_cn_title_containing_keyword_not_cleared(self):
        """
        中文片名恰好包含辅助关键词子串时（如"粤语残片"含"粤语"），
        不应被当作辅助标签清空。
        """
        path = Path("/Some Movie 2024/粤语残片.mkv")
        meta = MetaInfoPath(path)
        # stem 含有非关键词汉字"残片"，不应被全量匹配命中
        self.assertIn("粤语残片", meta.cn_name)
