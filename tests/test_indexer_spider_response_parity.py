import asyncio

import pytest

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider import haidan as haidan_module
from app.modules.indexer.spider import hddolby as hddolby_module
from app.modules.indexer.spider import mtorrent as mtorrent_module
from app.modules.indexer.spider import rousi as rousi_module
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.rousi import RousiSpider
from app.modules.indexer.spider.sunnypt import SunnyPTSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider
from app.schemas.types import MediaType


class _FakeResponse:
    """提供 Spider 响应判定所需的最小 HTTP 响应契约。"""

    def __init__(self, payload=None, status_code: int = 200, text: str = ""):
        """保存固定状态、JSON 数据和响应文本。"""
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def __bool__(self) -> bool:
        """按 requests.Response 的成功状态布尔语义返回结果。"""
        return self.status_code < 400

    def json(self):
        """返回预设 JSON 数据。"""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


_COMMON_INDEXER = {
    "id": "parity",
    "name": "Parity",
    "domain": "https://tracker.example/",
    "api_url": "https://api.tracker.example/v1",
    "apikey": "secret",
    "cookie": "session=secret",
    "ua": "MoviePilot-Test",
    "proxy": False,
    "category": {
        "movie": [{"id": 1}],
        "tv": [{"id": 2}],
        "music": [{"id": 3}],
    },
}


_PROCESSOR_CASES = {
    "mtorrent": {
        "processor": "_MTorrentSpider__process_response",
        "parser": "_MTorrentSpider__parse_result",
        "success": {"data": {"data": [{"marker": "hit"}]}},
        "empty": {"data": {"data": []}},
    },
    "hddolby": {
        "processor": "_HddolbySpider__process_response",
        "parser": "_HddolbySpider__parse_result",
        "success": {"data": [{"marker": "hit"}]},
        "empty": {"data": []},
        "business_error": {"error": {"message": "denied"}},
    },
    "torrentleech": {
        "processor": "_TorrentLeech__process_response",
        "parser": "_TorrentLeech__parse_result",
        "success": {"torrentList": [{"marker": "hit"}]},
        "empty": {"torrentList": []},
        "processor_args": (MediaType.MOVIE,),
    },
    "haidan": {
        "processor": "_HaiDanSpider__process_response",
        "parser": "_HaiDanSpider__parse_result",
        "success": {"code": 0, "data": {"1": {"marker": "hit"}}},
        "empty": {"code": 0, "data": {}},
        "business_error": {"code": 403, "msg": "denied"},
    },
    "rousi": {
        "processor": "_RousiSpider__process_response",
        "parser": "_RousiSpider__parse_result",
        "success": {"code": 0, "data": {"torrents": [{"marker": "hit"}]}},
        "empty": {"code": 0, "data": {"torrents": []}},
        "business_error": {"code": 403, "message": "denied"},
    },
    "sunnypt": {
        "processor": "_process_search_response",
        "parser": "_parse_result",
        "success": {"code": 0, "data": {"items": [{"marker": "hit"}]}},
        "empty": {"code": 0, "data": {"items": []}},
        "business_error": {"code": 403, "msg": "denied"},
    },
    "tnode": {
        "processor": "_TNodeSpider__process_response",
        "parser": "_TNodeSpider__parse_result",
        "success": {"data": {"torrents": [{"marker": "hit"}]}},
        "empty": {"data": {"torrents": []}},
    },
    "yema": {
        "processor": "_process_search_response",
        "parser": "_parse_result",
        "success": {"success": True, "data": [{"marker": "hit"}]},
        "empty": {"success": True, "data": []},
        "business_error": {"success": False, "errorMessage": "denied"},
    },
}


def _build_api_spider(name: str, monkeypatch, indexer=None):
    """构造不访问数据库或网络的专用 API Spider。"""
    if name in {"mtorrent", "hddolby", "haidan", "rousi"}:
        module = {
            "mtorrent": mtorrent_module,
            "hddolby": hddolby_module,
            "haidan": haidan_module,
            "rousi": rousi_module,
        }[name]
        monkeypatch.setattr(module, "get_configured_system_config", lambda: None)
    spider_class = {
        "mtorrent": MTorrentSpider,
        "hddolby": HddolbySpider,
        "torrentleech": TorrentLeech,
        "haidan": HaiDanSpider,
        "rousi": RousiSpider,
        "sunnypt": SunnyPTSpider,
        "tnode": TNodeSpider,
        "yema": YemaSpider,
    }[name]
    spider_indexer = dict(_COMMON_INDEXER) if indexer is None else indexer
    if name == "tnode":
        spider = object.__new__(spider_class)
        spider.__init__(spider_indexer)
        return spider
    return spider_class(spider_indexer)


