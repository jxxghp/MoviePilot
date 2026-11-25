"""
验证猪猪实验室接口的搜索、用户统计、连接检测与两段式下载。
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
import app.modules.indexer.spider.piggo as piggo
from app.application.site.contract import SiteSnapshot
from app.application.site.observation import capture_site_search_observation
from app.chain.site import SiteChain
from app.domain.context import TorrentInfo
from app.modules.indexer import _SPECIALIZED_SEARCH_ARGUMENTS, SPIDER_PARSER_CLASSES, IndexerModule
from app.modules.indexer.parser import SiteSchema
from app.runtime.cache import MemoryBackend
from app.schemas.exception import RateLimitExceededException
from app.schemas.types import MediaSource, MediaType

# 合成的测试凭据不对应任何真实账号。
INDEXER = {"id": 7274, "name": "猪猪", "domain": "https://piggo.me/", "url": "https://piggo.me/",
           "parser": "Piggo", "schema": "Piggo", "apikey": "synthetic-piggo-key", "cookie": "unused-cookie",
           "ua": "Piggo-Test", "proxy": True, "timeout": 25}
CATALOG = {"sections": [{"id": 11, "name": "电影"}, {"id": 22, "name": "剧集"}, {"id": 33, "name": "音乐"}],
           "categories": [{"id": 101, "section_id": 11, "name": "华语"},
                          {"id": 202, "section_id": 22, "name": "海外"},
                          {"id": 303, "section_id": 33, "name": "无损"}], "taxonomies": []}
ITEM = {"id": 56354, "name": "The Silent Rift 2026 S01 Complete 2160p", "small_descr": "全16集",
        "cover": "https://image.test/poster.jpg", "size": 78726120572, "section_id": 22,
        "category_ids": [202], "media": {"douban": "36457187", "imdb": "tt33265527", "tmdb": "", "tmdb_kind": ""},
        "seeders": 3, "leechers": 0, "times_completed": 9, "added_at": "2026-04-21 10:26:30",
        "page_url": "https://piggo.me/detail/56354"}


def _response(data, status=200, code=0):
    """
    回放实验室信封，保留 HTTP 错误的真实布尔语义。
    """
    response = Response()
    response.status_code = status
    response._content = json.dumps({"code": code, "data": data, "msg": "ok"}).encode()
    response.encoding = "utf-8"
    response.close = Mock()
    response.aclose = AsyncMock()
    return response


def _list(items=None, page=1):
    return {"items": deepcopy([ITEM] if items is None else items), "page": page, "per_page": 100,
            "pages": page, "total": 1, "total_capped": False}


def _recipe(enclosure):
    encoded, url = enclosure[1:].split("]", 1)
    return json.loads(base64.b64decode(encoded)), url


@pytest.fixture
def boundary(monkeypatch):
    """
    隔离网络、目录缓存和等待，保留真实协议构造与响应解析。
    """
    cache = MemoryBackend(maxsize=128, ttl=3600)
    monkeypatch.setattr(cache, "_region_caches", {})
    monkeypatch.setattr(piggo.PiggoSpider, "_catalog_cache", cache)
    monkeypatch.setattr(piggo, "get_runtime_setting", lambda key: {"USER_AGENT": "Default-UA", "PROXY": {"https": "proxy"}}[key])
    wait = Mock()
    async_wait = AsyncMock()
    monkeypatch.setattr(piggo, "wait_for_site_request", wait)
    monkeypatch.setattr(piggo, "async_wait_for_site_request", async_wait)
    responses = [_response(CATALOG), _response(_list())]
    request = Mock(post_res=Mock(side_effect=responses))
    factory = Mock(return_value=request)
    monkeypatch.setattr(piggo, "RequestUtils", factory)
    async_request = Mock(post_res=AsyncMock(side_effect=responses))
    async_factory = Mock(return_value=async_request)
    monkeypatch.setattr(piggo, "AsyncRequestUtils", async_factory)
    return SimpleNamespace(request=request, factory=factory, async_request=async_request,
                           async_factory=async_factory, responses=responses, wait=wait, async_wait=async_wait)


def test_piggo_is_registered_for_search_and_user_data() -> None:
    """
    猪猪改版后的站点资源必须能选择专用搜索与用户资料协议。
    """
    assert "Piggo" in SPIDER_PARSER_CLASSES
    assert _SPECIALIZED_SEARCH_ARGUMENTS["Piggo"] == ("keyword", "mtype", "cat", "page")
    assert SiteSchema.Piggo.value == "Piggo"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_search_uses_post_json_and_official_fields(boundary, asynchronous):
    """
    同步和异步均使用当前目录、正式分类与 IMDb 参数，不访问 PHP 或前台接口。
    """
    spider = piggo.PiggoSpider(INDEXER)
    result = (await spider.async_search("tt33265527", MediaType.TV, page=2) if asynchronous else
              spider.search("tt33265527", MediaType.TV, page=2))
    assert result[0] is False
    assert len(result[1]) == 1
    torrent = TorrentInfo(**result[1][0])
    assert (torrent.title, torrent.description, torrent.size) == (ITEM["name"], "全16集", ITEM["size"])
    assert (torrent.seeders, torrent.peers, torrent.grabs) == (3, 0, 9)
    assert (torrent.media_source, torrent.media_id, torrent.category) == (MediaSource.IMDb, "tt33265527", MediaType.TV.value)
    assert torrent.uploadvolumefactor is None and torrent.downloadvolumefactor is None
    assert "poster" not in result[1][0]
    recipe, url = _recipe(torrent.enclosure)
    assert url == "https://api.piggo.me/v1/api/torrents/56354/download-link"
    assert recipe["json"] == {} and recipe["cookie"] is False
    assert recipe["allow_redirects"] is False and recipe["proxy"] is True
    assert recipe["header"]["Authorization"] == "Bearer synthetic-piggo-key"
    assert recipe["header"]["User-Agent"] == "Piggo-Test"
    request = boundary.async_request if asynchronous else boundary.request
    factory = boundary.async_factory if asynchronous else boundary.factory
    assert [call.kwargs for call in request.post_res.call_args_list] == [
        {"url": "https://api.piggo.me/v1/api/catalog", "json": {}, "allow_redirects": False},
        {"url": "https://api.piggo.me/v1/api/torrents",
         "json": {"page": 3, "per_page": 100, "imdb_id": "tt33265527", "section_id": 22}, "allow_redirects": False},
    ]
    assert all(call.kwargs["headers"]["Authorization"] == "Bearer synthetic-piggo-key" for call in factory.call_args_list)
    assert all("cookies" not in call.kwargs and call.kwargs["timeout"] == 25 for call in factory.call_args_list)
    for response in boundary.responses:
        (response.aclose if asynchronous else response.close).assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_true_tags_use_section_catalog_not_taxonomies_or_title(boundary, asynchronous):
    """
    新增 tag_ids 按实际分区字典解释，国语、双语是真实标签，不从标题或二级分类猜测。
    """
    item = dict(ITEM, tag_ids=[4, "5", 4, 999], taxonomy_ids=[4])
    catalog = dict(CATALOG, tags=[{"id": 4, "name": "全站标签"}],
                   taxonomies=[{"id": 1, "name": "来源", "items": [{"id": 4, "name": "WEB-DL"}]}])
    section_tags = {"tags": [{"id": 4, "name": "国语", "color": "#ff0000", "font_color": "#ffffff"},
                              {"id": 5, "name": "双语", "description": "双音轨"}]}
    request = boundary.async_request if asynchronous else boundary.request
    request.post_res.side_effect = [_response(catalog), _response(_list([item])), _response(section_tags)]
    spider = piggo.PiggoSpider(INDEXER)
    result = await spider.async_search(None) if asynchronous else spider.search(None)
    assert result[0] is False and result[1][0]["labels"] == ["国语", "双语"]
    assert result[1][0]["category"] == MediaType.TV.value
    assert request.post_res.call_args_list[-1].kwargs == {
        "url": "https://api.piggo.me/v1/api/catalog", "json": {"section_id": 22}, "allow_redirects": False,
    }


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_section_tag_catalog_is_cached_without_reusing_other_section_names(boundary, asynchronous):
    """
    不同分区标签字典独立，同凭据后续翻页复用字典而不为每个种子重复请求目录。
    """
    items = [dict(ITEM, tag_ids=[4]), dict(ITEM, id=56355, section_id=11, category_ids=[101], tag_ids=[4])]
    responses = [_response(dict(CATALOG, tags=[])), _response(_list(items)),
                 _response({"tags": [{"id": 4, "name": "国语"}]}),
                 _response({"tags": [{"id": 4, "name": "原创"}]}), _response(_list(items))]
    request = boundary.async_request if asynchronous else boundary.request
    request.post_res.side_effect = responses
    spider = piggo.PiggoSpider(INDEXER)
    for _ in range(2):
        result = await spider.async_search(None) if asynchronous else spider.search(None)
        assert result[0] is False
        assert [row["labels"] for row in result[1]] == [["国语"], ["原创"]]
    assert request.post_res.call_count == 5
    for response in responses:
        (response.aclose if asynchronous else response.close).assert_called_once()


@pytest.mark.parametrize("tag_ids", [None, [], [999]])
def test_missing_or_unresolved_true_tags_are_not_fabricated(boundary, tag_ids):
    """
    兼容未返回标签的旧接口，无法解析的 ID 不显示成标签，也不把标题中的国语当作标签。
    """
    item = dict(ITEM, name="国语 双语 Series", taxonomy_ids=[4])
    if tag_ids is not None:
        item["tag_ids"] = tag_ids
    responses = [_response(CATALOG), _response(_list([item]))]
    if tag_ids:
        responses.append(_response({"tags": []}))
    boundary.request.post_res.side_effect = responses
    failed, result = piggo.PiggoSpider(INDEXER).search(None)
    assert not failed and result[0]["labels"] == []
    assert boundary.request.post_res.call_count == len(responses)


@pytest.mark.parametrize("tags", [[{"id": 4, "name": ""}], [{"id": True, "name": "国语"}],
                                 [{"id": 4, "name": "国语"}, {"id": 4, "name": "双语"}], {}])
def test_invalid_true_tag_dictionary_does_not_publish_partial_results(boundary, tags):
    """
    无效标签字典是接口结构失败，不发布带有猜测或半份标签的搜索结果。
    """
    boundary.request.post_res.side_effect = [_response(dict(CATALOG, tags=tags)), _response(_list())]
    assert piggo.PiggoSpider(INDEXER).search(None) == (True, [])


@pytest.mark.parametrize("tag_ids", ["4", [False], [-1], [4.5], [{}]])
def test_invalid_true_tag_ids_do_not_publish_partial_results(boundary, tag_ids):
    """
    标签 ID 保持严格整数校验，坏条目不能被静默跳过形成半页成功。
    """
    boundary.request.post_res.side_effect = [_response(dict(CATALOG, tags=[])), _response(_list([dict(ITEM, tag_ids=tag_ids)]))]
    assert piggo.PiggoSpider(INDEXER).search(None) == (True, [])


@pytest.mark.parametrize("keyword", [None, "", "  "])
def test_latest_torrents_omit_search_terms(boundary, keyword):
    """
    空关键词按同一列表接口从新到旧浏览，不构造不存在的最新或 RSS PHP 地址。
    """
    assert piggo.PiggoSpider(INDEXER).search(keyword)[0] is False
    assert boundary.request.post_res.call_args.kwargs["json"] == {"page": 1, "per_page": 100}
    assert piggo.PiggoSpider.get_search_page_size(keyword) == 100


def test_multiple_sections_use_complete_category_union(boundary):
    """
    电影涉及多个可见分区时只发一次全局分页查询，不合并不同分区的页。
    """
    catalog = deepcopy(CATALOG)
    catalog["sections"].append({"id": 44, "name": "儿童电影"})
    catalog["categories"].append({"id": 404, "section_id": 44, "name": "海外"})
    boundary.request.post_res.side_effect = [_response(catalog), _response(_list())]
    assert piggo.PiggoSpider(INDEXER).search("中文 2026", MediaType.MOVIE, page=1)[0] is False
    assert boundary.request.post_res.call_args.kwargs["json"] == {
        "page": 2, "per_page": 100, "q": "中文 2026", "category_id": [101, 404],
    }
    assert boundary.request.post_res.call_count == 2


def test_incomplete_section_categories_do_not_narrow_query(boundary):
    """
    多分区中一处分区未列分类时不缩窄查询，避免丢掉可见种子。
    """
    catalog = deepcopy(CATALOG)
    catalog["sections"].append({"id": 44, "name": "儿童电影"})
    boundary.request.post_res.side_effect = [_response(catalog), _response(_list())]
    assert piggo.PiggoSpider(INDEXER).search(None, MediaType.MOVIE)[0] is False
    assert boundary.request.post_res.call_args.kwargs["json"] == {"page": 1, "per_page": 100}


def test_explicit_category_precedes_media_type(boundary):
    """
    显式分类按正式 category_id 字段传递，去重且不混入 section_id。
    """
    assert piggo.PiggoSpider(INDEXER).search("name", MediaType.MOVIE, cat="202, 303,202")[0] is False
    assert boundary.request.post_res.call_args.kwargs["json"] == {
        "page": 1, "per_page": 100, "q": "name", "category_id": [202, 303],
    }


def test_catalog_cache_isolates_api_and_credentials(boundary):
    """
    同 API 同令牌复用目录，令牌或 API 地址变化后必须重新读取可见范围。
    """
    boundary.request.post_res.side_effect = [_response(CATALOG)] * 8
    first = piggo.PiggoSpider(INDEXER)
    assert first._load_catalog() == piggo.PiggoSpider(dict(INDEXER, cookie="changed-cookie"))._load_catalog()
    assert boundary.request.post_res.call_count == 1
    piggo.PiggoSpider(dict(INDEXER, apikey="another-synthetic-key"))._load_catalog()
    piggo.PiggoSpider(dict(INDEXER, api_url="https://api.custom.test/v1/api/"))._load_catalog()
    assert boundary.request.post_res.call_count == 3
    assert INDEXER["apikey"] not in first._cache_key


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_missing_token_never_uses_cookie_or_network(boundary, asynchronous):
    """
    只有 Cookie 时明确要求长期存取令牌，不自动登录、不生成令牌。
    """
    spider = piggo.PiggoSpider(dict(INDEXER, apikey="  ", token=None))
    result = await spider.async_search(None) if asynchronous else spider.search(None)
    assert result == (True, [])
    assert spider.error_detail == piggo.TOKEN_REQUIRED
    boundary.request.post_res.assert_not_called()
    boundary.async_request.post_res.assert_not_called()
    assert spider.get_download_url(56354) == ""


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_indexer_module_dispatches_piggo_and_preserves_identity(boundary, monkeypatch, asynchronous):
    """
    真实索引模块可路由猪猪协议，并保留作品身份、标准种子字段和站点配置。
    """
    health = Mock(async_success=AsyncMock(), async_fail=AsyncMock())
    monkeypatch.setattr(indexer_module, "get_configured_site_health_service", lambda: health)
    helper = Mock(check=Mock(return_value=(False, "")))
    monkeypatch.setattr(indexer_module, "SitesHelper", Mock(return_value=helper))
    module = object.__new__(IndexerModule)
    result = (await module.async_search_torrents(INDEXER, keyword="tt33265527", mtype=MediaType.TV) if asynchronous else
              module.search_torrents(INDEXER, keyword="tt33265527", mtype=MediaType.TV))
    assert len(result) == 1
    assert (result[0].site, result[0].media_source, result[0].media_id) == (7274, MediaSource.IMDb, "tt33265527")
    assert _recipe(result[0].enclosure)[1].endswith("/56354/download-link")
    assert result[0].downloadvolumefactor is None
    (health.async_success if asynchronous else health.success).assert_called_once()


@pytest.mark.parametrize("domain,resource_key", [("piggo.me", "schema"), ("mirror.test", "schema"), ("mirror.test", "parser")])
@pytest.mark.parametrize("data,status,code,expected", [
    ({"user": {"id": 42}}, 200, 0, "连接成功"),
    ({"user": {"id": 42, "enabled": False}}, 200, 0, "禁用"),
    ({"user": {"id": 42, "download_allowed": False}}, 200, 0, "下载权限"),
    ({"user": {"id": 0}}, 200, 0, "无效"),
    ({"user": []}, 200, 0, "有效用户资料"),
    ({}, 401, 401, "令牌"),
    ({"code": "notice_unread", "notice_gate": {"title": "必读公告"}}, 403, 0, "网页端阅读"),
    ({"message": "请稍后重试"}, 429, 429, "限流"),
    ({}, 200, 422, "失败"),
])
def test_connectivity_registration_uses_resource_adapter(monkeypatch, domain, resource_key, data, status, code, expected):
    """
    既有域名及协议注册均调用实验室适配器，Token、下载权限和公告语义一致。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=7274, name="猪猪", domain=domain, url=f"https://{domain}/", apikey="  ",
                        token=" synthetic-token ", cookie="unused-cookie", render=1, ua="Piggo-Test", timeout=25, proxy=1)
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={
        resource_key: "Piggo", "api_url": "https://api.custom.test/v1/api/",
    }))))
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(chain, "run_module", lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"},
    ))
    monkeypatch.setattr(piggo, "get_runtime_setting", lambda key: {"PROXY": {"https": "synthetic-proxy"}, "USER_AGENT": "Default-UA"}[key])
    wait = Mock()
    defer = Mock()
    monkeypatch.setattr(piggo, "wait_for_site_request", wait)
    monkeypatch.setattr(piggo, "defer_site_request", defer)
    response = _response(data, status, code)
    request = Mock(post_res=Mock(return_value=response))
    constructor = Mock(return_value=request)
    monkeypatch.setattr(piggo, "RequestUtils", constructor)
    generic = Mock(side_effect=AssertionError("猪猪不应请求首页或浏览器"))
    monkeypatch.setattr(SiteChain, "_SiteChain__test", generic)
    state, message = chain.test(site.url)
    assert state is (expected == "连接成功")
    assert expected in message
    request.post_res.assert_called_once_with(url="https://api.custom.test/v1/api/me", json={}, allow_redirects=False)
    options = constructor.call_args.kwargs
    assert options["headers"]["Authorization"] == "Bearer synthetic-token"
    assert options["headers"]["User-Agent"] == "Piggo-Test"
    assert "cookies" not in options and options["timeout"] == 25
    assert options["proxies"] == {"https": "synthetic-proxy"}
    generic.assert_not_called()
    wait.assert_called_once()
    response.close.assert_called_once()
    assert defer.called is (status == 429)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stage", ["catalog", "torrents"])
