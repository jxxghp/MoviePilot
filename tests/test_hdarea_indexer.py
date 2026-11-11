"""
验证高清视界 API 的搜索、连接检测和下载配方，不使用真实凭据或网络。
"""

import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from requests import Response

import app.chain.site as site_chain
import app.modules.indexer as indexer_module
import app.modules.indexer.spider.hdarea as hdarea
from app.application.search.session import torrent_snapshot
from app.application.site.contract import SiteSnapshot
from app.application.site.observation import capture_site_search_observation
from app.chain.site import SiteChain
from app.domain.context import TorrentInfo
from app.modules.indexer import _SPECIALIZED_SEARCH_ARGUMENTS, SPIDER_PARSER_CLASSES, IndexerModule
from app.modules.indexer.parser import SiteSchema
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType

# 所有凭据、标题和账号均为合成值；响应字段依据官方文档和脱敏联调结构。
INDEXER = {
    "id": 7370, "name": "高清视界", "domain": "https://hdarea.club/", "url": "https://hdarea.club/",
    "parser": "HDArea", "schema": "HDArea", "apikey": "synthetic-hdarea-key", "cookie": "unused-cookie",
    "ua": "HDArea-Test", "proxy": True, "timeout": 25, "pri": 3,
    "category": {"movie": [{"id": 410}, {"id": 404}, {"id": 405}],
                 "tv": [{"id": 402}, {"id": 403}, {"id": 404}, {"id": 405}],
                 "music": [{"id": 406}, {"id": 408}]},
}
ITEM = {
    "id": 42, "name": "Example Movie 2026 1080p BluRay", "small_descr": "合成电影 / 国英双语",
    "category_id": 410, "torznab_cat": 2000, "size": 13159379567, "seeders": 4,
    "leechers": 2, "times_completed": 14, "added": "2026-11-10 18:26:49",
    "download_volume_factor": 0.5, "upload_volume_factor": 2, "sp_state": 6,
    "promotion_until": "2026-11-20 18:26:49", "imdb_id": "tt1234567", "douban_id": "123456",
    "detail_url": "https://hdarea.club/details.php?id=42",
    "download_url": "https://hdarea.club/download.php?id=42&dl_token=old-synthetic-ticket", "tags": ["中字"],
}


def _response(data, status=200, code=0, success=True, message="OK"):
    """
    构造具有真实 HTTP 布尔语义的响应，关闭动作保留为可验证替身。
    """
    response = Response()
    response.status_code = status
    response._content = json.dumps({"success": success, "code": code, "message": message, "data": data}).encode()
    response.encoding = "utf-8"
    response.close = Mock()
    response.aclose = AsyncMock()
    return response


def _list(items=None, page=0, has_more=False):
    return {"items": deepcopy([ITEM] if items is None else items),
            "pagination": {"page": page, "per_page": 100, "has_more": has_more, "total": 3}}


def _recipe(enclosure):
    encoded, url = enclosure[1:].split("]", 1)
    return json.loads(base64.b64decode(encoded)), url


def _connectivity_adapter(monkeypatch, chain, response=None):
    """
    保留注册表和真实索引分发，仅替换适配器 HTTP、等待与代理配置边界。
    """
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(chain, "run_module", lambda method, **kwargs: getattr(module, method)(**kwargs))
    monkeypatch.setattr(hdarea, "get_runtime_setting", lambda key: {
        "PROXY": chain.runtime_config.proxy or {}, "USER_AGENT": chain.runtime_config.user_agent,
    }[key])
    wait, defer = Mock(), Mock()
    monkeypatch.setattr(hdarea, "wait_for_site_request", wait)
    monkeypatch.setattr(hdarea, "defer_site_request", defer)
    request = Mock(get_res=Mock(return_value=response))
    constructor = Mock(return_value=request)
    monkeypatch.setattr(hdarea, "RequestUtils", constructor)
    return wait, defer, request, constructor


