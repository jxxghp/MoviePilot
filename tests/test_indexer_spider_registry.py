from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.registry import (
    SPIDER_REGISTRY,
    build_search_kwargs,
    resolve_spider_class,
)
from app.modules.indexer.spider.rousi import RousiSpider
from app.modules.indexer.spider.sunnypt import SunnyPTSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider


def test_every_registered_parser_resolves_to_its_spider_class():
    """
    每个已注册的 parser 标识都应解析到对应的专用 Spider 类。
    """
    expected = {
        "TNodeSpider": TNodeSpider,
        "TorrentLeech": TorrentLeech,
        "mTorrent": MTorrentSpider,
        "Yema": YemaSpider,
        "Haidan": HaiDanSpider,
        "HDDolby": HddolbySpider,
        "RousiPro": RousiSpider,
        "SunnyPT": SunnyPTSpider,
    }
    assert set(SPIDER_REGISTRY) == set(expected)
    for parser, spider_cls in expected.items():
        assert resolve_spider_class(parser) is spider_cls


def test_unknown_parser_falls_back_to_generic_site_spider():
    """
    未注册的 parser 标识以及缺省值都应回落到通用模板爬虫 SiteSpider。
    """
    assert resolve_spider_class("some-unregistered-parser") is SiteSpider
    assert resolve_spider_class(None) is SiteSpider
    assert resolve_spider_class("") is SiteSpider


def test_build_search_kwargs_matches_each_spider_search_signature():
    """
    过滤后的搜索参数应与各 Spider 类 search 方法实际声明的形参一致。
    """
    # SunnyPT/RousiPro 的 search 同时接受 keyword、mtype、cat、page
    full_kwargs = build_search_kwargs(
        SunnyPTSpider, keyword="k", mtype=None, cat="c", page=2
    )
    assert full_kwargs == {"keyword": "k", "mtype": None, "cat": "c", "page": 2}

    # Haidan 的 search 不接受 cat 与 page
    haidan_kwargs = build_search_kwargs(
        HaiDanSpider, keyword="k", mtype=None, cat="c", page=2
    )
    assert haidan_kwargs == {"keyword": "k", "mtype": None}

    # TNodeSpider 的 search 只接受 keyword 与 page
    tnode_kwargs = build_search_kwargs(
        TNodeSpider, keyword="k", mtype=None, cat="c", page=2
    )
    assert tnode_kwargs == {"keyword": "k", "page": 2}
