# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.identify import IdentifyChain


class RecognizeTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_recognize(self):
        result = IdentifyChain().process(title="我和我的祖国 2019")
        self.assertEqual(str(result.media_info.tmdb_id), '612845')