@pytest.mark.parametrize("status,code,data", [
    (401, 401, {}), (429, 429, {"message": "一分钟请求过多"}),
    (403, 0, {"code": "notice_unread", "notice_gate": {"title": "公告"}}),
    (200, 422, {"message": "无效参数"}),
])
@pytest.mark.asyncio
async def test_api_errors_stop_search_and_invalidate_catalog(boundary, monkeypatch, asynchronous, stage, status, code, data):
    """
    目录或列表的 HTTP、业务、公告与限流失败都不发布结果，失效目录不再复用。
    """
    error = _response(data, status, code)
    responses = [error] if stage == "catalog" else [_response(CATALOG), error]
    request = boundary.async_request if asynchronous else boundary.request
    request.post_res.side_effect = responses
    defer = Mock()
    monkeypatch.setattr(piggo, "defer_site_request", defer)
    spider = piggo.PiggoSpider(INDEXER)
    if status == 429:
        with pytest.raises(RateLimitExceededException, match="限流"):
            if asynchronous:
                await spider.async_search("title")
            else:
                spider.search("title")
    else:
        result = await spider.async_search("title") if asynchronous else spider.search("title")
        assert result == (True, []) and spider.error_detail
    assert request.post_res.call_count == len(responses)
    assert spider._catalog_cache.get(spider._cache_key, region=spider._catalog_region) is None
    assert defer.called is (status == 429)
    for response in responses:
        (response.aclose if asynchronous else response.close).assert_called_once()


