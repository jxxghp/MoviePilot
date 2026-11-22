"""
验证可乐开放 API 的搜索、连接检测和下载配方，不使用真实凭据或网络。
"""

import base64
import datetime
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from requests import Response

import app.chain.site as site_chain
import app.modules.indexer as indexer_module
import app.modules.indexer.spider.colapt as colapt
from app.application.search.session import torrent_snapshot
from app.application.site.contract import SiteSnapshot
from app.application.site.observation import capture_site_search_observation
from app.chain.site import SiteChain
from app.domain.context import TorrentInfo
from app.modules.indexer import _SPECIALIZED_SEARCH_ARGUMENTS, SPIDER_PARSER_CLASSES, IndexerModule
from app.modules.indexer.parser import SiteSchema
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType

# 所有凭据、标题和账号均为合成值；字段依据站点 /api-docs 公布的开放 API 契约。
INDEXER = {
    "id": 9101, "name": "可乐", "domain": "https://www.colapt.com/", "url": "https://www.colapt.com/",
    "parser": "ColaPT", "schema": "ColaPT", "apikey": "synthetic-colapt-key", "cookie": "unused-cookie",
    "ua": "ColaPT-Test", "proxy": True, "timeout": 25, "pri": 3,
}
ITEM = {
    "id": 42, "media_id": "rc201", "release_name": "Synthetic.Coast.2026.2160p.WEB-DL.HEVC-GRP",
    "subtitle": "合成海岸 / 国英双语", "info_hash": "b" * 40, "size_bytes": "9007199254740993",
    "status": "approved", "published_at": "2026-11-10T10:26:49Z", "seeders": 4, "leechers": 2,
    "completions": 14, "tags": ["官方", "4K", "中字"],
    "promotion": {"upload": {"num": 2, "den": 1}, "download": {"num": 1, "den": 2},
                  "ends_at": "2026-11-20T10:26:49Z"},
}
MEDIA = {"media_id": "rc201", "title": "合成海岸", "category": "movie", "imdb_id": "tt1234567",
         "douban_id": "123456"}


def _local(value):
    """
    按运行环境时区换算预期的本地时间文本。
    """
    return datetime.datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _response(data, status=200):
    """
    构造具有真实 HTTP 布尔语义的响应，关闭动作保留为可验证替身。
    """
    response = Response()
    response.status_code = status
    response._content = json.dumps(data).encode()
    response.encoding = "utf-8"
    response.close = Mock()
    response.aclose = AsyncMock()
    return response


def _error(code, status, message="合成错误", **extra):
    """
    构造开放 API 的统一错误信封。
    """
    return _response({"error": {"code": code, "message": message, "request_id": "req-1", "fields": {}, **extra}},
                     status)


def _group(item=None, media=MEDIA):
    return {"group_id": "torrent:42", "media": deepcopy(media),
            "versions": [deepcopy(ITEM if item is None else item)], "matched_count": 1, "visible_count": 1}


def _page(groups=None, page=1, total_pages=1):
    groups = [_group()] if groups is None else groups
    return {"items": groups, "page": page, "page_size": 100, "total": len(groups), "total_pages": total_pages}


def _recipe(enclosure):
    encoded, url = enclosure[1:].split("]", 1)
    return json.loads(base64.b64decode(encoded)), url


@pytest.fixture
def boundary(monkeypatch):
    """
    隔离传输、等待和运行配置，保留真实参数构造与响应解析。
    """
    monkeypatch.setattr(colapt, "get_runtime_setting", lambda key: {
        "USER_AGENT": "Default-UA", "PROXY": {"https": "proxy"},
    }[key])
    wait, async_wait, defer = Mock(), AsyncMock(), Mock()
    monkeypatch.setattr(colapt, "wait_for_site_request", wait)
    monkeypatch.setattr(colapt, "async_wait_for_site_request", async_wait)
    monkeypatch.setattr(colapt, "defer_site_request", defer)
    response = _response(_page())
    request = Mock(get_res=Mock(return_value=response))
    factory = Mock(return_value=request)
    async_request = Mock(get_res=AsyncMock(return_value=response))
    async_factory = Mock(return_value=async_request)
    monkeypatch.setattr(colapt, "RequestUtils", factory)
    monkeypatch.setattr(colapt, "AsyncRequestUtils", async_factory)
    return SimpleNamespace(response=response, request=request, factory=factory, async_request=async_request,
                           async_factory=async_factory, wait=wait, async_wait=async_wait, defer=defer)


