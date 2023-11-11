# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfo


class RecognizeTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_recognize(self):
        media_info = MediaChain().recognize_by_meta(MetaInfo("我和我的祖国 2019"))
        self.assertEqual(media_info.tmdb_id, 612845)
        exists = DownloadChain().get_no_exists_info(MetaInfo("我和我的祖国 2019"), media_info)
        self.assertTrue(exists[0])