@pytest.mark.parametrize("media,section,categories,source,identity,mtype", [
    ({"douban": "36457187"}, 22, [202], MediaSource.Douban, "36457187", MediaType.TV),
    ({"tmdb": "42", "tmdb_kind": "movie"}, 22, [202], MediaSource.TMDB, "42", MediaType.MOVIE),
    ({}, 999, [999], None, None, MediaType.UNKNOWN),
    ({}, 33, [303], None, None, MediaType.MUSIC),
    ({}, 11, [101, 202], None, None, MediaType.MOVIE),
])
def test_media_identity_and_unknown_category_mapping(boundary, media, section, categories, source, identity, mtype):
    """
    正式作品字段决定身份，未知或冲突目录不伪造电影、电视剧和促销信息。
    """
    item = deepcopy(ITEM)
    item.update(media=media, section_id=section, category_ids=categories, page_url=None)
    boundary.request.post_res.side_effect = [_response(CATALOG), _response(_list([item]))]
    error, result = piggo.PiggoSpider(INDEXER).search(None)
    assert not error
    assert (result[0]["media_source"], result[0]["media_id"], result[0]["category"]) == (source, identity, mtype.value)
    assert result[0]["page_url"] == ITEM["page_url"]


@pytest.mark.parametrize("change", [{"id": 0}, {"id": 1.5}, {"id": True}, {"name": ""}, {"size": -1},
                                    {"seeders": "invalid"}, {"media": []}, {"category_ids": "202"},
                                    {"small_descr": {}}, {"page_url": []}, {"added_at": {}},
                                    {"media": {"imdb": {}}}, {"media": {"imdb": "not-an-imdb-id"}},
                                    {"media": {"douban": True}}, {"media": {"tmdb": "not-an-id"}}])
