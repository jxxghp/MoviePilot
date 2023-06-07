# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.transfer import TransferChain


class TransferTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_transfer(self):
        result = TransferChain().process()
        self.assertEqual(result[0], True)
