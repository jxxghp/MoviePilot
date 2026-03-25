# -*- coding: utf-8 -*-
from pathlib import Path
from unittest import TestCase

from app.core.metainfo import MetaInfo, MetaInfoPath
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