@pytest.fixture
def boundary(monkeypatch):
    """
    隔离传输、等待和运行配置，保留真实参数构造与响应解析。
    """
    monkeypatch.setattr(hdarea, "get_runtime_setting", lambda key: {
        "USER_AGENT": "Default-UA", "PROXY": {"https": "proxy"},
    }[key])
    wait, async_wait, defer = Mock(), AsyncMock(), Mock()
    monkeypatch.setattr(hdarea, "wait_for_site_request", wait)
    monkeypatch.setattr(hdarea, "async_wait_for_site_request", async_wait)
    monkeypatch.setattr(hdarea, "defer_site_request", defer)
    response = _response(_list())
    request = Mock(get_res=Mock(return_value=response))
    factory = Mock(return_value=request)
    async_request = Mock(get_res=AsyncMock(return_value=response))
    async_factory = Mock(return_value=async_request)
    monkeypatch.setattr(hdarea, "RequestUtils", factory)
    monkeypatch.setattr(hdarea, "AsyncRequestUtils", async_factory)
    return SimpleNamespace(response=response, request=request, factory=factory, async_request=async_request,
                           async_factory=async_factory, wait=wait, async_wait=async_wait, defer=defer)


def test_hdarea_protocol_is_registered() -> None:
    """
    站点资源中的 HDArea 协议能够选择专用的搜索和资料解析器。
    """
    assert "HDArea" in SPIDER_PARSER_CLASSES
    assert _SPECIALIZED_SEARCH_ARGUMENTS["HDArea"] == ("keyword", "mtype", "cat", "page")
    assert SiteSchema.HDArea.value == "HDArea"
    assert IndexerModule.get_search_page_size(INDEXER) == 100


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("keyword", ["合成电影 2026", "tt1234567", None, "  "])
@pytest.mark.asyncio
async def test_search_uses_key_only_and_projects_official_fields(boundary, asynchronous, keyword):
    """
    同步、异步、IMDb 和最新列表都只使用 API Key，搜索时不请求下载票据。
    """
    spider = hdarea.HDAreaSpider(INDEXER)
    result = await spider.async_search(keyword) if asynchronous else spider.search(keyword)
    assert result[0] is False and len(result[1]) == 1
    torrent = TorrentInfo(**result[1][0])
    assert (torrent.title, torrent.description, torrent.size) == (ITEM["name"], ITEM["small_descr"], ITEM["size"])
    assert (torrent.seeders, torrent.peers, torrent.grabs) == (4, 2, 14)
    assert (torrent.downloadvolumefactor, torrent.uploadvolumefactor) == (0.5, 2)
    assert torrent.freedate == "2026-11-20 18:26:49"
    assert (torrent.media_source, torrent.media_id, torrent.category) == (
        MediaSource.IMDb, "tt1234567", MediaType.MOVIE.value,
    )
    assert torrent.labels == ["中字"]
    recipe, url = _recipe(torrent.enclosure)
    assert url == "https://hdarea.club/api/v1/torrents/genDlToken"
    assert recipe["json"] == {"id": 42} and recipe["cookie"] is False
    assert recipe["header"]["X-API-Key"] == "synthetic-hdarea-key"
    assert recipe["allow_redirects"] is False and recipe["proxy"] is True
    assert "old-synthetic-ticket" not in torrent.enclosure
    request = boundary.async_request if asynchronous else boundary.request
    factory = boundary.async_factory if asynchronous else boundary.factory
    expected = {"page": 0, "per_page": 100, "sort": "added", "order": "desc"}
    if keyword and keyword.strip():
        expected["imdb" if keyword.startswith("tt") else "keyword"] = keyword
    request.get_res.assert_called_once_with(url="https://hdarea.club/api/v1/torrents", params=expected,
                                           allow_redirects=False)
    assert factory.call_args.kwargs == {
        "headers": {"X-API-Key": "synthetic-hdarea-key", "Accept": "application/json", "User-Agent": "HDArea-Test"},
        "proxies": {"https": "proxy"}, "timeout": 25,
    }
    (boundary.async_wait if asynchronous else boundary.wait).assert_called_once()
    (boundary.response.aclose if asynchronous else boundary.response.close).assert_called_once()


@pytest.mark.parametrize("mtype,cat,expected", [
    (MediaType.TV, None, "402,403,404,405"), (MediaType.MUSIC, None, "406,408"),
    (MediaType.MOVIE, None, "410,404,405"), (MediaType.TV, "406,408", "406,408"),
])
def test_categories_use_one_global_query(boundary, mtype, cat, expected):
    """
    分类合并在一次请求内完成，显式分类优先，不合并不同分类各自的分页。
    """
    assert hdarea.HDAreaSpider(INDEXER).search(None, mtype=mtype, cat=cat)[0] is False
    assert boundary.request.get_res.call_args.kwargs["params"]["cat"] == expected
    boundary.request.get_res.assert_called_once()