def test_colapt_protocol_is_registered_without_domain_binding() -> None:
    """
    资源声明 ColaPT 时选择专用适配器；域名不绑定，现有按 MTorrent 接入的用户不受影响。
    """
    assert SPIDER_PARSER_CLASSES["ColaPT"] is colapt.ColaPTSpider
    assert _SPECIALIZED_SEARCH_ARGUMENTS["ColaPT"] == ("keyword", "mtype", "cat", "page")
    assert SiteSchema.ColaPT.value == "ColaPT"
    assert "colapt.com" not in indexer_module._DOMAIN_CONNECTION_PARSERS
    assert IndexerModule.get_search_page_size(INDEXER) == 100


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("keyword", ["合成海岸 2026", "tt1234567", None, "  "])
@pytest.mark.asyncio
async def test_search_uses_key_only_and_projects_contract_fields(boundary, asynchronous, keyword):
    """
    同步、异步、IMDb 和最新列表都只使用 API Key，搜索时不签发下载地址。
    """
    spider = colapt.ColaPTSpider(INDEXER)
    result = await spider.async_search(keyword) if asynchronous else spider.search(keyword)
    assert result[0] is False and len(result[1]) == 1
    torrent = TorrentInfo(**result[1][0])
    assert (torrent.title, torrent.description) == (ITEM["release_name"], ITEM["subtitle"])
    assert torrent.size == 9007199254740993
    assert (torrent.seeders, torrent.peers, torrent.grabs) == (4, 2, 14)
    assert (torrent.downloadvolumefactor, torrent.uploadvolumefactor) == (0.5, 2)
    assert torrent.pubdate == _local("2026-11-10T10:26:49+00:00")
    assert torrent.freedate == _local("2026-11-20T10:26:49+00:00")
    assert torrent.page_url == "https://www.colapt.com/torrents/42"
    assert (torrent.media_source, torrent.media_id, torrent.category) == (
        MediaSource.IMDb, "tt1234567", MediaType.MOVIE.value,
    )
    assert torrent.labels == ["官方", "4K", "中字"]
    recipe, url = _recipe(torrent.enclosure)
    assert url == "https://www.colapt.com/api/v1/torrents/42/download-links"
    assert recipe["method"] == "post" and recipe["cookie"] is False and "json" not in recipe
    assert recipe["header"]["X-API-Key"] == "synthetic-colapt-key" and recipe["torrent_id"] == 42
    assert recipe["allow_redirects"] is False and recipe["proxy"] is True and recipe["protocol"] == "ColaPT"
    request = boundary.async_request if asynchronous else boundary.request
    factory = boundary.async_factory if asynchronous else boundary.factory
    expected = {"view": "torrents", "sort": "latest", "direction": "desc", "official_first": "false",
                "page": 1, "page_size": 100}
    if keyword and keyword.strip():
        expected["imdb_id" if keyword.startswith("tt") else "q"] = keyword
    request.get_res.assert_called_once_with(url="https://www.colapt.com/api/v1/media", params=expected,
                                           allow_redirects=False)
    assert factory.call_args.kwargs == {
        "headers": {"X-API-Key": "synthetic-colapt-key", "Accept": "application/json", "User-Agent": "ColaPT-Test"},
        "proxies": {"https": "proxy"}, "timeout": 25,
    }
    (boundary.async_wait if asynchronous else boundary.wait).assert_called_once()
    (boundary.response.aclose if asynchronous else boundary.response.close).assert_called_once()


