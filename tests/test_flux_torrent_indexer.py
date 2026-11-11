"""
验证 FluxTorrent 兼容层站点的搜索、用户数据解析与站点资源协议路由。
"""

import asyncio
import json

import app.modules.indexer as indexer_module
from app.foundation import temporal as time_tools
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.fluxtorrent import FluxTorrentSiteUserInfo
from app.modules.indexer.spider import fluxtorrent as flux_torrent_module
from app.modules.indexer.spider.fluxtorrent import FluxTorrentSpider
from app.schemas.types import MediaType

_SITE_URL = "https://flux.example/"
_API_TOKEN = "fxo_test_token"


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
    assert FluxTorrentSpider._media_type({}) == MediaType.UNKNOWN.value


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


class _BrokenResponse:
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
