"""
验证各站点爬虫对响应异常和空结果的处理一致性。
"""

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
    """
    提供 Spider 响应判定所需的最小 HTTP 响应契约。
    """

    def __init__(self, payload=None, status_code: int = 200, text: str = ""):
        """
        保存固定状态、JSON 数据和响应文本。
        """
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def __bool__(self) -> bool:
        """
        按 requests.Response 的成功状态布尔语义返回结果。
        """
        return self.status_code < 400

    def json(self):
        """
        返回预设 JSON 数据。
        """
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
        "success": {"code": "0", "data": {"data": [{"marker": "hit"}]}},
        "empty": {"code": "0", "data": {"data": []}},
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
    """
    构造不访问数据库或网络的专用 API Spider。
    """
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
    """
    各 API Spider 应由同一纯响应入口处理成功、空结果和失败分支。
    """
    spider = _build_api_spider(name, monkeypatch)
    case = _PROCESSOR_CASES[name]
    parser_calls = []

    def parse_result(results, *_args):
        """
        记录处理器传入解析器的数据并返回可识别投影。
        """
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
    """
    已声明 JSON 容错的 Spider 应继续把解析异常稳定降级为空错误结果。
    """
    spider = _build_api_spider(name, monkeypatch)
    case = _PROCESSOR_CASES[name]
    processor = getattr(spider, case["processor"])

    assert processor(_FakeResponse(ValueError("invalid json"))) == (True, [])


@pytest.mark.parametrize(
    "name",
    ["mtorrent", "hddolby", "torrentleech", "haidan", "tnode"],
)
def test_legacy_json_exception_semantics_are_not_broadened(name, monkeypatch):
    """
    未提供 JSON 容错的旧 Spider 重构后仍应向调用方传播同一解析异常。
    """
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
    """
    同步和异步 HTTP 边界应把响应交给同一个状态与结果投影入口。
    """
    spider = _build_api_spider(name, monkeypatch)
    io_method, processor_name, search_kwargs = _SEARCH_CASES[name]
    response = object()
    processor_calls = []

    def process_response(actual_response, *_args):
        """
        记录同步和异步入口交付的原始响应对象。
        """
        processor_calls.append(actual_response)
        return False, [{"title": "projected"}]

    def sync_request(*_args, **_kwargs):
        """
        回放同步 HTTP 响应。
        """
        return response

    async def async_request(*_args, **_kwargs):
        """
        回放异步 HTTP 响应。
        """
        return response

    monkeypatch.setattr(spider, processor_name, process_response)
    monkeypatch.setattr(RequestUtils, io_method, sync_request)
    monkeypatch.setattr(AsyncRequestUtils, io_method, async_request)
    if name == "tnode":
        monkeypatch.setattr(spider, "_TNodeSpider__get_token", lambda: "token")

        async def async_token():
            """
            返回与同步入口相同的固定 CSRF 令牌。
            """
            return "token"

        monkeypatch.setattr(spider, "_TNodeSpider__async_get_token", async_token)

    sync_result = spider.search(**search_kwargs)
    async_result = asyncio.run(spider.async_search(**search_kwargs))

    assert sync_result == async_result == (False, [{"title": "projected"}])
    assert processor_calls == [response, response]


def test_tnode_token_projection_is_identical_for_sync_and_async_responses(monkeypatch):
    """
    TNode 两种首页请求应共享同一 CSRF 文本解析和无效状态判定。
    """
    spider = _build_api_spider("tnode", monkeypatch)
    valid = _FakeResponse(text='<meta name="x-csrf-token" content="same-token">')
    invalid = _FakeResponse(text='<meta name="x-csrf-token" content="ignored">', status_code=403)
    processor = spider._TNodeSpider__parse_token_response

    assert processor(valid) == "same-token"
    assert processor(invalid) is None
    assert processor(None) is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("changed", ["domain", "cookie", "ua"])