@pytest.mark.parametrize("mtype,cat,server", [
    (None, "movie", "movie"),
    (MediaType.TV, "documentary", "documentary"),
    (None, "anime, documentary,anime", "anime,documentary"),
    (MediaType.MOVIE, None, "movie,anime,documentary,other"),
    (MediaType.TV, None, "tv,anime,variety,documentary,other"),
    (MediaType.MOVIE, "unknown-cat", "movie,anime,documentary,other"),
    (None, "unknown-cat", None),
])
def test_categories_are_sent_to_server_in_one_query(boundary, mtype, cat, server):
    """
    显式分类去重后多选交给服务端，否则按媒体类型排除明确属于另一类型的分类；结果不再本地筛选。
    """
    groups = [_group(dict(ITEM, id=index + 1), dict(MEDIA, category=category) if category else None)
              for index, category in enumerate(["movie", "tv", "anime", None])]
    boundary.request.get_res.return_value = _response(_page(groups, total_pages=3))
    with capture_site_search_observation() as observation:
        error, rows = colapt.ColaPTSpider(INDEXER).search(None, mtype=mtype, cat=cat)
    assert error is False and len(rows) == 4
    assert boundary.request.get_res.call_args.kwargs["params"].get("category") == server
    assert observation.raw_count == 4 and observation.has_more is True
    boundary.request.get_res.assert_called_once()


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, False)])
def test_hit_and_run_flag_is_projected(boundary, value, expected):
    """
    H&R 标记表示此刻下载会为调用者开启考核；缺省视为不会。
    """
    item = dict(ITEM, hit_and_run=value) if value is not None else dict(ITEM)
    boundary.request.get_res.return_value = _response(_page([_group(item)]))
    _, rows = colapt.ColaPTSpider(INDEXER).search(None)
    assert TorrentInfo(**rows[0]).hit_and_run is expected


@pytest.mark.parametrize("category,imdb,douban,expected_type,expected_source,expected_id", [
    ("tv", None, "123456", MediaType.TV, MediaSource.Douban, "123456"),
    ("variety", None, None, MediaType.TV, None, None),
    ("anime", None, None, MediaType.UNKNOWN, None, None),
    ("documentary", None, None, MediaType.UNKNOWN, None, None),
    ("other", "invalid", "invalid", MediaType.UNKNOWN, None, None),
])
def test_category_and_media_identity_are_not_guessed(boundary, category, imdb, douban,
                                                   expected_type, expected_source, expected_id):
    """
    正式媒体标识用于识别，动漫、纪录片等共享分类保持未知。
    """
    media = dict(MEDIA, category=category, imdb_id=imdb, douban_id=douban)
    boundary.request.get_res.return_value = _response(_page([_group(media=media)]))
    _, rows = colapt.ColaPTSpider(INDEXER).search(None)
    torrent = TorrentInfo(**rows[0])
    assert (torrent.category, torrent.media_source, torrent.media_id) == (
        expected_type.value, expected_source, expected_id,
    )


def test_unassociated_torrent_and_works_without_versions(boundary):
    """
    未关联作品的种子保留且分类未知，没有版本的作品分组被跳过而不是整页失败。
    """
    item = dict(ITEM, subtitle=None, seeders=None, leechers=None, completions=None)
    empty = {"group_id": "media:9", "media": MEDIA, "versions": []}
    boundary.request.get_res.return_value = _response(_page([_group(item, media=None), empty]))
    _, rows = colapt.ColaPTSpider(INDEXER).search(None)
    assert len(rows) == 1
    assert rows[0]["category"] == MediaType.UNKNOWN.value and rows[0]["description"] is None
    assert (rows[0]["seeders"], rows[0]["peers"], rows[0]["grabs"]) == (0, 0, 0)
    assert "media_id" not in rows[0]


