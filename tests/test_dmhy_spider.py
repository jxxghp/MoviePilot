from datetime import datetime
from urllib.parse import parse_qs, urlparse

from app.modules.indexer import IndexerModule
from app.modules.indexer.spider.dmhy import DMHYSpider
from app.schemas.types import MediaType


def _indexer(**kwargs):
    """
    构造动漫花园测试索引器。
    """
    data = {
        "id": 1,
        "name": "动漫花园",
        "domain": "https://dmhy.anoneko.com/",
        "parser": "DMHY",
        "result_num": 40,
        "timeout": 15,
    }
    data.update(kwargs)
    return data


def test_dmhy_spider_search_merges_default_and_season_pack_rss(monkeypatch):
    """
    验证搜索会合并普通 RSS 和季度全集 RSS，并对重复种子补充标签。
    """
    calls = []

    def fake_parse(self, url, **kwargs):
        """
        按 URL 返回不同 RSS 条目。
        """
        calls.append((url, kwargs))
        query = parse_qs(urlparse(url).query)
        if query.get("sort_id") == ["31"]:
            return [
                {
                    "title": "测试动画季度全集",
                    "enclosure": "magnet:?xt=urn:btih:season",
                    "link": "https://dmhy.anoneko.com/topics/view/2.html",
                    "size": "1",
                    "pubdate": datetime(2026, 7, 8, 1, 2, 3),
                },
                {
                    "title": "测试动画第一集",
                    "enclosure": "magnet:?xt=urn:btih:episode",
                    "link": "https://dmhy.anoneko.com/topics/view/1.html",
                    "size": "1",
                },
            ]
        return [
            {
                "title": "测试动画第一集",
                "enclosure": "magnet:?xt=urn:btih:episode",
                "link": "https://dmhy.anoneko.com/topics/view/1.html",
                "size": "1",
            }
        ]

    monkeypatch.setattr("app.modules.indexer.spider.dmhy.RssHelper.parse", fake_parse)

    error, torrents = DMHYSpider(_indexer()).search(
        keyword="测试 动画",
        mtype=MediaType.TV,
    )

    assert not error
    assert [torrent["title"] for torrent in torrents] == ["测试动画第一集", "测试动画季度全集"]
    assert torrents[0]["labels"] == ["季度全集"]
    assert torrents[0]["size"] == 0
    assert torrents[1]["pubdate"] == "2026-07-08 01:02:03"
    assert torrents[1]["category"] == MediaType.TV.value
    assert len(calls) == 2
    assert parse_qs(urlparse(calls[0][0]).query)["keyword"] == ["测试 动画"]
    assert parse_qs(urlparse(calls[1][0]).query)["sort_id"] == ["31"]


def test_dmhy_spider_search_returns_primary_results_when_season_pack_fails(monkeypatch):
    """
    验证季度全集补充搜索失败时，普通搜索结果仍可返回。
    """

    def fake_parse(self, url, **kwargs):
        """
        模拟季度全集 RSS 请求失败。
        """
        query = parse_qs(urlparse(url).query)
        if query.get("sort_id") == ["31"]:
            return False
        return [
            {
                "title": "测试动画第一集",
                "enclosure": "magnet:?xt=urn:btih:episode",
                "description": "<p>很长的发布正文</p>",
                "size": 12345,
            }
        ]

    monkeypatch.setattr("app.modules.indexer.spider.dmhy.RssHelper.parse", fake_parse)

    error, torrents = DMHYSpider(_indexer()).search(keyword="测试动画")

    assert not error
    assert len(torrents) == 1
    assert torrents[0]["description"] == ""
    assert torrents[0]["size"] == 12345
    assert torrents[0]["seeders"] == 0


def test_dmhy_spider_prefers_url_and_normalizes_relative_rss(monkeypatch):
    """
    站点 domain 为裸域时，应优先使用完整 url 并补齐相对 RSS 地址。
    """
    calls = []

    def fake_parse(self, url, **kwargs):
        calls.append(url)
        return []

    monkeypatch.setattr("app.modules.indexer.spider.dmhy.RssHelper.parse", fake_parse)

    error, torrents = DMHYSpider(
        _indexer(
            domain="anoneko.com",
            url="https://dmhy.anoneko.com/",
            rss="/topics/rss/rss.xml",
        )
    ).search(keyword="测试 动画")

    assert not error
    assert torrents == []
    assert calls[0].startswith("https://dmhy.anoneko.com/topics/rss/rss.xml?")
    assert parse_qs(urlparse(calls[0]).query)["keyword"] == ["测试 动画"]
    assert parse_qs(urlparse(calls[1]).query)["sort_id"] == ["31"]


def test_dmhy_spider_adds_scheme_to_bare_domain(monkeypatch):
    """
    只有裸域名时，应补齐 https 协议后生成 RSS 地址。
    """
    calls = []

    def fake_parse(self, url, **kwargs):
        calls.append(url)
        return []

    monkeypatch.setattr("app.modules.indexer.spider.dmhy.RssHelper.parse", fake_parse)

    error, torrents = DMHYSpider(
        _indexer(domain="dmhy.anoneko.com", url=None, rss=None)
    ).search(keyword="测试")

    assert not error
    assert torrents == []
    assert calls[0].startswith("https://dmhy.anoneko.com/topics/rss/rss.xml?")


def test_dmhy_spider_uses_fixed_rss_page_size_when_result_num_is_configured(monkeypatch):
    """
    动漫花园 RSS 页容量固定，不能被站点 result_num 改写。
    """

    def fake_parse(self, url, **kwargs):
        query = parse_qs(urlparse(url).query)
        if query.get("sort_id") == ["31"]:
            return []
        return [
            {
                "title": f"测试动画第{index}集",
                "enclosure": f"magnet:?xt=urn:btih:episode{index}",
                "size": "1",
            }
            for index in range(45)
        ]

    monkeypatch.setattr("app.modules.indexer.spider.dmhy.RssHelper.parse", fake_parse)

    error, torrents = DMHYSpider(_indexer(result_num=2)).search(keyword="测试动画")

    assert not error
    assert len(torrents) == 40
    assert IndexerModule.get_search_page_size(
        {"parser": "DMHY", "result_num": 2}, keyword="测试"
    ) == 40


def test_indexer_module_registers_dmhy_parser():
    """
    验证索引模块已注册动漫花园专用解析器。
    """
    assert IndexerModule.get_search_page_size({"parser": "DMHY"}, keyword="测试") == 40
