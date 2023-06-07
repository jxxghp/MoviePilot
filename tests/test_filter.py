# -*- coding: utf-8 -*-

from unittest import TestCase

from app.core import TorrentInfo
from app.modules.filter import FilterModule


class FilterTest(TestCase):
    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test_filter(self):
        torrent = TorrentInfo(title="The Wolf Children Ame and Yuki 2012 BluRay 1080p DTS-HDMA5.1 x265.10bit-CHD",
                              description="狼的孩子雨和雪/狼之子雨与雪/Okami kodomo no ame to yuki")
        _filter = FilterModule()
        _filter.init_module()
        result = _filter.filter_torrents(torrent_list=[torrent])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pri_order, 97)