@pytest.mark.parametrize("promotion,expected", [
    (None, (1, 1, None)),
    ({"upload": {"num": 1, "den": 1}, "download": {"num": 0, "den": 1}, "ends_at": None}, (0, 1, None)),
    ({"upload": {"num": 1, "den": 1}, "download": {"num": 1, "den": 1}, "ends_at": "2026-11-20T10:26:49Z"},
     (1, 1, None)),
    ({"upload": {"num": 3, "den": 2}, "download": {"num": 3, "den": 10}, "ends_at": "2026-11-20T10:26:49Z"},
     (0.3, 1.5, _local("2026-11-20T10:26:49+00:00"))),
])
def test_promotion_is_current_billing_and_null_means_normal(boundary, promotion, expected):
    """
    促销倍率为服务端当前计费的分数；无促销即正常计费，没有生效促销时不报结束时间。
    """
    boundary.request.get_res.return_value = _response(_page([_group(dict(ITEM, promotion=promotion))]))
    _, rows = colapt.ColaPTSpider(INDEXER).search(None)
    assert (rows[0]["downloadvolumefactor"], rows[0]["uploadvolumefactor"], rows[0]["freedate"]) == expected


def test_page_numbers_are_translated_and_last_page_detected(boundary):
    """
    MoviePilot 零起始页码换成接口的一起始页码，末页依据服务端 total_pages。
    """
    boundary.request.get_res.return_value = _response(_page(page=3, total_pages=3))
    with capture_site_search_observation() as observation:
        assert colapt.ColaPTSpider(INDEXER).search(None, page=2)[0] is False
    assert boundary.request.get_res.call_args.kwargs["params"]["page"] == 3
    assert observation.has_more is False and observation.raw_count == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code,status,expected", [
    ("invalid_credentials", 401, "已失效"), ("api_key_scope_denied", 403, "读取"),
    ("password_change_required", 403, "修改密码"), ("permission_denied", 403, "无权"),
    ("service_unavailable", 503, "HTTP 503"), ("invalid_request", 400, "HTTP 400"),
    ("rate_limited", 429, "限流"),
])
@pytest.mark.asyncio
async def test_api_errors_are_not_empty_success(boundary, asynchronous, code, status, expected):
    """
    按错误码区分失败原因，限流进入共享冷却，错误消息不回显密钥。
    """
    response = _error(code, status, "synthetic-colapt-key", retry_after=60)
    request = boundary.async_request if asynchronous else boundary.request
    request.get_res.return_value = response
    spider = colapt.ColaPTSpider(INDEXER)
    if status == 429:
        with pytest.raises(RateLimitExceededException, match="60 秒"):
            if asynchronous:
                await spider.async_search(None)
            else:
                spider.search(None)
        boundary.defer.assert_called_once()
    else:
        assert (await spider.async_search(None) if asynchronous else spider.search(None)) == (True, [])
        boundary.defer.assert_not_called()
    assert expected in spider.error_detail and "synthetic-colapt-key" not in spider.error_detail
    (response.aclose if asynchronous else response.close).assert_called_once()


@pytest.mark.parametrize("data", [None, [], {}, {"items": None, "page": 1, "total_pages": 1},
                                  {"items": [None], "page": 1, "total_pages": 1},
                                  {"items": [{"versions": None}], "page": 1, "total_pages": 1},
                                  _page(page=2), {"error": {"code": "internal_error"}}])
def test_invalid_page_structure_is_explicit_failure(boundary, data):
    """
    缺少列表、条目结构无效、错页或成功状态下的错误信封都不能被当成成功空页。
    """
    boundary.request.get_res.return_value = _response(data)
    spider = colapt.ColaPTSpider(INDEXER)
    assert spider.search(None) == (True, []) and spider.error_detail


@pytest.mark.parametrize("field,value", [("id", 0), ("id", True), ("release_name", " "), ("size_bytes", -1),
                                         ("size_bytes", 1.5), ("seeders", -1), ("published_at", "yesterday"),
                                         ("published_at", "2026-11-10T10:26:49"),
                                         ("promotion", {"upload": {"num": 1, "den": 0},
                                                        "download": {"num": 1, "den": 1}, "ends_at": None}),
                                         ("promotion", "synthetic-colapt-key"), ("hit_and_run", "yes")])
