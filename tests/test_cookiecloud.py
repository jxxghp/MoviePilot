# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.cookiecloud import CookieCloudChain


class CookieCloudTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_cookiecloud(self):
        result = CookieCloudChain().process()
        self.assertEqual(result[0], True)
