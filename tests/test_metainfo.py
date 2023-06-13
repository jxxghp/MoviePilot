# -*- coding: utf-8 -*-

from unittest import TestCase

from app.core.metainfo import MetaInfo
from tests.cases.meta import meta_cases


class MetaInfoTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_metainfo(self):
        for info in meta_cases:
            if not info.get("title"):
                continue
            meta_info = MetaInfo(title=info.get("title"), subtitle=info.get("subtitle"))
            target = {
                "type": meta_info.type.value,
                "cn_name": meta_info.cn_name or "",
                "en_name": meta_info.en_name or "",
                "year": meta_info.year or "",
                "part": meta_info.part or "",
                "season": meta_info.season,
                "episode": meta_info.episode,
                "restype": meta_info.edtion,
                "pix": meta_info.resource_pix or "",
                "video_codec": meta_info.video_encode or "",
                "audio_codec": meta_info.audio_encode or ""
            }
            self.assertEqual(target, info.get("target"))
