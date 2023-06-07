# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.douban_sync import DoubanSyncChain


class DoubanSyncTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_doubansync(self):
        result = DoubanSyncChain().process()
        self.assertEqual(result[0], True)
