# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.douban import DoubanChain


class DoubanSyncTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    @staticmethod
    def test_doubansync():
        DoubanChain().sync()