def test_invalid_required_fields_reject_entire_page(boundary, field, value):
    """
    必要字段无效时拒绝整页，不静默丢记录，也不在错误中回显原始值。
    """
    item = dict(ITEM)
    item[field] = value
    boundary.request.get_res.return_value = _response(_page([_group(item)]))
    spider = colapt.ColaPTSpider(INDEXER)
    assert spider.search(None) == (True, [])
    assert "synthetic-colapt-key" not in spider.error_detail


def test_empty_list_is_success_and_no_response_is_failure(boundary):
    """
    明确的空列表与连接失败分别保留成功和失败语义。
    """
    spider = colapt.ColaPTSpider(INDEXER)
    boundary.request.get_res.return_value = _response(_page([], total_pages=0))
    assert spider.search(None) == (False, [])
    boundary.request.get_res.return_value = None
    assert spider.search(None) == (True, [])
    assert spider.error_detail == "无法连接 ColaPT API"


@pytest.mark.parametrize("status", [200, 502])
def test_non_json_response_does_not_escape_or_leak_body(boundary, status):
    """
    网页、WAF 和网关错误页不能作为 API 成功或暴露原始正文。
    """
    response = _response({}, status)
    response._content = b"<html>synthetic-colapt-key</html>"
    boundary.request.get_res.return_value = response
    spider = colapt.ColaPTSpider(INDEXER)
    assert spider.search(None) == (True, [])
    assert "synthetic-colapt-key" not in spider.error_detail
    response.close.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_missing_key_never_uses_cookie(boundary, asynchronous):
    """
    没有密钥时明确提示配置，不发请求、不生成配方，也不回退 Cookie。
    """
    spider = colapt.ColaPTSpider(dict(INDEXER, apikey=" ", token="unused-token"))
    result = await spider.async_search(None) if asynchronous else spider.search(None)
    assert result == (True, []) and spider.error_detail == colapt.KEY_REQUIRED
    assert spider.get_download_url(42) == ""
    boundary.factory.assert_not_called()
    boundary.async_factory.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_indexer_dispatch_preserves_site_and_priority(boundary, monkeypatch, asynchronous):
    """
    真实索引模块选择开放 API 爬虫，并保留来源、优先级、代理和媒体身份。
    """
    health = Mock(async_success=AsyncMock(), async_fail=AsyncMock())
    monkeypatch.setattr(indexer_module, "get_configured_site_health_service", lambda: health)
    monkeypatch.setattr(indexer_module, "SitesHelper", Mock(return_value=Mock(check=Mock(return_value=(False, "")))))
    module = object.__new__(IndexerModule)
    torrents = (await module.async_search_torrents(INDEXER, keyword="tt1234567") if asynchronous else
                module.search_torrents(INDEXER, keyword="tt1234567"))
    assert len(torrents) == 1
    assert (torrents[0].site, torrents[0].site_name, torrents[0].site_order) == (9101, "可乐", 3)
    assert torrents[0].site_proxy is True and torrents[0].media_id == "tt1234567"
    (health.async_success if asynchronous else health.success).assert_called_once()


def test_checkpoint_restoration_rebuilds_recipe_with_current_key(boundary):
    """
    检查点不持久化密钥，恢复时按详情页路径中的 ID 用当前密钥重建配方，不重新搜索。
    """
    original = TorrentInfo(site=9101, title="Example", page_url="https://www.colapt.com/torrents/42",
                           site_cookie="private-cookie",
                           enclosure=colapt.ColaPTSpider(INDEXER).get_download_url(42))
    snapshot = torrent_snapshot(original)
    assert snapshot["enclosure"] is None and snapshot["page_url"] == "https://www.colapt.com/torrents/42"
    assert "synthetic-colapt-key" not in json.dumps(snapshot) and "private-cookie" not in json.dumps(snapshot)
    torrent = IndexerModule.restore_search_torrent(dict(INDEXER, apikey="new-synthetic-key"), snapshot)
    assert torrent is not None
    recipe, url = _recipe(torrent.enclosure)
    assert recipe["header"]["X-API-Key"] == "new-synthetic-key" and recipe["torrent_id"] == 42
    assert url.endswith("/torrents/42/download-links")
    boundary.factory.assert_not_called()