def test_page_metadata_keeps_short_page_has_more(boundary):
    """
    短页仍可有下一页，估算总数不覆盖站点返回的 has_more。
    """
    boundary.request.get_res.return_value = _response(_list(page=2, has_more=True))
    with capture_site_search_observation() as observation:
        assert hdarea.HDAreaSpider(INDEXER).search(None, page=2)[0] is False
    assert observation.has_more is True and observation.raw_count == 1
    assert boundary.request.get_res.call_args.kwargs["params"]["page"] == 2


@pytest.mark.parametrize("category,imdb,douban,expected_type,expected_source,expected_id", [
    (402, "", "123456", MediaType.TV, MediaSource.Douban, "123456"),
    (406, "", "", MediaType.MUSIC, None, None),
    (404, "", "", MediaType.UNKNOWN, None, None),
    (405, "", "", MediaType.UNKNOWN, None, None),
    (999, "invalid", "invalid", MediaType.UNKNOWN, None, None),
])
def test_category_and_media_identity_are_not_guessed(boundary, category, imdb, douban,
                                                   expected_type, expected_source, expected_id):
    """
    正式媒体标识用于识别，共享或未知分类不靠聚合 Torznab 编号强行归类。
    """
    item = dict(ITEM, category_id=category, imdb_id=imdb, douban_id=douban)
    boundary.request.get_res.return_value = _response(_list([item]))
    _, rows = hdarea.HDAreaSpider(INDEXER).search(None)
    torrent = TorrentInfo(**rows[0])
    assert (torrent.category, torrent.media_source, torrent.media_id) == (
        expected_type.value, expected_source, expected_id,
    )


@pytest.mark.parametrize("download,upload,expected_date", [
    (0, 1, "2026-11-20 18:26:49"), (0, 2, "2026-11-20 18:26:49"),
    (0.3, 1, "2026-11-20 18:26:49"), (1, 1, None), (None, None, None),
])
def test_current_factors_override_stale_promotion_enum(boundary, download, upload, expected_date):
    """
    服务端当前倍率优先于过期促销枚举，缺失倍率保持未知而非免费。
    """
    item = dict(ITEM, sp_state=4, download_volume_factor=download, upload_volume_factor=upload)
    boundary.request.get_res.return_value = _response(_list([item]))
    _, rows = hdarea.HDAreaSpider(INDEXER).search(None)
    assert rows[0]["downloadvolumefactor"] == download
    assert rows[0]["uploadvolumefactor"] == upload
    assert rows[0]["freedate"] == expected_date


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status,code,success", [(401, 401, False), (403, 403, False), (500, 0, True),
                                                (200, 3, True), (200, 0, False), (429, 429, False)])
@pytest.mark.asyncio
async def test_api_failures_are_not_empty_success(boundary, asynchronous, status, code, success):
    """
    HTTP、业务码和 success 任一失败即拒绝结果，限流保留原因且不泄露凭据。
    """
    response = _response(_list(), status, code, success, "限流请等待 60 秒" if status == 429 else "synthetic-hdarea-key")
    request = boundary.async_request if asynchronous else boundary.request
    request.get_res.return_value = response
    spider = hdarea.HDAreaSpider(INDEXER)
    if status == 429:
        with pytest.raises(RateLimitExceededException, match="限流"):
            if asynchronous:
                await spider.async_search(None)
            else:
                spider.search(None)
        boundary.defer.assert_called_once()
    else:
        result = await spider.async_search(None) if asynchronous else spider.search(None)
        assert result == (True, [])
    assert spider.error_detail and "synthetic-hdarea-key" not in spider.error_detail
    (response.aclose if asynchronous else response.close).assert_called_once()


@pytest.mark.parametrize("data", [None, [], {}, {"items": [], "pagination": None},
                                  {"items": [None], "pagination": {"page": 0}}, _list(page=1)])
def test_invalid_list_structure_is_explicit_failure(boundary, data):
    """
    缺少列表、无效信封或错页不能被默认为成功空页。
    """
    boundary.request.get_res.return_value = _response(data)
    spider = hdarea.HDAreaSpider(INDEXER)
    assert spider.search(None) == (True, []) and spider.error_detail


