"""RSS 索引在 HTTP 解码后应与网页索引共用同步、异步和原生解析入口。"""

import asyncio
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from requests import Response

from app.modules.indexer import spider as spider_module
from app.modules.indexer.spider import SiteSpider

RSS_BODY = """<rss version="2.0"><channel><title>索引</title>
<item><title>示例番剧 01 &amp; SP [1080p]</title>
<pubDate>Sat, 03 Oct 2026 10:20:30 +0800</pubDate>
<description><![CDATA[<p>详情介绍</p>]]></description>
<torrent xmlns="https://example.com/torrent"><pubDate>2026-10-03T10:20:30+08:00</pubDate></torrent>
<enclosure url="https://example.com/one.torrent" length="123456789" type="application/x-bittorrent" />
</item><item><title>示例番剧 02 [1080p]</title>
<torrent xmlns="https://example.com/torrent"><pubDate>2026-10-03T11:20:30+08:00</pubDate></torrent>
<enclosure url="https://example.com/two.torrent" length="987654321" type="application/x-bittorrent" />
</item></channel></rss>"""


@pytest.fixture
def rss_indexer():
    """构造与资源包约定一致的 RSS 选择器，避免依赖本机资源扩展。"""
    return {
        "id": "rss-example",
        "name": "RSS 示例",
        "domain": "https://example.com/",
        "search": {"paths": [{"path": "rss.xml"}], "params": {"q": "{keyword}"}},
        "torrents": {
            "list": {"selector": "item"},
            "fields": {
                "title": {"selector": "title"},
                "download": {"selector": "enclosure", "attribute": "url"},
                "size": {"selector": "enclosure", "attribute": "length"},
                "date_added": {
                    "selector": "torrent pubdate",
                    "filters": [
                        {"name": "re_search", "args": [r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", 0]},
                        {"name": "replace", "args": ["T", " "]},
                    ],
                },
            },
        },
    }


@pytest.fixture(params=["python", "rust"])
def parser_engine(request, monkeypatch):
    """显式选择真实 Python 或 Rust 解析器，原生依赖缺失时只跳过原生路径。"""
    if request.param == "python":
        monkeypatch.setattr(spider_module.rust_accel, "parse_indexer_torrents", Mock(return_value=None))
        return
    native = pytest.importorskip("moviepilot_rust")

    def parse_native(**kwargs):
        """直接调用已安装的 Rust 引擎，避免配置开关让原生测试落到 Python。"""
        return native.parse_indexer_torrents_fast(
            kwargs["html_text"], kwargs["domain"], kwargs["list_config"],
            kwargs["fields"], kwargs["category"], kwargs["result_num"],
        )

    monkeypatch.setattr(spider_module.rust_accel, "parse_indexer_torrents", parse_native)


@pytest.mark.usefixtures("parser_engine")
@pytest.mark.parametrize("declaration", [
    "",
    '<?xml version="1.0" encoding="UTF-8"?>',
    "\ufeff  <?xml version='1.0' encoding='GBK'?>",
])
def test_rss_indexer_preserves_decoded_text_and_item_fields(rss_indexer, declaration):
    """已解码内容不再使用 XML 声明二次解码，混合大小写日期和多条种子均完整保留。"""
    spider = SiteSpider(rss_indexer)
    rows = spider.parse(declaration + RSS_BODY)

    assert not spider.is_error
    assert [(row["title"], row["enclosure"], row["size"], row["pubdate"]) for row in rows] == [
        ("示例番剧 01 & SP [1080p]", "https://example.com/one.torrent", 123456789, "2026-10-03 10:20:30"),
        ("示例番剧 02 [1080p]", "https://example.com/two.torrent", 987654321, "2026-10-03 11:20:30"),
    ]


@pytest.mark.usefixtures("parser_engine")
@pytest.mark.parametrize("asynchronous", [False, True])
def test_rss_indexer_http_paths_decode_xml_response(rss_indexer, monkeypatch, asynchronous):
    """真实响应解码之后，同步和异步请求都能解析公开 RSS 并正确编码搜索关键词。"""
    response = Response()
    response.status_code = 200
    response.headers["Content-Type"] = "application/rss+xml; charset=utf-8"
    response.encoding = "utf-8"
    response._content = ('<?xml version="1.0" encoding="UTF-8"?>' + RSS_BODY).encode("utf-8")
    requester = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    transport = spider_module.AsyncRequestUtils if asynchronous else spider_module.RequestUtils
    monkeypatch.setattr(transport, "get_res", requester)
    keyword = "示例 & SP"
    spider = SiteSpider(rss_indexer, keyword=keyword)

    rows = asyncio.run(spider.async_get_torrents()) if asynchronous else spider.get_torrents()

    assert len(rows) == 2
    assert rows[0]["title"] == "示例番剧 01 & SP [1080p]"
    assert not spider.is_error
    query = parse_qs(urlparse(requester.call_args.args[0]).query)
    assert query["q"] == [keyword]


@pytest.mark.usefixtures("parser_engine")
def test_empty_rss_channel_is_valid(rss_indexer):
    """公开接口没有匹配资源时，空 channel 应作为成功的空结果。"""
    spider = SiteSpider(rss_indexer)
    assert spider.parse('<?xml version="1.0" encoding="UTF-8"?><rss><channel /></rss>') == []
    assert not spider.is_error