def test_bad_torrent_record_does_not_publish_partial_page(boundary, change):
    """
    第二条种子无效时整页失败，不悄悄跳过坏记录或把浮点 ID 截断。
    """
    item = dict(ITEM, id=56355)
    item.update(change)
    boundary.request.post_res.side_effect = [_response(CATALOG), _response(_list([ITEM, item]))]
    assert piggo.PiggoSpider(INDEXER).search(None) == (True, [])


def test_catalog_cache_expires_and_remains_bounded(boundary, monkeypatch):
    """
    目录一小时后重新读取，轮换大量令牌也不突破 128 份内存容量。
    """
    boundary.request.post_res.side_effect = [_response(CATALOG)] * 132
    first = piggo.PiggoSpider(INDEXER)
    first._load_catalog()
    region_cache = first._catalog_cache._region_caches["region:piggo-catalog"]
    clock = SimpleNamespace(now=region_cache.timer())
    monkeypatch.setattr(region_cache.timer, "_Timer__timer", lambda: clock.now)
    clock.now += 3599
    first._load_catalog()
    assert boundary.request.post_res.call_count == 1
    clock.now += 2
    first._load_catalog()
    assert boundary.request.post_res.call_count == 2
    for number in range(129):
        piggo.PiggoSpider(dict(INDEXER, apikey=f"synthetic-key-{number}"))._load_catalog()
    assert len(region_cache) == region_cache.maxsize == 128
    assert region_cache.ttl == 3600
    assert first._catalog_cache.get(first._cache_key, region=first._catalog_region) is None