@pytest.mark.parametrize("name", _PROCESSOR_CASES)
def test_api_spider_response_processors_cover_success_empty_and_errors(
    name,
    monkeypatch,
):
    """各 API Spider 应由同一纯响应入口处理成功、空结果和失败分支。"""
    spider = _build_api_spider(name, monkeypatch)
    case = _PROCESSOR_CASES[name]
    parser_calls = []

    def parse_result(results, *_args):
        """记录处理器传入解析器的数据并返回可识别投影。"""
        parser_calls.append(results)
        return [{"title": "projected"}]

    monkeypatch.setattr(spider, case["parser"], parse_result)
    processor = getattr(spider, case["processor"])
    processor_args = case.get("processor_args", ())

    assert processor(_FakeResponse(case["success"]), *processor_args) == (
        False,
        [{"title": "projected"}],
    )
    assert len(parser_calls) == 1

    monkeypatch.undo()
    spider = _build_api_spider(name, monkeypatch)
    processor = getattr(spider, case["processor"])
    assert processor(_FakeResponse(case["empty"]), *processor_args) == (False, [])
    assert processor(_FakeResponse({}, status_code=503), *processor_args) == (True, [])
    assert processor(None, *processor_args) == (True, [])
    if business_error := case.get("business_error"):
        assert processor(_FakeResponse(business_error), *processor_args) == (True, [])


@pytest.mark.parametrize(
    "name",
    ["rousi", "sunnypt", "yema"],
)
def test_json_validating_spiders_preserve_invalid_payload_fallback(name, monkeypatch):
    """已声明 JSON 容错的 Spider 应继续把解析异常稳定降级为空错误结果。"""
    spider = _build_api_spider(name, monkeypatch)
    case = _PROCESSOR_CASES[name]
    processor = getattr(spider, case["processor"])

    assert processor(_FakeResponse(ValueError("invalid json"))) == (True, [])


@pytest.mark.parametrize(
    "name",
    ["mtorrent", "hddolby", "torrentleech", "haidan", "tnode"],
)
def test_legacy_json_exception_semantics_are_not_broadened(name, monkeypatch):
    """未提供 JSON 容错的旧 Spider 重构后仍应向调用方传播同一解析异常。"""
    spider = _build_api_spider(name, monkeypatch)
    case = _PROCESSOR_CASES[name]
    processor = getattr(spider, case["processor"])

    with pytest.raises(ValueError, match="invalid json"):
        processor(
            _FakeResponse(ValueError("invalid json")),
            *case.get("processor_args", ()),
        )


_SEARCH_CASES = {
    "mtorrent": ("post_res", "_MTorrentSpider__process_response", {"keyword": "Movie"}),
    "hddolby": ("post_res", "_HddolbySpider__process_response", {"keyword": "Movie"}),
    "torrentleech": (
        "get_res",
        "_TorrentLeech__process_response",
        {"keyword": "Movie", "mtype": MediaType.MOVIE},
    ),
    "haidan": ("get_res", "_HaiDanSpider__process_response", {"keyword": "Movie"}),
    "rousi": ("get_res", "_RousiSpider__process_response", {"keyword": "Movie"}),
    "sunnypt": ("get_res", "_process_search_response", {"keyword": "Movie"}),
    "tnode": ("post_res", "_TNodeSpider__process_response", {"keyword": "Movie"}),
    "yema": ("post_res", "_process_search_response", {"keyword": "Movie"}),
}


@pytest.mark.parametrize("name", _SEARCH_CASES)
def test_api_spider_sync_async_entries_share_response_projection(name, monkeypatch):
    """同步和异步 HTTP 边界应把响应交给同一个状态与结果投影入口。"""
    spider = _build_api_spider(name, monkeypatch)
    io_method, processor_name, search_kwargs = _SEARCH_CASES[name]
    response = object()
    processor_calls = []

    def process_response(actual_response, *_args):
        """记录同步和异步入口交付的原始响应对象。"""
        processor_calls.append(actual_response)
        return False, [{"title": "projected"}]

    def sync_request(*_args, **_kwargs):
        """回放同步 HTTP 响应。"""
        return response

    async def async_request(*_args, **_kwargs):
        """回放异步 HTTP 响应。"""
        return response

    monkeypatch.setattr(spider, processor_name, process_response)
    monkeypatch.setattr(RequestUtils, io_method, sync_request)
    monkeypatch.setattr(AsyncRequestUtils, io_method, async_request)
    if name == "tnode":
        monkeypatch.setattr(spider, "_TNodeSpider__get_token", lambda: "token")

        async def async_token():
            """返回与同步入口相同的固定 CSRF 令牌。"""
            return "token"

        monkeypatch.setattr(spider, "_TNodeSpider__async_get_token", async_token)

    sync_result = spider.search(**search_kwargs)
    async_result = asyncio.run(spider.async_search(**search_kwargs))

    assert sync_result == async_result == (False, [{"title": "projected"}])
    assert processor_calls == [response, response]