@pytest.mark.parametrize("url,identity,expiring", [
    ("https://www.colapt.com/torrents/42", "42", False),
    ("https://www.colapt.com/downloads/synthetic-credential/42?exp=1795000000&sig=abc", "42", True),
    ("https://www.colapt.com/downloads/synthetic-credential/42", "42", False),
    ("https://unit3d.test/torrents/42", None, False),
    ("https://www.colapt.com/media/rc201", None, False),
])
def test_site_paths_give_stable_identity_and_signed_links(url, identity, expiring):
    """
    只解释本站详情页和签名下载路径，其他站点同形路径不被误认。
    """
    assert colapt.ColaPTSpider.torrent_id_from_url(url) == identity
    assert IndexerModule.is_expiring_download_url(url) is expiring
    resource = IndexerModule.torrent_resource_identity(TorrentInfo(site=9101, page_url=url))
    assert resource == ((("9101", f"id={identity}")) if identity else None)


@pytest.mark.parametrize("resource_key", ["schema", "parser"])
def test_connectivity_uses_stats_by_protocol(monkeypatch, resource_key):
    """
    资源声明 ColaPT 后连接检测改用会员数据接口，不访问 Cookie 页面。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=9101, name="可乐", domain="colapt.com", url="https://www.colapt.com/",
                        apikey="synthetic-key", cookie="unused-cookie", ua="ColaPT-Test", proxy=1, timeout=25)
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={
        resource_key: "ColaPT",
    }))))
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(chain, "run_module", lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"},
    ))
    monkeypatch.setattr(colapt, "get_runtime_setting", lambda key: {
        "PROXY": chain.runtime_config.proxy or {}, "USER_AGENT": chain.runtime_config.user_agent,
    }[key])
    wait = Mock()
    monkeypatch.setattr(colapt, "wait_for_site_request", wait)
    response = _response({"user": {"id": 42, "username": "synthetic-user", "avatar_url": None}, "ratio": None})
    request = Mock(get_res=Mock(return_value=response))
    constructor = Mock(return_value=request)
    monkeypatch.setattr(colapt, "RequestUtils", constructor)
    generic = Mock(side_effect=AssertionError("不能访问 Cookie 页面"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic)
    assert chain.test(site.url) == (True, "连接成功")
    request.get_res.assert_called_once_with(url="https://www.colapt.com/api/v1/me/stats", allow_redirects=False)
    headers = constructor.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == "synthetic-key" and "Cookie" not in headers and "Origin" not in headers
    wait.assert_called_once()
    generic.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("response,expected,deferred", [
    (_error("invalid_credentials", 401), "已失效", False),
    (_error("api_key_scope_denied", 403), "读取", False),
    (_error("rate_limited", 429, retry_after=60), "限流", True),
    (_response({"unexpected": True}), "无效", False),
    (_response({"user": {"id": 0, "username": "synthetic-user"}}), "无效", False),
    (None, "无法连接", False),
])
def test_connection_failures_are_explicit(boundary, response, expected, deferred):
    """
    连接检测的认证、范围、限流和结构错误分别提示，限流进入共享冷却。
    """
    boundary.request.get_res.return_value = response
    state, message = colapt.ColaPTSpider(INDEXER).test_connection()
    assert state is False and expected in message
    assert boundary.defer.called is deferred
    assert colapt.ColaPTSpider(dict(INDEXER, apikey="")).test_connection() == (False, colapt.KEY_REQUIRED)


@pytest.mark.parametrize("value", [True, [], "-1", "1e3", " 1", "private-value", None])
def test_invalid_api_number_does_not_echo_input(value):
    """
    非十进制字符串、布尔值和空值转换为固定错误，不回显私人统计字段。
    """
    with pytest.raises(ValueError, match="ColaPT API 数值字段无效"):
        colapt.decimal(value)
    assert colapt.decimal("-12.5000", signed=True) == -12.5