@pytest.mark.parametrize("apikey,token,expected", [(" key ", "other", "key"), ("  ", " token ", "token"),
                                                  (None, " token ", "token"), (None, None, "")])
def test_token_selection(apikey, token, expected):
    """
    非空 API Key 优先，只有空白时兼容 Token。
    """
    assert piggo.access_token(apikey, token) == expected


def test_checkpoint_restores_recipe_using_current_token(boundary):
    """
    检查点不保存换票凭据，恢复时按稳定种子 ID 使用当前令牌重建。
    """
    record = {"site": 7274, "site_name": "猪猪", "title": ITEM["name"], "page_url": ITEM["page_url"]}
    restored = IndexerModule.restore_search_torrent(dict(INDEXER, apikey="rotated-synthetic-key"), record)
    assert restored is not None
    recipe, url = _recipe(restored.enclosure)
    assert recipe["header"]["Authorization"] == "Bearer rotated-synthetic-key"
    assert url.endswith("/56354/download-link")
    assert IndexerModule.restore_search_torrent(dict(INDEXER, apikey=None), record) is None
    boundary.request.post_res.assert_not_called()


@pytest.mark.parametrize("status,payload,expected", [
    (200, None, "结构无效"), (200, {"code": 0, "data": []}, "有效 data"),
    (401, None, "令牌"), (429, None, "限流"),
    (200, {"code": 0, "data": {"code": "notice_unread", "notice_gate": "invalid"}}, "网页端阅读"),
    (200, {"code": "notice_unread", "data": {}}, "网页端阅读"),
    (422, {"code": 422, "data": {"message": "synthetic-key 参数无效"}}, "失败"),
])
def test_envelope_errors_are_safe_and_keep_protocol_semantics(status, payload, expected):
    """
    HTTP、内层业务码和坏结构都能明确失败，错误文本不回显令牌。
    """
    with pytest.raises(ValueError) as error:
        piggo.response_data(status, payload, "synthetic-key")
    assert expected in str(error.value)
    assert "synthetic-key" not in str(error.value)