@pytest.mark.parametrize("field,value", [("id", 0), ("id", True), ("name", " "), ("size", -1),
                                         ("seeders", 1.5), ("upload_volume_factor", "nan"),
                                         ("download_volume_factor", "synthetic-hdarea-key")])
def test_invalid_required_fields_reject_entire_page(boundary, field, value):
    """
    必要字段无效时拒绝整页，不静默丢记录，也不在错误中回显原始密钥。
    """
    item = dict(ITEM)
    item[field] = value
    boundary.request.get_res.return_value = _response(_list([item]))
    spider = hdarea.HDAreaSpider(INDEXER)
    assert spider.search(None) == (True, [])
    assert "synthetic-hdarea-key" not in spider.error_detail


def test_empty_list_is_success_and_no_response_is_failure(boundary):
    """
    明确的空列表与连接失败分别保留成功和失败语义。
    """
    spider = hdarea.HDAreaSpider(INDEXER)
    boundary.request.get_res.return_value = _response(_list([]))
    assert spider.search(None) == (False, [])
    boundary.request.get_res.return_value = None
    assert spider.search(None) == (True, [])
    assert spider.error_detail == "无法连接 HDArea API"


@pytest.mark.parametrize("status", [200, 500])
def test_non_json_response_does_not_escape_or_leak_body(boundary, status):
    """
    登录页、WAF 和空 500 响应不能作为 API 成功或暴露原始正文。
    """
    response = _response({}, status)
    response._content = b"<html>synthetic-hdarea-key</html>"
    boundary.request.get_res.return_value = response
    spider = hdarea.HDAreaSpider(INDEXER)
    assert spider.search(None) == (True, [])
    assert "synthetic-hdarea-key" not in spider.error_detail
    response.close.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_missing_key_never_uses_cookie(boundary, asynchronous):
    """
    没有密钥时明确提示配置，不发请求、生成令牌或回退 Cookie。
    """
    spider = hdarea.HDAreaSpider(dict(INDEXER, apikey=" ", token="unused-token"))
    result = await spider.async_search(None) if asynchronous else spider.search(None)
    assert result == (True, []) and spider.error_detail == hdarea.KEY_REQUIRED
    assert spider.get_download_url(42) == ""
    boundary.factory.assert_not_called()
    boundary.async_factory.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_indexer_dispatch_preserves_site_and_priority(boundary, monkeypatch, asynchronous):
    """
    真实索引模块选择 API 爬虫，并保留来源、优先级、代理和资源身份。
    """
    health = Mock(async_success=AsyncMock(), async_fail=AsyncMock())
    monkeypatch.setattr(indexer_module, "get_configured_site_health_service", lambda: health)
    monkeypatch.setattr(indexer_module, "SitesHelper", Mock(return_value=Mock(check=Mock(return_value=(False, "")))))
    module = object.__new__(IndexerModule)
    torrents = (await module.async_search_torrents(INDEXER, keyword="tt1234567") if asynchronous else
                module.search_torrents(INDEXER, keyword="tt1234567"))
    assert len(torrents) == 1
    assert (torrents[0].site, torrents[0].site_name, torrents[0].site_order) == (7370, "高清视界", 3)
    assert torrents[0].site_proxy is True and torrents[0].media_id == "tt1234567"
    (health.async_success if asynchronous else health.success).assert_called_once()


def test_checkpoint_restoration_rebuilds_recipe_with_current_key(boundary):
    """
    检查点不持久化密钥，恢复时按详情 ID 用当前密钥重建配方，不重新搜索。
    """
    original = TorrentInfo(site=7370, title="Example", page_url=ITEM["detail_url"], site_cookie="private-cookie",
                           enclosure=hdarea.HDAreaSpider(INDEXER).get_download_url(42))
    snapshot = torrent_snapshot(original)
    assert snapshot["enclosure"] is None and snapshot["page_url"] == ITEM["detail_url"]
    assert "synthetic-hdarea-key" not in json.dumps(snapshot) and "private-cookie" not in json.dumps(snapshot)
    torrent = IndexerModule.restore_search_torrent(dict(INDEXER, apikey="new-synthetic-key"), snapshot)
    assert torrent is not None
    recipe, _url = _recipe(torrent.enclosure)
    assert recipe["header"]["X-API-Key"] == "new-synthetic-key"
    assert recipe["json"] == {"id": 42}
    boundary.factory.assert_not_called()