def test_tnode_search_caches_tokens_per_current_connection_identity(tmp_path, monkeypatch, asynchronous, changed):
    """
    真实搜索入口在站点、Cookie 或UA变化后使用各自CSRF，重复搜索复用各自缓存。
    """
    first_config = {**_COMMON_INDEXER, "cookie": f"session={tmp_path}"}
    second_config = {**first_config, changed: {
        "domain": "https://other.tracker.example/",
        "cookie": f"session=other-{tmp_path}",
        "ua": "Other-MoviePilot-Test",
    }[changed]}
    identities = [(config["domain"], config["cookie"], config["ua"]) for config in (first_config, second_config)]
    tokens = dict(zip(identities, ("first-token", "second-token")))
    pages, submitted = [], []

    def http_client(*_args, **kwargs):
        """
        只替换HTTP边界，令牌缓存、首页解析和搜索请求组装均由真实实现执行。
        """
        def get_res(url):
            """
            按当前连接身份返回独立令牌，并记录实际首页请求。
            """
            identity = (url, kwargs["cookies"], kwargs["ua"])
            pages.append(identity)
            return _FakeResponse(text=f'<meta name="x-csrf-token" content="{tokens[identity]}">')

        def post_res(url, json):
            """
            记录搜索提交的令牌和页码，返回合法空结果。
            """
            submitted.append((url, {key.lower(): value for key, value in kwargs["headers"].items()}, json))
            return _FakeResponse({"data": {"torrents": []}})

        async def async_get_res(url):
            """
            在异步搜索入口复用同一首页响应合同。
            """
            return get_res(url)

        async def async_post_res(url, json):
            """
            在异步搜索入口记录同一请求合同。
            """
            return post_res(url, json)

        from types import SimpleNamespace

        return SimpleNamespace(get_res=async_get_res if asynchronous else get_res,
            post_res=async_post_res if asynchronous else post_res)

    monkeypatch.setattr("app.modules.indexer.spider.tnode.AsyncRequestUtils" if asynchronous else
        "app.modules.indexer.spider.tnode.RequestUtils", http_client)
    first, second = TNodeSpider(first_config), TNodeSpider(second_config)
    for spider in (first, second, first, second):
        result = asyncio.run(spider.async_search("Movie", page=2)) if asynchronous else spider.search("Movie", page=2)
        assert result == (False, [])

    assert pages == identities
    assert [headers["x-csrf-token"] for _url, headers, _params in submitted] == ["first-token", "second-token"] * 2
    assert [params["page"] for _url, _headers, params in submitted] == [3] * 4


@pytest.mark.parametrize("asynchronous", [False, True])
def test_tnode_invalid_homepage_is_retried_before_search(tmp_path, monkeypatch, asynchronous):
    """
    首页拒绝不能缓存为成功或提交搜索，重试取得有效令牌后才执行搜索。
    """
    from types import SimpleNamespace

    responses = [_FakeResponse(status_code=403), _FakeResponse(text='<meta name="x-csrf-token" content="renewed">')]
    pages, submitted = [], []

    def http_client(*_args, **kwargs):
        """
        模拟首页先拒绝后成功的传输边界，保留真实令牌重试逻辑。
        """
        def get_res(url):
            """
            依次返回拒绝和恢复响应，并记录重试次数。
            """
            pages.append(url)
            return responses.pop(0)

        def post_res(url, json):
            """
            记录取得有效令牌后才允许发起的搜索。
            """
            del url, json
            submitted.append(kwargs["headers"])
            return _FakeResponse({"data": {"torrents": []}})

        async def async_get_res(url):
            """
            在异步入口复用可恢复的首页响应序列。
            """
            return get_res(url)

        async def async_post_res(url, json):
            """
            在异步入口记录取得有效令牌后的搜索。
            """
            return post_res(url, json)

        return SimpleNamespace(get_res=async_get_res if asynchronous else get_res,
            post_res=async_post_res if asynchronous else post_res)

    monkeypatch.setattr("app.modules.indexer.spider.tnode.AsyncRequestUtils" if asynchronous else
        "app.modules.indexer.spider.tnode.RequestUtils", http_client)
    spider = TNodeSpider({**_COMMON_INDEXER, "cookie": f"retry={tmp_path}"})
    for expected in [(True, []), (False, []), (False, [])]:
        result = asyncio.run(spider.async_search("Movie")) if asynchronous else spider.search("Movie")
        assert result == expected
    assert pages == [_COMMON_INDEXER["domain"]] * 2
    assert len(submitted) == 2
    assert all({key.lower(): value for key, value in headers.items()}["x-csrf-token"] == "renewed"
               for headers in submitted)


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
    """
    具备本地认证门禁的 Spider 两种入口都应在 HTTP 前返回错误。
    """
    indexer = {**_COMMON_INDEXER, credential: None}
    spider = _build_api_spider(name, monkeypatch, indexer=indexer)

    def unexpected_request(*_args, **_kwargs):
        """
        认证缺失时若触发同步网络请求则立即失败。
        """
        pytest.fail("missing credentials must skip sync network I/O")

    async def unexpected_async_request(*_args, **_kwargs):
        """
        认证缺失时若触发异步网络请求则立即失败。
        """
        pytest.fail("missing credentials must skip async network I/O")

    monkeypatch.setattr(RequestUtils, "get_res", unexpected_request)
    monkeypatch.setattr(RequestUtils, "post_res", unexpected_request)
    monkeypatch.setattr(AsyncRequestUtils, "get_res", unexpected_async_request)
    monkeypatch.setattr(AsyncRequestUtils, "post_res", unexpected_async_request)

    assert spider.search(**search_kwargs) == (True, [])
    assert asyncio.run(spider.async_search(**search_kwargs)) == (True, [])