@pytest.mark.parametrize("case", ["no-token", "no-response", "bad-json", "cooldown"])
def test_connectivity_failures_do_not_open_web_pages(monkeypatch, case):
    """
    缺少令牌、无响应、坏 JSON 和既有冷却都明确失败，不用 Cookie 探测页面。
    """
    chain = SiteChain()
    site = SiteSnapshot(id=7274, name="猪猪", domain="piggo.me", url="https://piggo.me/", cookie="unused-cookie",
                        apikey=None if case == "no-token" else "synthetic-key")
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={"parser": "Piggo"}))))
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(chain, "run_module", lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"},
    ))
    wait = Mock(side_effect=RateLimitExceededException("站点限流") if case == "cooldown" else None)
    monkeypatch.setattr(piggo, "wait_for_site_request", wait)
    response = _response({})
    response._content = b"<html>WAF</html>"
    request = Mock(post_res=Mock(return_value=None if case == "no-response" else response))
    monkeypatch.setattr(piggo, "RequestUtils", Mock(return_value=request))
    state, message = chain.test(site.url)
    assert not state
    assert {"no-token": "长期存取令牌", "no-response": "无法连接", "bad-json": "结构无效", "cooldown": "限流"}[case] in message
    assert request.post_res.call_count == (0 if case in ("no-token", "cooldown") else 1)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_piggo_rate_limit_is_classified_by_the_indexer(boundary, monkeypatch, asynchronous):
    """
    猪猪限流必须进入订阅限流分类，并记录失败而不是把捕获的异常算作搜索成功。
    """
    response = _response({"message": "请在一分钟后重试"}, 429, 429)
    request = boundary.async_request if asynchronous else boundary.request
    request.post_res.side_effect = [_response(CATALOG), response]
    monkeypatch.setattr(piggo, "defer_site_request", Mock())
    health = Mock(async_success=AsyncMock(), async_fail=AsyncMock())
    monkeypatch.setattr(indexer_module, "get_configured_site_health_service", lambda: health)
    monkeypatch.setattr(indexer_module, "SitesHelper", Mock(return_value=Mock(check=Mock(return_value=(False, "")))))
    module = object.__new__(IndexerModule)
    with capture_site_search_observation() as observation:
        result = (await module.async_search_torrents(INDEXER, keyword="title") if asynchronous else
                  module.search_torrents(INDEXER, keyword="title"))
    assert result == []
    assert observation.outcome == "rate_limited" and "限流" in observation.error
    (health.async_fail if asynchronous else health.fail).assert_called_once()
    (health.async_success if asynchronous else health.success).assert_not_called()