@pytest.mark.parametrize("domain,resource_key", [("hdarea.club", "schema"), ("mirror.test", "schema"),
                                                ("mirror.test", "parser")])
@pytest.mark.parametrize("render", [0, 1])
def test_connectivity_uses_profile_by_domain_or_protocol(monkeypatch, domain, resource_key, render):
    """
    域名和 schema/parser 路由均使用相同 API，浏览器开关不改变认证方式。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=7370, name="高清视界", domain=domain, url=f"https://{domain}/",
                        apikey="synthetic-key", cookie="unused-cookie", ua="HDArea-Test", render=render,
                        proxy=1, timeout=25)
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={
        resource_key: "HDArea", "api_url": "https://api.custom.test/v1/",
    }))))
    response = _response({"id": 42, "enabled": True})
    wait, _defer, request, constructor = _connectivity_adapter(monkeypatch, chain, response)
    generic = Mock(side_effect=AssertionError("不能访问 Cookie 页面"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic)
    assert chain.test(site.url) == (True, "连接成功")
    request.get_res.assert_called_once_with(url="https://api.custom.test/v1/user/profile", allow_redirects=False)
    options = constructor.call_args.kwargs
    assert options["headers"]["X-API-Key"] == "synthetic-key" and "cookies" not in options
    assert options["headers"]["User-Agent"] == "HDArea-Test"
    assert options["timeout"] == 25 and options["proxies"] == (chain.runtime_config.proxy or {})
    wait.assert_called_once()
    generic.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("status,data,code,expected", [(401, {}, 401, "API Key"), (403, {}, 403, "白名单"),
                                                     (429, {}, 429, "限流"), (500, {}, 0, "HTTP 500"),
                                                     (200, {"id": 0}, 0, "无效"),
                                                     (200, {"id": 42, "enabled": False}, 0, "禁用")])
def test_connectivity_rejects_api_errors_and_invalid_identity(monkeypatch, status, data, code, expected):
    """
    连通性必须取得有效用户身份，错误及被禁用账号不能通过检测。
    """
    chain = SiteChain()
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={}))))
    _wait, defer, _request, _constructor = _connectivity_adapter(monkeypatch, chain, _response(data, status, code))
    state, message = chain.special_site_test["hdarea.club"](SiteSnapshot(
        id=7370, name="高清视界", url="https://hdarea.club/", apikey="synthetic-key",
    ))
    assert state is False and expected in message
    assert defer.called is (status == 429)


def test_connectivity_without_key_or_response_has_clear_failure(monkeypatch):
    """
    缺少配置与网络无响应保留不同提示，缺少密钥时不调用网络端口。
    """
    chain = SiteChain()
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={}))))
    _wait, _defer, request, constructor = _connectivity_adapter(monkeypatch, chain)
    site = SiteSnapshot(id=7370, name="高清视界", url="https://hdarea.club/", cookie="unused-cookie")
    assert chain.special_site_test["hdarea.club"](site) == (False, hdarea.KEY_REQUIRED)
    constructor.assert_not_called()
    site = SiteSnapshot(id=7370, name="高清视界", url="https://hdarea.club/", apikey="synthetic-key")
    assert chain.special_site_test["hdarea.club"](site) == (False, "无法连接 HDArea API")
    request.get_res.assert_called_once()


def test_connectivity_rejects_non_json_profile(monkeypatch):
    """
    API 返回登录页或非 JSON 时连接测试失败，响应正文不进入错误消息。
    """
    chain = SiteChain()
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={}))))
    response = _response({})
    response._content = b"<html>private-body</html>"
    _connectivity_adapter(monkeypatch, chain, response)
    state, message = chain.special_site_test["hdarea.club"](SiteSnapshot(
        id=7370, name="高清视界", url="https://hdarea.club/", apikey="synthetic-key",
    ))
    assert state is False and "private-body" not in message


@pytest.mark.parametrize("value", [True, [], -1, "nan", "inf", "private-value", 10 ** 400])
def test_invalid_api_number_does_not_echo_input(value):
    """
    非数值、负数、布尔值和溢出均转换为固定错误，不回显私人统计字段。
    """
    with pytest.raises(ValueError, match="HDArea API 数值字段无效"):
        hdarea.number(value)
