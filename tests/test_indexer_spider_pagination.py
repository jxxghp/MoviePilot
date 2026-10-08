"""通用站点没有服务端页码时，从完整响应中提取互不重叠的搜索页。"""

import asyncio
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from requests import Response

from app.modules.indexer import IndexerModule
from app.modules.indexer import spider as spider_module
from app.modules.indexer.spider import SiteSpider


@pytest.fixture
def indexer():
    """按 MiKan 的搜索路径与表格选择器构造离线配置。"""
    return {
        "id": "mikanani",
        "name": "MiKan",
        "domain": "https://mikan.example/",
        "search": {"paths": [{"path": "Home/Search?searchstr={keyword}"}]},
        "torrents": {
            "list": {"selector": "div.episode-table > table > tbody > tr.js-search-results-row"},
            "fields": {
                "title": {"selector": "td:nth-child(2) > a.magnet-link-wrap"},
                "download": {
                    "selector": "td:nth-child(2) > a.js-magnet",
                    "attribute": "data-clipboard-text",
                },
                "size": {"selector": "td:nth-child(3)"},
            },
        },
    }


@pytest.fixture(params=["python", "rust"])
def parser_engine(request, monkeypatch):
    """分别验证 Python 回退和真实 Rust 引擎，避免开关掩盖原生路径。"""
    if request.param == "python":
        monkeypatch.setattr(spider_module.rust_accel, "parse_indexer_torrents", Mock(return_value=None))
        return
    native = pytest.importorskip("moviepilot_rust")

    def parse_native(**kwargs):
        """将同一窗口交给已安装的 Rust 解析入口。"""
        return native.parse_indexer_torrents_fast(
            kwargs["html_text"], kwargs["domain"], kwargs["list_config"],
            kwargs["fields"], kwargs["category"], kwargs["result_num"],
        )

    monkeypatch.setattr(spider_module.rust_accel, "parse_indexer_torrents", parse_native)


def _response(count: int = 250) -> Response:
    """生成一次返回多页结果的响应，每条标题携带可断言的原始行号。"""
    rows = "".join(
        f'<tr class="js-search-results-row"><td>{i}</td><td>'
        f'<a class="magnet-link-wrap">番剧 {i:03d}</a>'
        f'<a class="js-magnet" data-clipboard-text="magnet:?xt=urn:btih:{i:040x}"></a>'
        '</td><td>1 GB</td></tr>'
        for i in range(count)
    )
    response = Response()
    response.status_code = 200
    response.encoding = "utf-8"
    response._content = (
        f'<html><title>搜索结果</title><div class="episode-table"><table><tbody>{rows}'
        '</tbody></table></div></html>'
    ).encode("utf-8")
    return response


@pytest.mark.usefixtures("parser_engine")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("page, start, stop", [(0, 0, 100), (1, 100, 200), (2, 200, 250), (3, 250, 250)])
def test_unpaged_keyword_search_returns_requested_window(indexer, monkeypatch, asynchronous, page, start, stop):
    """相同响应在两种请求与解析路径中产生独立页、尾页和成功空页。"""
    requester = AsyncMock(return_value=_response()) if asynchronous else Mock(return_value=_response())
    transport = spider_module.AsyncRequestUtils if asynchronous else spider_module.RequestUtils
    monkeypatch.setattr(transport, "get_res", requester)
    spider = SiteSpider(indexer, keyword="魔女 & SP", page=page)

    rows = asyncio.run(spider.async_get_torrents()) if asynchronous else spider.get_torrents()

    assert [row["title"] for row in rows] == [f"番剧 {i:03d}" for i in range(start, stop)]
    assert not spider.is_error
    query = parse_qs(urlparse(requester.call_args.args[0]).query)
    assert query == {"searchstr": ["魔女 & SP"]}
    assert IndexerModule.get_search_page_size(indexer, keyword="魔女 & SP") == 100


@pytest.mark.usefixtures("parser_engine")
def test_unpaged_search_uses_configured_page_size(indexer, monkeypatch):
    """本地窗口使用站点 result_num，与搜索编排的单页容量一致。"""
    indexer["result_num"] = 50
    monkeypatch.setattr(spider_module.RequestUtils, "get_res", Mock(return_value=_response()))

    rows = SiteSpider(indexer, keyword="魔女", page=2).get_torrents()

    assert [row["title"] for row in rows] == [f"番剧 {i:03d}" for i in range(100, 150)]
    assert IndexerModule.get_search_page_size(indexer, keyword="魔女") == 50


@pytest.mark.usefixtures("parser_engine")
@pytest.mark.parametrize("search", [
    {"paths": [{"path": "search?q={keyword}&page={page}"}]},
    {"paths": [{"path": "search"}], "params": {"q": "{keyword}"}},
])
def test_server_paged_search_keeps_response_first_window(indexer, monkeypatch, search):
    """路径占位符和通用查询参数已传递页码时，不再对返回页应用偏移。"""
    indexer["search"] = search
    requester = Mock(return_value=_response())
    monkeypatch.setattr(spider_module.RequestUtils, "get_res", requester)

    rows = SiteSpider(indexer, keyword="魔女", page=2).get_torrents()

    assert [row["title"] for row in rows] == [f"番剧 {i:03d}" for i in range(100)]
    assert parse_qs(urlparse(requester.call_args.args[0]).query)["page"] == ["2"]


@pytest.mark.usefixtures("parser_engine")
def test_browse_with_server_page_keeps_response_first_window(indexer, monkeypatch):
    """无关键词浏览仍按浏览路径的服务端页码处理响应。"""
    indexer["browse"] = {"path": "Home/Classic/{page}", "start": 1}
    requester = Mock(return_value=_response())
    monkeypatch.setattr(spider_module.RequestUtils, "get_res", requester)

    rows = SiteSpider(indexer, page=2).get_torrents()

    assert len(rows) == 100 and rows[0]["title"] == "番剧 000"
    assert urlparse(requester.call_args.args[0]).path == "/Home/Classic/3"