def test_yema_sync_async_entries_share_request_plan_and_result_finalizer(
    monkeypatch,
):
    """
    Yema 分类搜索的两种入口仅替换 HTTP，规划和归并决策必须同源。
    """
    spider = _build_api_spider("yema", monkeypatch)
    prepare = []
    finalized = []
    original_prepare = spider._prepare_search_requests

    def prepare_search_requests(**kwargs):
        """
        记录两种入口交给共享请求规划器的参数。
        """
        prepare.append(kwargs)
        return original_prepare(**kwargs)

    def finalize_search_responses(responses):
        """
        记录两种入口交给共享结果归并器的响应序列。
        """
        finalized.append(responses)
        return False, [{"title": "projected"}]

    def sync_request(*_args, **kwargs):
        """
        返回可按分类请求体识别的同步响应。
        """
        return ("sync", kwargs["json"]["categoryId"])

    async def async_request(*_args, **kwargs):
        """
        返回可按分类请求体识别的异步响应。
        """
        return ("async", kwargs["json"]["categoryId"])

    monkeypatch.setattr(spider, "_prepare_search_requests", prepare_search_requests)
    monkeypatch.setattr(spider, "_finalize_search_responses", finalize_search_responses)
    monkeypatch.setattr(RequestUtils, "post_res", sync_request)
    monkeypatch.setattr(AsyncRequestUtils, "post_res", async_request)

    sync_result = spider.search(keyword="Movie", mtype=MediaType.MUSIC, page=2)
    async_result = asyncio.run(
        spider.async_search(keyword="Movie", mtype=MediaType.MUSIC, page=2)
    )

    expected_plan = {"keyword": "Movie", "mtype": MediaType.MUSIC, "page": 2, "cat": None}
    assert sync_result == async_result == (False, [{"title": "projected"}])
    assert prepare == [expected_plan, expected_plan]
    assert finalized == [
        [("sync", 8), ("sync", 16)],
        [("async", 8), ("async", 16)],
    ]


def test_tnode_and_torrentleech_preflight_failures_skip_network(monkeypatch):
    """
    令牌缺失和不支持中文的入口都应在同步、异步 HTTP 前终止。
    """
    tnode = _build_api_spider("tnode", monkeypatch)
    torrentleech = _build_api_spider("torrentleech", monkeypatch)

    def no_token():
        """
        回放同步令牌缺失。
        """
        return None

    async def no_async_token():
        """
        回放异步令牌缺失。
        """
        return None

    monkeypatch.setattr(tnode, "_TNodeSpider__get_token", no_token)
    monkeypatch.setattr(tnode, "_TNodeSpider__async_get_token", no_async_token)

    assert tnode.search(keyword="Movie") == (True, [])
    assert asyncio.run(tnode.async_search(keyword="Movie")) == (True, [])
    assert torrentleech.search(keyword="中文") == (True, [])
    assert asyncio.run(torrentleech.async_search(keyword="中文")) == (True, [])


def test_site_spider_sync_async_share_admission_and_decoding(monkeypatch):
    """
    普通页面 Spider 仅保留 HTTP 与线程执行差异，准入和解码投影保持一致。
    """
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
        """
        回放普通页面同步响应。
        """
        return response

    async def async_request(*_args, **_kwargs):
        """
        回放普通页面异步响应。
        """
        return response

    def decode(actual_response, **_kwargs):
        """
        记录统一解码入口收到的响应。
        """
        assert actual_response is response
        return actual_response.text

    def parse(_self, text):
        """
        记录两种入口交付的相同解析文本。
        """
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


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (None, "站点无响应（网络错误或超时）"),
        (
            _FakeResponse(
                status_code=403,
                text="<html><head><title>\n  Just a moment...\n</title></head>"
                     "<body>Enable JavaScript and cookies to continue</body></html>",
            ),
            "HTTP 403，页面标题：Just a moment...",
        ),
        (_FakeResponse(status_code=502, text="Bad Gateway"), "HTTP 502，响应内容：Bad Gateway"),
        (_FakeResponse(status_code=200, text=""), "HTTP 200，响应内容为空"),
        (
            _FakeResponse(text="<html><head><title>登录</title></head><body></body></html>"),
            "返回登录或权限提示页",
        ),
    ],
)
def test_site_spider_records_failure_detail_for_unusable_page(monkeypatch, response, detail):
    """
    未拿到可用页面时同步、异步入口都记录状态码与页面摘要，供搜索失败原因展示。
    """
    indexer = {
        "id": "generic",
        "name": "Generic",
        "domain": "https://tracker.example/",
        "search": {"paths": [{"path": "browse.php"}]},
        "torrents": {"list": {}, "fields": {}},
    }

    async def async_request(*_args, **_kwargs):
        """
        回放普通页面异步响应。
        """
        return response

    monkeypatch.setattr(RequestUtils, "get_res", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(AsyncRequestUtils, "get_res", async_request)

    sync_spider = SiteSpider(indexer=indexer, keyword="Movie")
    async_spider = SiteSpider(indexer=indexer, keyword="Movie")

    assert sync_spider.get_torrents() == []
    assert asyncio.run(async_spider.async_get_torrents()) == []
    assert sync_spider.is_error and async_spider.is_error
    assert sync_spider.error_detail == async_spider.error_detail == detail