def test_tnode_token_projection_is_identical_for_sync_and_async_responses(monkeypatch):
    """TNode 两种首页请求应共享同一 CSRF 文本解析和无效状态判定。"""
    spider = _build_api_spider("tnode", monkeypatch)
    valid = _FakeResponse(text='<meta name="x-csrf-token" content="same-token">')
    invalid = _FakeResponse(text='<meta name="x-csrf-token" content="ignored">', status_code=403)
    processor = spider._TNodeSpider__parse_token_response

    assert processor(valid) == "same-token"
    assert processor(invalid) is None
    assert processor(None) is None


@pytest.mark.parametrize(
    ("name", "credential", "search_kwargs"),
    [
        ("mtorrent", "apikey", {"keyword": "Movie"}),
        ("haidan", "cookie", {"keyword": "Movie"}),
        ("rousi", "apikey", {"keyword": "Movie"}),
        ("sunnypt", "apikey", {"keyword": "Movie"}),
        ("yema", "apikey", {"keyword": "Movie"}),
    ],
)
def test_missing_credentials_reject_sync_and_async_search_without_network(
    name,
    credential,
    search_kwargs,
    monkeypatch,
):
    """具备本地认证门禁的 Spider 两种入口都应在 HTTP 前返回错误。"""
    indexer = {**_COMMON_INDEXER, credential: None}
    spider = _build_api_spider(name, monkeypatch, indexer=indexer)

    def unexpected_request(*_args, **_kwargs):
        """认证缺失时若触发同步网络请求则立即失败。"""
        pytest.fail("missing credentials must skip sync network I/O")

    async def unexpected_async_request(*_args, **_kwargs):
        """认证缺失时若触发异步网络请求则立即失败。"""
        pytest.fail("missing credentials must skip async network I/O")

    monkeypatch.setattr(RequestUtils, "get_res", unexpected_request)
    monkeypatch.setattr(RequestUtils, "post_res", unexpected_request)
    monkeypatch.setattr(AsyncRequestUtils, "get_res", unexpected_async_request)
    monkeypatch.setattr(AsyncRequestUtils, "post_res", unexpected_async_request)

    assert spider.search(**search_kwargs) == (True, [])
    assert asyncio.run(spider.async_search(**search_kwargs)) == (True, [])


def test_tnode_and_torrentleech_preflight_failures_skip_network(monkeypatch):
    """令牌缺失和不支持中文的入口都应在同步、异步 HTTP 前终止。"""
    tnode = _build_api_spider("tnode", monkeypatch)
    torrentleech = _build_api_spider("torrentleech", monkeypatch)

    def no_token():
        """回放同步令牌缺失。"""
        return None

    async def no_async_token():
        """回放异步令牌缺失。"""
        return None

    monkeypatch.setattr(tnode, "_TNodeSpider__get_token", no_token)
    monkeypatch.setattr(tnode, "_TNodeSpider__async_get_token", no_async_token)

    assert tnode.search(keyword="Movie") == (True, [])
    assert asyncio.run(tnode.async_search(keyword="Movie")) == (True, [])
    assert torrentleech.search(keyword="中文") == (True, [])
    assert asyncio.run(torrentleech.async_search(keyword="中文")) == (True, [])


def test_site_spider_sync_async_share_admission_and_decoding(monkeypatch):
    """普通页面 Spider 仅保留 HTTP 与线程执行差异，准入和解码投影保持一致。"""
    indexer = {
        "id": "generic",
        "name": "Generic",
        "domain": "https://tracker.example/",
        "search": {"paths": [{"path": "browse.php"}]},
        "torrents": {"list": {}, "fields": {}},
    }
    response = _FakeResponse(text="encoded-body")
    parsed = []

    def sync_request(*_args, **_kwargs):
        """回放普通页面同步响应。"""
        return response

    async def async_request(*_args, **_kwargs):
        """回放普通页面异步响应。"""
        return response

    def decode(actual_response, **_kwargs):
        """记录统一解码入口收到的响应。"""
        assert actual_response is response
        return actual_response.text

    def parse(_self, text):
        """记录两种入口交付的相同解析文本。"""
        parsed.append(text)
        return [{"title": text}]

    monkeypatch.setattr(RequestUtils, "get_res", sync_request)
    monkeypatch.setattr(AsyncRequestUtils, "get_res", async_request)
    monkeypatch.setattr(RequestUtils, "get_decoded_html_content", staticmethod(decode))
    monkeypatch.setattr(SiteSpider, "parse", parse)
    spider = SiteSpider(indexer=indexer, keyword="Movie")

    sync_result = spider.get_torrents()
    async_result = asyncio.run(spider.async_get_torrents())

    assert sync_result == async_result == [{"title": "encoded-body"}]
    assert parsed == ["encoded-body", "encoded-body"]

    rejected = SiteSpider(indexer={**indexer, "media_type": "music"}, mtype=MediaType.MOVIE)
    assert rejected.get_torrents() == []
    assert asyncio.run(rejected.async_get_torrents()) == []
