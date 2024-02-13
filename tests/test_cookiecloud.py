# -*- coding: utf-8 -*-

from unittest import TestCase

from app.chain.site import SiteChain


class CookieCloudTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_cookiecloud(self):
        result = SiteChain().sync_cookies()
        self.assertTrue(result[0])