def test_category_id_can_belong_to_multiple_sections(boundary):
    """
    分类 ID 在电影和剧集分区复用时仍可加载，按种子的实际分区判断类型。
    """
    catalog = deepcopy(CATALOG)
    catalog["categories"].append({"id": 101, "section_id": 22, "name": "海外"})
    item = dict(ITEM, section_id=22, category_ids=[101], media={})
    boundary.request.post_res.side_effect = [_response(catalog), _response(_list([item]))]
    failed, results = piggo.PiggoSpider(INDEXER).search(None, MediaType.TV)
    assert not failed and results[0]["category"] == MediaType.TV.value
    assert boundary.request.post_res.call_args.kwargs["json"]["section_id"] == 22


def test_multi_section_category_union_deduplicates_shared_ids(boundary):
    """
    多分区的分类并集只传递唯一 ID，分类成员关系不会因复用 ID 而丢失。
    """
    catalog = deepcopy(CATALOG)
    catalog["sections"].append({"id": 44, "name": "儿童电影"})
    catalog["categories"].append({"id": 101, "section_id": 44, "name": "海外"})
    boundary.request.post_res.side_effect = [_response(catalog), _response(_list())]
    assert piggo.PiggoSpider(INDEXER).search(None, MediaType.MOVIE)[0] is False
    assert boundary.request.post_res.call_args.kwargs["json"] == {"page": 1, "per_page": 100, "category_id": [101]}


def test_ambiguous_visible_section_is_not_excluded_by_media_filter(boundary):
    """
    可见目录有未能判型的纪录片分区时，不用单一电影分区过滤掉其中的电影。
    """
    catalog = deepcopy(CATALOG)
    catalog["sections"].append({"id": 66, "name": "纪录片"})
    catalog["categories"].append({"id": 101, "section_id": 66, "name": "冒险"})
    item = dict(ITEM, section_id=66, category_ids=[101], media={"tmdb": "42", "tmdb_kind": "movie"})
    boundary.request.post_res.side_effect = [_response(catalog), _response(_list([item]))]
    failed, result = piggo.PiggoSpider(INDEXER).search("name", MediaType.MOVIE)
    assert not failed and result[0]["category"] == MediaType.MOVIE.value
    assert boundary.request.post_res.call_args.kwargs["json"] == {"page": 1, "per_page": 100, "q": "name"}
