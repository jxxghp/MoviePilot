# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.download import DownloadChain
from app.chain.identify import IdentifyChain
from app.core.metainfo import MetaInfo


class RecognizeTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_recognize(self):
        result = IdentifyChain().process(title="我和我的祖国 2019")
        self.assertEqual(result.media_info.tmdb_id, 612845)
        exists = DownloadChain().get_no_exists_info(MetaInfo("我和我的祖国 2019"), result.media_info)
        self.assertTrue(exists[0])
