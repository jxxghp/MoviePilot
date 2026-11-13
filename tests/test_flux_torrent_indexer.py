"""
验证 FluxTorrent 兼容层站点的搜索、用户数据解析与站点资源协议路由。
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

import app.modules.indexer as indexer_module
from app.foundation import temporal as time_tools
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser import fluxtorrent as flux_parser_module
from app.modules.indexer.parser.fluxtorrent import FluxTorrentSiteUserInfo
from app.modules.indexer.spider import fluxtorrent as flux_torrent_module
from app.modules.indexer.spider.fluxtorrent import FluxTorrentSpider
from app.schemas.types import MediaType

_SITE_URL = "https://flux.example/"
_API_TOKEN = "fxo_test_token"


@pytest.fixture(autouse=True)
def _isolate_request_limits(monkeypatch):
    """
    隔离共享请求等待和站点查询，回放测试不消耗真实时间或站点配置。
    """
    monkeypatch.setattr(flux_torrent_module, "wait_for_site_request", Mock())
    monkeypatch.setattr(flux_torrent_module, "async_wait_for_site_request", AsyncMock())
    monkeypatch.setattr(
        flux_parser_module, "site_request_config",
        lambda _url: {"id": "flux", "domain": _SITE_URL},
    )


class _FakeResponse:
    """
    提供 FluxTorrent 兼容层响应判定所需的最小 HTTP 响应契约。
    """

    def __init__(self, payload=None, status_code: int = 200):
        """
        保存固定状态、JSON 数据和响应文本。
        """
        self._payload = payload
        self.status_code = status_code
        self.reason = "OK"
        self.text = json.dumps(payload) if payload is not None else ""
        self.closed = False

    def __bool__(self) -> bool:
        """
        按 requests.Response 的成功状态布尔语义返回结果。
        """
        return self.status_code < 400

    def json(self):
        """
        返回预设 JSON 数据。
        """
        return self._payload

    def close(self):
        """
        记录同步响应释放，供重试和最终解析收尾断言。
        """
        self.closed = True

    async def aclose(self):
        """
        记录异步响应释放，避免重试延迟期间继续持有连接。
        """
        self.closed = True


def _build_indexer(**overrides) -> dict:
    """
    构造 FluxTorrent 站点索引配置。
    """
    indexer = {
        "id": "flux",
        "name": "FluxTorrent",
        "url": _SITE_URL,
        "domain": _SITE_URL,
        "apikey": _API_TOKEN,
        "ua": "MoviePilot-Test",
        "proxy": False,
    }
    indexer.update(overrides)
    return indexer


def _envelope(data, code: int = 0, message: str = "ok") -> dict:
    """
    构造 FluxTorrent 统一响应信封。
    """
    return {
        "code": code,
        "message": message,
        "data": data,
        "request_id": "req-1",
    }


def _torrent_items() -> list:
    """
    构造兼容层种子列表数据。
    """
    return [
        {
            "id": 123,
            "name": "Example.Movie.2026.2160p.WEB-DL.H.265-GROUP",
            "small_descr": "电影中文副标题",
            "seeders": 15,
            "leechers": 2,
            "completed": 30,
            "size": 21474836480,
            "added": 1753084800,
            "category": 1,
            "category_name": "电影",
            "category_newznab": 2000,
            "promotion": "free",
            "promotion_name": "Free",
            "official": True,
            "sticky": False,
        }
    ]


def test_flux_torrent_spider_maps_compat_items(monkeypatch):
    """
    兼容层种子应映射为标准字段，并按促销档位给出计费倍率。
    """
    captured = {}

    def fake_get_res(request, url: str, params: dict = None, **_kwargs):
        """
        回放兼容层种子列表响应并记录请求参数。
        """
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = request._headers
        return _FakeResponse(_envelope({"items": _torrent_items()}))

    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res", fake_get_res)
    error, torrents = FluxTorrentSpider(_build_indexer()).search(keyword="Example", page=0)

    assert not error
    assert captured["url"] == "https://flux.example/api/v1/compat/nexusphp/torrents.json"
    assert captured["params"] == {"page": 1, "pagesize": 50, "keyword": "Example"}
    assert captured["headers"]["Authorization"] == f"Token {_API_TOKEN}"
    assert torrents == [{
        "title": "Example.Movie.2026.2160p.WEB-DL.H.265-GROUP",
        "description": "电影中文副标题",
        "enclosure": (
            "https://flux.example/api/v1/compat/nexusphp/download.php"
            f"?id=123&apikey={_API_TOKEN}"
        ),
        # 兼容层给的是 Unix 时间戳，发布时间按运行环境时区渲染，这里对同一转换取期望值。
        "pubdate": time_tools.format_timestamp(1753084800),
        "size": 21474836480,
        "seeders": 15,
        "peers": 2,
        "grabs": 30,
        "downloadvolumefactor": 0.0,
        "uploadvolumefactor": 1.0,
        "page_url": "https://flux.example/torrent/123",
        "labels": ["Free", "官方"],
        "category": MediaType.MOVIE.value,
    }]


def test_flux_torrent_request_limits_preserve_user_settings():
    """
    用户配置的间隔与次数限制保持有效，流控快照不携带凭据。
    """
    limits = flux_torrent_module.site_request_limits(_build_indexer(
        limit_seconds=8, limit_interval=60, limit_count=5,
    ))

    assert limits == {
        "id": "flux", "domain": _SITE_URL.rstrip("/"),
        "limit_seconds": 8, "limit_interval": 60, "limit_count": 5,
    }
    assert flux_torrent_module.site_request_limits(_build_indexer())["limit_seconds"] is None
    assert FluxTorrentSpider.manages_request_throttle is True


def test_flux_torrent_search_timeout_preserves_user_setting():
    """
    慢搜索默认等待三十秒，用户显式设置的超时时间继续优先。
    """
    assert FluxTorrentSpider(_build_indexer())._timeout == 30
    assert FluxTorrentSpider(_build_indexer(timeout=45))._timeout == 45


@pytest.mark.parametrize("ua", [None, "", "MoviePilot/3.2.6", "Custom-UA"])
def test_flux_torrent_user_agent_reaches_api_and_download_context(monkeypatch, ua):
    """
    默认标识转换为浏览器标识，资料、搜索结果和检查点恢复沿用相同规则。
    """
    monkeypatch.setattr(flux_torrent_module, "get_runtime_setting", lambda key: {
        "USER_AGENT": "MoviePilot/3.2.6", "NORMAL_USER_AGENT": "Browser-UA",
    }[key])
    site = _build_indexer(id=42, parser="FluxTorrent", ua=ua)
    expected = "Custom-UA" if ua == "Custom-UA" else "Browser-UA"
    spider = FluxTorrentSpider(site)
    assert spider._request_headers()["User-Agent"] == expected
    records = spider._parse_result(_torrent_items())
    torrents = IndexerModule._IndexerModule__parse_result(site, records, seconds=0)
    assert torrents[0].site_ua == expected
    restored = IndexerModule.restore_search_torrent(site, torrents[0].to_dict())
    assert restored.site_ua == expected
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent", url=_SITE_URL, site_cookie=None,
        apikey=_API_TOKEN, token=None, ua=ua,
    )
    parser._parse_site_page({})
    assert parser._addition_headers["User-Agent"] == expected


def test_standard_indexer_preserves_configured_user_agent():
    """
    普通站点搜索结果及检查点恢复保持原有 User-Agent 配置语义。
    """
    site = _build_indexer(id=42, ua="Custom-UA")
    records = FluxTorrentSpider(site)._parse_result(_torrent_items())
    torrents = IndexerModule._IndexerModule__parse_result(site, records, seconds=0)
    assert torrents[0].site_ua == "Custom-UA"
    restored = IndexerModule.restore_search_torrent(site, torrents[0].to_dict())
    assert restored.site_ua == "Custom-UA"


def test_flux_torrent_connection_and_search_wait_before_each_request(monkeypatch):
    """
    连接检测、同步搜索与异步搜索应在 HTTP 调用之前申请相同的站点额度。
    """
    events = []
    expected_limits = flux_torrent_module.site_request_limits(_build_indexer())

    def wait(limits):
        """
        记录同步请求额度申请并核对稳定站点身份。
        """
        assert limits == expected_limits
        events.append("wait")

    async def async_wait(limits):
        """
        记录异步请求额度申请并核对稳定站点身份。
        """
        wait(limits)

    def search(_self, url, **_kwargs):
        """
        回放同步种子搜索响应。
        """
        events.append("connection" if url.endswith("user.json") else "search")
        return _FakeResponse(_envelope({"items": _torrent_items()}))

    async def async_search(_self, **_kwargs):
        """
        回放异步种子搜索响应。
        """
        events.append("async_search")
        return _FakeResponse(_envelope({"items": _torrent_items()}))

    monkeypatch.setattr(flux_torrent_module, "wait_for_site_request", wait)
    monkeypatch.setattr(flux_torrent_module, "async_wait_for_site_request", async_wait)
    monkeypatch.setattr(flux_torrent_module.RequestUtils, "get_res", search)
    monkeypatch.setattr(flux_torrent_module.AsyncRequestUtils, "get_res", async_search)

    spider = FluxTorrentSpider(_build_indexer())
    assert spider.test_connection()[0]
    assert not spider.search(keyword="Example")[0]
    assert not asyncio.run(spider.async_search(keyword="Example"))[0]
    assert events == ["wait", "connection", "wait", "search", "wait", "async_search"]


def test_flux_torrent_userdata_waits_before_profile_and_legacy_statistics(monkeypatch):
    """
    资料刷新和老版本补充统计均须经过共享流控，沿用搜索与连接检测的站点身份。
    """
    events = []
    expected_limits = flux_torrent_module.site_request_limits(_build_indexer())

    def wait(limits):
        """
        核对资料刷新使用的站点额度与专站爬虫一致。
        """
        assert limits == expected_limits
        events.append("wait")

    def get_res(_self, url, **_kwargs):
        """
        回放缺少做种字段的资料以及后续补充统计。
        """
        events.append(url.rsplit("/", 1)[-1])
        data = {"id": 42, "username": "tester"} if url.endswith("user.json") else {
            "seeding": 12, "seedingSize": 1024, "joinTime": "2025-07-15T20:45:13+00:00",
        }
        return _FakeResponse(_envelope(data))

    monkeypatch.setattr(flux_torrent_module, "wait_for_site_request", wait)
    monkeypatch.setattr(flux_torrent_module.RequestUtils, "get_res", get_res)
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent", url=_SITE_URL, site_cookie=None,
        apikey=_API_TOKEN, token=None, ua="MoviePilot-Test",
    )
    parser.parse()

    assert events == ["wait", "user.json", "wait", "ptppUserInfo"]
    assert parser.seeding == 12
    assert parser.seeding_size == 1024
    assert parser.err_msg is None


def test_flux_torrent_auth_failure_does_not_request_other_userdata_models(monkeypatch):
    """
    API 认证失败后保留失败结果，停止补充统计和网页模型回退，避免额外请求放大限流。
    """
    requests = []

    def get_res(_self, url, **_kwargs):
        """
        记录真实资料入口发出的请求并回放认证失败。
        """
        requests.append(url)
        return _FakeResponse(_envelope(None, code=2001, message="未认证"), status_code=401)

    monkeypatch.setattr(flux_torrent_module.RequestUtils, "get_res", get_res)
    monkeypatch.setattr(IndexerModule, "_site_schemas", [FluxTorrentSiteUserInfo])
    module = object.__new__(IndexerModule)
    result = module.refresh_userdata(_build_indexer(schema="FluxTorrent"))

    assert result.userid is None
    assert result.err_msg == "未认证"
    assert requests == [f"{_SITE_URL}api/v1/compat/nexusphp/user.json"]


def test_flux_torrent_spider_maps_promotion_factors():
    """
    促销档位应换算为站点计费口径的下载与上传倍率。
    """
    assert FluxTorrentSpider._volume_factors("free") == (0.0, 1.0)
    assert FluxTorrentSpider._volume_factors("x2free") == (0.0, 2.0)
    assert FluxTorrentSpider._volume_factors("half") == (0.5, 1.0)
    assert FluxTorrentSpider._volume_factors("x2half") == (0.5, 2.0)
    assert FluxTorrentSpider._volume_factors("x2") == (1.0, 2.0)
    assert FluxTorrentSpider._volume_factors("p30") == (0.3, 1.0)
    assert FluxTorrentSpider._volume_factors(None) == (1.0, 1.0)
    assert FluxTorrentSpider._volume_factors("unknown") == (1.0, 1.0)


def test_flux_torrent_spider_maps_media_types():
    """
    分类应按 Newznab 标准号映射媒体类型，缺标准号时按分类名兜底。
    """
    assert FluxTorrentSpider._media_type({"category_newznab": 2000}) == MediaType.MOVIE.value
    assert FluxTorrentSpider._media_type({"category_newznab": 5000}) == MediaType.TV.value
    assert FluxTorrentSpider._media_type({"category_newznab": 5070}) == MediaType.TV.value
    assert FluxTorrentSpider._media_type({"category_newznab": 3000}) == MediaType.MUSIC.value
    assert FluxTorrentSpider._media_type({"category_newznab": 7020}) == MediaType.COLLECTION.value
    assert FluxTorrentSpider._media_type({"category_newznab": 4000}) == MediaType.UNKNOWN.value
    assert FluxTorrentSpider._media_type({"category_name": "短剧"}) == MediaType.TV.value
    assert FluxTorrentSpider._media_type({"category_name": "电子书"}) == MediaType.COLLECTION.value
    # 教育类没有对应的 Newznab 媒体号：保留站内分类名，不写「未知」，
    # 教育影音也不能被当成电视剧。
    assert FluxTorrentSpider._media_type({"category_name": "初中"}) == "初中"
    assert FluxTorrentSpider._media_type({"category_name": "大学"}) == "大学"
    assert FluxTorrentSpider._media_type({
        "category_newznab": 8000, "category_name": "教育影音",
    }) == "教育影音"
    assert FluxTorrentSpider._media_type({
        "category_newznab": 8000, "category_name": "初中",
    }) == "初中"
    assert FluxTorrentSpider._media_type({}) == MediaType.UNKNOWN.value


def _patch_connection(monkeypatch, response, captured: dict = None):
    """
    让连接检测拿到固定响应，并记录请求地址。
    """
    def fake_get_res(_self, **kwargs):
        """
        回放预设响应。
        """
        if captured is not None:
            captured.update(kwargs)
        return response

    monkeypatch.setattr(
        flux_torrent_module.RequestUtils, "get_res", fake_get_res
    )


def test_flux_torrent_test_connection_uses_api_token(monkeypatch):
    """
    连接检测应请求兼容层用户接口并复用开放 API 令牌。
    """
    captured = {}
    _patch_connection(
        monkeypatch,
        _FakeResponse(_envelope({"id": 8, "username": "gntv"})),
        captured,
    )

    assert FluxTorrentSpider(_build_indexer()).test_connection() == (
        True, "连接成功",
    )
    assert captured["url"] == (
        "https://flux.example/api/v1/compat/nexusphp/user.json"
    )


def test_flux_torrent_test_connection_requires_token(monkeypatch):
    """
    没有令牌时不应发起连接检测请求。
    """
    def fake_get_res(*_args, **_kwargs):
        """
        记录不应发生的请求。
        """
        raise AssertionError("未配置令牌时不应发起请求")

    monkeypatch.setattr(
        flux_torrent_module.RequestUtils, "get_res", fake_get_res
    )

    assert FluxTorrentSpider(_build_indexer(apikey="")).test_connection() == (
        False, "未配置开放 API Token（fxo_ 开头）",
    )


def test_flux_torrent_test_connection_surfaces_auth_error(monkeypatch):
    """
    鉴权失败时把兼容层信封里的原因透出来，而不是报 Cookie 失效。
    """
    _patch_connection(
        monkeypatch,
        _FakeResponse(
            _envelope(None, code=2001, message="未认证"), status_code=401,
        ),
    )

    success, message = FluxTorrentSpider(_build_indexer()).test_connection()

    assert success is False
    assert "未认证" in message


@pytest.mark.parametrize("operation", ["connection", "search", "userdata"])
def test_flux_torrent_retries_gateway_503_once(monkeypatch, operation):
    """
    只读入口遇到网关 503 时先释放响应，延迟后重新申请额度并重试一次。
    """
    first = _FakeResponse(None, status_code=503)
    second = _FakeResponse(_envelope({
        "id": 42, "username": "tester", "items": _torrent_items(),
        "seeding": 0, "seeding_size": 0, "join_at": "2025-07-15T20:45:13+00:00",
    }))
    request = Mock(side_effect=[first, second])
    sleep = Mock()
    monkeypatch.setattr(flux_torrent_module.RequestUtils, "get_res", request)
    monkeypatch.setattr(flux_torrent_module.time, "sleep", sleep)

    if operation == "connection":
        assert FluxTorrentSpider(_build_indexer()).test_connection()[0]
    elif operation == "search":
        assert not FluxTorrentSpider(_build_indexer()).search(keyword="Example")[0]
    else:
        parser = FluxTorrentSiteUserInfo(
            site_name="FluxTorrent", url=_SITE_URL, site_cookie=None,
            apikey=_API_TOKEN, token=None, ua="MoviePilot-Test",
        )
        parser.parse()
        assert parser.userid == 42
        assert parser.err_msg is None

    assert request.call_count == 2
    assert first.closed and second.closed
    sleep.assert_called_once_with(3)
    assert flux_torrent_module.wait_for_site_request.call_count == 2
    assert request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_flux_torrent_retry_is_bounded_and_preserves_final_failure(monkeypatch, status):
    """
    认证与限流失败保持原响应，连续 503 最多重试两次并将最终失败返回调用方。
    """
    response = _FakeResponse(_envelope(None, code=2001, message="接口失败"), status_code=status)
    request = Mock(return_value=response)
    sleep = Mock()
    monkeypatch.setattr(flux_torrent_module.RequestUtils, "get_res", request)
    monkeypatch.setattr(flux_torrent_module.time, "sleep", sleep)

    assert not FluxTorrentSpider(_build_indexer()).test_connection()[0]
    assert request.call_count == (3 if status == 503 else 1)
    assert sleep.call_count == (2 if status == 503 else 0)
    if status == 503:
        assert [call.args[0] for call in sleep.call_args_list] == [3, 6]
    assert response.closed


def test_flux_torrent_async_gateway_retry_releases_response(monkeypatch):
    """
    异步 503 重试使用可等待的延迟并释放两次响应，结果与同步解析保持一致。
    """
    first = _FakeResponse(None, status_code=503)
    second = _FakeResponse(_envelope({"items": _torrent_items()}))
    request = AsyncMock(side_effect=[first, second])
    sleep = AsyncMock()
    monkeypatch.setattr(flux_torrent_module.AsyncRequestUtils, "get_res", request)
    monkeypatch.setattr(flux_torrent_module.asyncio, "sleep", sleep)

    error, torrents = asyncio.run(FluxTorrentSpider(_build_indexer()).async_search(keyword="Example"))

    assert not error and len(torrents) == 1
    assert request.await_count == 2
    assert first.closed and second.closed
    sleep.assert_awaited_once_with(3)
    assert flux_torrent_module.async_wait_for_site_request.await_count == 2


def test_flux_torrent_async_gateway_retry_can_be_cancelled(monkeypatch):
    """
    取消异步退避时传播取消信号，已释放失败响应且不继续发送请求。
    """
    response = _FakeResponse(None, status_code=503)
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(flux_torrent_module.AsyncRequestUtils, "get_res", request)
    monkeypatch.setattr(flux_torrent_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(FluxTorrentSpider(_build_indexer()).async_search(keyword="Example"))

    assert response.closed
    assert request.await_count == 1


def test_flux_torrent_test_connection_rejects_non_json(monkeypatch):
    """
    首页 HTML 等非兼容层响应不能被当成连接成功。
    """
    _patch_connection(monkeypatch, _FakeResponse(None))

    success, message = FluxTorrentSpider(_build_indexer()).test_connection()

    assert success is False
    assert "兼容层" in message


def test_flux_torrent_spider_requires_api_token(monkeypatch):
    """
    未配置开放 API 令牌时不应发起搜索请求。
    """
    def fake_get_res(*_args, **_kwargs):
        """
        记录不应发生的请求。
        """
        raise AssertionError("未配置令牌时不应发起请求")

    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res", fake_get_res)
    error, torrents = FluxTorrentSpider(_build_indexer(apikey="")).search(keyword="Example")

    assert error
    assert torrents == []


def test_flux_torrent_spider_reports_business_error(monkeypatch):
    """
    兼容层返回业务错误码时应判定搜索失败。
    """
    monkeypatch.setattr(
        "app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res",
        lambda *_args, **_kwargs: _FakeResponse(
            _envelope(None, code=2001, message="令牌无效"), status_code=401
        ),
    )
    error, torrents = FluxTorrentSpider(_build_indexer()).search(keyword="Example")

    assert error
    assert torrents == []


def test_flux_torrent_spider_sync_async_parity(monkeypatch):
    """
    同步与异步搜索入口应返回一致结果。
    """
    def sync_get_res(_request, url: str, params: dict = None, **_kwargs):
        """
        回放兼容层种子列表响应。
        """
        return _FakeResponse(_envelope({"items": _torrent_items()}))

    async def async_get_res(_request, url: str, params: dict = None, **_kwargs):
        """
        回放兼容层种子列表响应。
        """
        return _FakeResponse(_envelope({"items": _torrent_items()}))

    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res", sync_get_res)
    monkeypatch.setattr(
        "app.modules.indexer.spider.fluxtorrent.AsyncRequestUtils.get_res", async_get_res
    )
    spider = FluxTorrentSpider(_build_indexer())

    assert spider.search(keyword="Example", page=0) == asyncio.run(
        spider.async_search(keyword="Example", page=0)
    )


def test_flux_torrent_parser_reads_user_profile(monkeypatch):
    """
    兼容层用户接口应解析出账号资料与流量统计。
    """
    payload = _envelope({
        "id": 42,
        "username": "tester",
        "uploaded": 21474836480,
        "downloaded": 1073741824,
        "seedbonus": 1024,
        "class": 9,
        "class_name": "VIP",
        "ratio": 20.0,
        "ratio_infinite": False,
        "ratio_display": "20.0000",
        "seeding": 11322,
        "seeding_size": 588458459803,
        "join_at": "2025-07-15T20:45:13+00:00",
    })
    monkeypatch.setattr(
        FluxTorrentSiteUserInfo,
        "_get_page_content",
        lambda self, url, **_kwargs: json.dumps(payload),
    )
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent",
        url=_SITE_URL,
        site_cookie=None,
        apikey=_API_TOKEN,
        token=None,
        ua="MoviePilot-Test",
    )
    parser.parse()

    assert parser.userid == 42
    assert parser.username == "tester"
    assert parser.user_level == "VIP"
    assert parser.upload == 21474836480
    assert parser.download == 1073741824
    assert parser.ratio == 20.0
    assert parser.bonus == 1024.0
    assert parser.seeding == 11322
    assert parser.seeding_size == 588458459803
    assert parser.join_at and parser.join_at.startswith("2025-07-15")
    assert parser.err_msg is None


def test_flux_torrent_parser_handles_infinite_ratio(monkeypatch):
    """
    只上传未下载时站点标记分享率为无穷大，此时不应给出有限数值。
    """
    payload = _envelope({
        "id": 42,
        "username": "tester",
        "uploaded": 21474836480,
        "downloaded": 0,
        "seedbonus": 0,
        "class": 1,
        "class_name": "普通用户",
        "ratio": 0.0,
        "ratio_infinite": True,
    })
    monkeypatch.setattr(
        FluxTorrentSiteUserInfo,
        "_get_page_content",
        lambda self, url, **_kwargs: json.dumps(payload),
    )
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent",
        url=_SITE_URL,
        site_cookie=None,
        apikey=_API_TOKEN,
        token=None,
        ua="MoviePilot-Test",
    )
    parser.parse()

    assert parser.userid == 42
    assert parser.ratio == 0.0


def test_flux_torrent_parser_falls_back_to_ptpp_seeding(monkeypatch):
    """
    老版本站点的 user.json 没有做种与注册时间时，用 PTPP 端点补齐。
    """
    legacy = _envelope({
        "id": 8, "username": "gntv", "uploaded": 1, "downloaded": 1,
        "seedbonus": 0, "class": 9, "class_name": "维护开发员",
        "ratio": 1.0, "ratio_infinite": False,
    })
    ptpp = _envelope({
        "id": 8, "name": "gntv", "seeding": 11322,
        "seedingSize": 588458459803,
        "joinTime": "2025-07-15T20:45:13+00:00",
    })

    def fake_page(_self, url, **_kwargs):
        """
        按地址回放用户接口与聚合端点。
        """
        if "ptppUserInfo" in url:
            return json.dumps(ptpp)
        return json.dumps(legacy)

    monkeypatch.setattr(FluxTorrentSiteUserInfo, "_get_page_content", fake_page)
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent", url=_SITE_URL, site_cookie=None,
        apikey=_API_TOKEN, token=None, ua="MoviePilot-Test",
    )
    parser.parse()

    assert parser.seeding == 11322
    assert parser.seeding_size == 588458459803
    assert parser.join_at and parser.join_at.startswith("2025-07-15")
    assert parser.err_msg is None


def test_flux_torrent_parser_skips_ptpp_when_profile_complete(monkeypatch):
    """
    新版本站点字段齐全时不再请求聚合端点，省掉一次开放 API 配额。
    """
    payload = _envelope({
        "id": 8, "username": "gntv", "uploaded": 1, "downloaded": 1,
        "seedbonus": 0, "class": 9, "class_name": "维护开发员",
        "ratio": 1.0, "ratio_infinite": False,
        "seeding": 11322, "seeding_size": 588458459803,
        "join_at": "2025-07-15T20:45:13+00:00",
    })
    requested = []

    def fake_page(_self, url, **_kwargs):
        """
        记录所有请求地址。
        """
        requested.append(url)
        return json.dumps(payload)

    monkeypatch.setattr(FluxTorrentSiteUserInfo, "_get_page_content", fake_page)
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent", url=_SITE_URL, site_cookie=None,
        apikey=_API_TOKEN, token=None, ua="MoviePilot-Test",
    )
    parser.parse()

    assert parser.seeding == 11322
    assert not any("ptppUserInfo" in url for url in requested)


def test_flux_torrent_parser_keeps_profile_when_ptpp_unavailable(monkeypatch):
    """
    老站没部署聚合端点时，补充失败不能把用户资料判成解析失败。
    """
    legacy = _envelope({
        "id": 7, "username": "legacy", "uploaded": 21474836480,
        "downloaded": 1073741824, "seedbonus": 10, "class": 1,
        "class_name": "普通用户", "ratio": 20.0, "ratio_infinite": False,
    })

    def fake_page(_self, url, **_kwargs):
        """
        聚合端点返回空响应，模拟站点未部署该接口。
        """
        if "ptppUserInfo" in url:
            return ""
        return json.dumps(legacy)

    monkeypatch.setattr(FluxTorrentSiteUserInfo, "_get_page_content", fake_page)
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent", url=_SITE_URL, site_cookie=None,
        apikey=_API_TOKEN, token=None, ua="MoviePilot-Test",
    )
    parser.parse()

    assert parser.username == "legacy"
    assert parser.upload == 21474836480
    assert parser.seeding == 0
    assert parser.join_at is None
    assert parser.err_msg is None


def test_flux_torrent_parser_reports_business_error(monkeypatch):
    """
    兼容层返回业务错误码时应给出错误信息且不产出用户 ID。
    """
    monkeypatch.setattr(
        FluxTorrentSiteUserInfo,
        "_get_page_content",
        lambda self, url, **_kwargs: json.dumps(
            _envelope(None, code=2001, message="令牌无效或已撤销")
        ),
    )
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent",
        url=_SITE_URL,
        site_cookie=None,
        apikey=_API_TOKEN,
        token=None,
        ua="MoviePilot-Test",
    )
    parser.parse()

    assert parser.userid is None
    assert parser.err_msg == "令牌无效或已撤销"


def test_flux_torrent_registrations():
    """
    FluxTorrent 应登记为专站搜索协议并声明支持的搜索参数。
    """
    assert SiteSchema.FluxTorrent.value == "FluxTorrent"
    assert indexer_module.SPIDER_PARSER_CLASSES["FluxTorrent"] is FluxTorrentSpider
    assert indexer_module._SPECIALIZED_SEARCH_ARGUMENTS["FluxTorrent"] == ("keyword", "page")
    assert FluxTorrentSpider.get_search_page_size() == 50


def test_flux_torrent_routes_by_declared_parser():
    """
    站点资源声明 FluxTorrent 后，搜索应直接路由到兼容层索引器。
    """
    assert IndexerModule.get_search_page_size(_build_indexer(parser="FluxTorrent")) == 50
    request = IndexerModule._IndexerModule__create_search_request(
        site=_build_indexer(parser="FluxTorrent"),
        keyword="Example",
        mtype=MediaType.MOVIE,
        page=1,
    )

    assert request is not None
    assert request.parser_class is FluxTorrentSpider
    assert dict(request.arguments) == {"keyword": "Example", "page": 1}


def test_refresh_userdata_uses_declared_flux_torrent_schema(monkeypatch):
    """
    站点资源声明 FluxTorrent 用户数据模型后，刷新应直接使用兼容层解析器。
    """
    payload = _envelope({
        "id": 42,
        "username": "tester",
        "uploaded": 1024,
        "downloaded": 512,
        "seedbonus": 10,
        "class": 3,
        "class_name": "VIP",
        "ratio": 2.0,
        "ratio_infinite": False,
    })
    monkeypatch.setattr(
        FluxTorrentSiteUserInfo,
        "_get_page_content",
        lambda self, url, **_kwargs: json.dumps(payload),
    )
    monkeypatch.setattr(IndexerModule, "_site_schemas", [FluxTorrentSiteUserInfo])

    module = object.__new__(IndexerModule)
    result = module.refresh_userdata({
        "name": "FluxTorrent",
        "url": _SITE_URL,
        "schema": SiteSchema.FluxTorrent.value,
        "apikey": _API_TOKEN,
        "public": False,
    })

    assert result.userid == 42
    assert result.username == "tester"
    assert result.user_level == "VIP"
    assert result.ratio == 2.0


class _BrokenResponse(_FakeResponse):
    """
    提供响应体不是 JSON 时的最小 HTTP 响应契约。
    """

    status_code = 200

    def __bool__(self) -> bool:
        """
        按 requests.Response 的成功状态布尔语义返回结果。
        """
        return True

    def json(self):
        """
        模拟响应体解析失败。
        """
        raise ValueError("not json")


def test_flux_torrent_site_base_url_normalizes_address():
    """
    站点地址缺少协议时补 https，末尾斜杠应去掉，空地址返回空串。
    """
    assert flux_torrent_module.site_base_url({"url": "https://flux.example/"}) == "https://flux.example"
    assert flux_torrent_module.site_base_url({"url": "flux.example/"}) == "https://flux.example"
    assert flux_torrent_module.site_base_url({"url": "  "}) == ""
    assert flux_torrent_module.site_api_token({"apikey": " fxo_a "}) == "fxo_a"


def test_flux_torrent_spider_tolerates_unusable_responses(monkeypatch):
    """
    站点无响应或响应不是 JSON 时应判定为失败而不是抛异常。
    """
    spider = FluxTorrentSpider(_build_indexer())

    monkeypatch.setattr(
        "app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res",
        lambda *_args, **_kwargs: None,
    )
    assert spider.search(keyword="Example", page=0) == (True, [])

    monkeypatch.setattr(
        "app.modules.indexer.spider.fluxtorrent.RequestUtils.get_res",
        lambda *_args, **_kwargs: _BrokenResponse(),
    )
    assert spider.search(keyword="Example", page=0) == (True, [])


def test_flux_torrent_async_search_requires_api_token():
    """
    异步搜索在没有开放 API 令牌时应直接判定失败。
    """
    spider = FluxTorrentSpider(_build_indexer(apikey="", token=""))

    assert asyncio.run(spider.async_search(keyword="Example", page=0)) == (True, [])


def test_flux_torrent_parser_rejects_unusable_payloads():
    """
    用户接口返回空响应、非 JSON、结构异常或业务错误码时都应给出错误信息。
    """
    parser = FluxTorrentSiteUserInfo(
        site_name="FluxTorrent",
        url=_SITE_URL,
        site_cookie=None,
        apikey=_API_TOKEN,
        token=None,
        ua="",
    )

    assert parser._load_api_payload("", "获取用户信息") is None
    assert "未收到接口响应" in parser.err_msg
    assert parser._load_api_payload("not-json", "获取用户信息") is None
    assert "不是有效 JSON" in parser.err_msg
    assert parser._load_api_payload("[1, 2]", "获取用户信息") is None
    assert "结构无效" in parser.err_msg
    payload = _envelope(None, code=2001, message="invalid token")
    assert parser._load_api_payload(json.dumps(payload), "获取用户信息") is None
    assert parser.err_msg == "invalid token"
