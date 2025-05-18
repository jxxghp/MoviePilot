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
                meta_info = MetaInfo(title=info.get("title"), subtitle=info.get("subtitle"))
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
                "audio_codec": meta_info.audio_encode or ""
            }

            # 检查tmdbid
            if info.get("target").get("tmdbid"):
                target["tmdbid"] = meta_info.tmdbid

            self.assertEqual(target, info.get("target"))

    def test_emby_format_ids(self):
        """
        测试Emby格式ID识别
        """
        # 测试文件路径
        test_paths = [
            # 文件名中包含tmdbid
            ("/movies/The Vampire Diaries (2009) [tmdbid=18165]/The.Vampire.Diaries.S01E01.1080p.mkv", 18165),
            # 目录名中包含tmdbid
            ("/movies/Inception (2010) [tmdbid-27205]/Inception.2010.1080p.mkv", 27205),
            # 父目录名中包含tmdbid
            ("/movies/Breaking Bad (2008) [tmdb=1396]/Season 1/Breaking.Bad.S01E01.1080p.mkv", 1396),
            # 祖父目录名中包含tmdbid
            ("/tv/Game of Thrones (2011) {tmdb=1399}/Season 1/Game.of.Thrones.S01E01.1080p.mkv", 1399),
            # 测试{tmdb-xxx}格式
            ("/movies/Avatar (2009) {tmdb-19995}/Avatar.2009.1080p.mkv", 19995),
        ]

        for path_str, expected_tmdbid in test_paths:
            meta = MetaInfoPath(Path(path_str))
            self.assertEqual(meta.tmdbid, expected_tmdbid,
                             f"路径 {path_str} 期望的tmdbid为 {expected_tmdbid}，实际识别为 {meta.tmdbid}")
