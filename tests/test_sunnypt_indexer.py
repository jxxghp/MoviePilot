import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.core.config import settings
from app.core.context import TorrentInfo
from app.db.message_oper import MessageOper
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.sunnypt import SunnyPTSiteUserInfo
from app.modules.indexer.spider.sunnypt import SunnyPTSpider
from app.schemas import MediaType, NotificationType


class _FakeResponse:
    """构造站点 API 测试使用的最小响应对象。"""

    def __init__(self, payload: dict, status_code: int = 200):
        """保存响应数据和状态码。"""
        self._payload = payload
        self.status_code = status_code
        self.reason = "OK"

    def __bool__(self) -> bool:
        """按 HTTP 成功状态模拟 requests.Response 的布尔值。"""
        return self.status_code < 400

    def json(self) -> dict:
        """返回预设 JSON 数据。"""
        return self._payload


@pytest.fixture(autouse=True)
def clear_sunnypt_category_cache():
    """在用例前后清理 SunnyPT 分类缓存，避免进程级状态互相污染。"""
    SunnyPTSpider._category_cache.clear()
    yield
    SunnyPTSpider._category_cache.clear()


def _build_indexer() -> dict:
    """构造 SunnyPT API Spider 所需的最小站点配置。"""
    return {
        "id": "sunnypt",
        "name": "Sunny",
        "domain": "https://sunnypt.top/",
        "api_url": "https://api.sunnypt.top/api/v1/mp",
        "apikey": "sunny-secret",
        "ua": "MoviePilot-Test",
        "proxy": False,
        "category": {
            "movie": [{"id": 401}, {"id": 404}, {"id": 405}],
            "tv": [{"id": 402}, {"id": 403}, {"id": 404}, {"id": 405}],
        },
    }


def _category_response() -> _FakeResponse:
    """构造 SunnyPT 分类接口响应。"""
    return _FakeResponse({
        "code": 0,
        "msg": "ok",
        "data": [
            {"id": 401, "name": "电影", "media_types": ["movie"]},
            {"id": 402, "name": "电视剧", "media_types": ["tv"]},
            {"id": 404, "name": "纪录片", "media_types": ["movie", "tv"]},
        ],
    })


def _torrent_response() -> _FakeResponse:
    """构造 SunnyPT 种子搜索接口响应。"""
    return _FakeResponse({
        "code": 0,
        "msg": "ok",
        "data": {
            "items": [{
                "id": 123,
                "title": "Movie.2026.2160p.WEB-DL.H.265-GROUP",
                "subtitle": "电影中文副标题",
                "media_type": "movie",
                "size": 21474836480,
                "created_at": "2026-07-21T14:00:00+08:00",
                "seeders": 15,
                "leechers": 2,
                "completed": 30,
                "imdb_id": "tt1234567",
                "tags": ["中字", "HDR"],
                "hit_and_run": True,
                "promotion": {
                    "is_active": True,
                    "up_multiplier": 2.0,
                    "down_multiplier": 0.0,
                    "until": "2026-07-23T14:00:00+08:00",
                },
                "details_url": "https://sunnypt.top/torrent/123",
            }],
        },
    })


def test_sunnypt_search_maps_api_fields_and_caches_categories(monkeypatch):
    """SunnyPT 搜索应映射标准字段、编码下载请求并复用分类缓存。"""
    calls = []

    def fake_get_res(request, url: str, params: dict = None, **_kwargs):
        """按请求路径回放分类和种子响应。"""
        calls.append((url, params, request._headers))
        if url.endswith("/categories"):
            return _category_response()
        return _torrent_response()

    monkeypatch.setattr(
        "app.modules.indexer.spider.sunnypt.RequestUtils.get_res",
        fake_get_res,
    )
    spider = SunnyPTSpider(_build_indexer())

    error, torrents = spider.search(
        keyword="tt1234567",
        mtype=MediaType.MOVIE,
        page=0,
    )
    second_error, _ = SunnyPTSpider(_build_indexer()).search(
        keyword="Movie",
        mtype=MediaType.MOVIE,
        page=1,
    )

    assert not error
    assert not second_error
    assert [call[0] for call in calls].count(
        "https://api.sunnypt.top/api/v1/mp/categories"
    ) == 1
    search_url, search_params, search_headers = calls[1]
    assert search_url == "https://api.sunnypt.top/api/v1/mp/torrents"
    assert search_params == {
        "page": 1,
        "page_size": 100,
        "sort": "created_at",
        "order": "desc",
        "keyword": "tt1234567",
        "media_type": "movie",
        "categories": "401,404",
    }
    assert search_headers["X-API-Key"] == "sunny-secret"
    assert torrents == [{
        "title": "Movie.2026.2160p.WEB-DL.H.265-GROUP",
        "description": "电影中文副标题",
        "enclosure": torrents[0]["enclosure"],
        "pubdate": "2026-07-21 14:00:00",
        "size": 21474836480,
        "seeders": 15,
        "peers": 2,
        "grabs": 30,
        "downloadvolumefactor": 0.0,
        "uploadvolumefactor": 2.0,
        "freedate": "2026-07-23 14:00:00",
        "page_url": "https://sunnypt.top/torrent/123",
        "imdbid": "tt1234567",
        "labels": ["中字", "HDR"],
        "hit_and_run": True,
        "category": MediaType.MOVIE.value,
    }]

    encoded_config, token_url = torrents[0]["enclosure"].split("]", 1)
    request_config = json.loads(base64.b64decode(encoded_config[1:]).decode("utf-8"))
    assert token_url == "https://api.sunnypt.top/api/v1/mp/torrents/123/download-token"
    assert request_config == {
        "method": "post",
        "cookie": False,
        "header": {
            "X-API-Key": "sunny-secret",
            "Accept": "application/json",
        },
        "proxy": False,
        "result": "data.download_url",
        "result_base_url": "https://api.sunnypt.top/api/v1/mp",
    }


def test_sunnypt_async_search_uses_api_contract(monkeypatch):
    """SunnyPT 异步搜索应使用同一套 GET 参数和响应映射。"""
    calls = []

    async def fake_get_res(request, url: str, params: dict = None, **_kwargs):
        """异步回放分类和种子响应。"""
        calls.append((url, params, request._headers))
        if url.endswith("/categories"):
            return _category_response()
        return _torrent_response()

    monkeypatch.setattr(
        "app.modules.indexer.spider.sunnypt.AsyncRequestUtils.get_res",
        fake_get_res,
    )

    error, torrents = asyncio.run(
        SunnyPTSpider(_build_indexer()).async_search(
            keyword="Movie",
            mtype=MediaType.TV,
            cat="402,404",
            page=2,
        )
    )

    assert not error
    assert len(torrents) == 1
    assert len(calls) == 1
    assert calls[0][1]["page"] == 3
    assert calls[0][1]["media_type"] == "tv"
    assert calls[0][1]["categories"] == "402,404"
    assert calls[0][2]["X-API-Key"] == "sunny-secret"


def test_sunnypt_user_parser_reads_profile_and_messages_without_marking_read(monkeypatch):
    """SunnyPT 用户解析器应读取统计和未读消息，但不能在解析阶段标记已读。"""
    requested_urls = []
    profile = {
        "code": 0,
        "msg": "ok",
        "data": {
            "id": 1001,
            "username": "sunny",
            "level": "Power User",
            "registered_at": "2025-01-01T12:00:00+08:00",
            "uploaded": 1099511627776,
            "downloaded": 536870912000,
            "ratio": 2.048,
            "bonus": 12345.6,
            "seeding_count": 30,
            "seeding_size": 2147483648000,
            "leeching_count": 1,
            "leeching_size": 10737418240,
            "unread_messages": 1,
        },
    }
    messages = {
        "code": 0,
        "msg": "ok",
        "data": {
            "items": [{
                "id": 9001,
                "title": "种子审核通过",
                "content": "你发布的种子已审核通过。",
                "created_at": "2026-07-21T16:30:00+08:00",
                "unread": True,
            }],
            "unread_count": 1,
            "has_more": False,
        },
    }

    def fake_get_page_content(_self, url: str, **_kwargs):
        """按请求地址回放用户信息和消息列表。"""
        requested_urls.append(url)
        return json.dumps(messages if "/messages" in url else profile)

    monkeypatch.setattr(SunnyPTSiteUserInfo, "_get_page_content", fake_get_page_content)
    monkeypatch.setattr(settings, "SITE_MESSAGE", True)
    parser = SunnyPTSiteUserInfo(
        site_name="Sunny",
        url="https://sunnypt.top/",
        site_cookie="",
        apikey="sunny-secret",
        token=None,
        api_url="https://api.sunnypt.top/api/v1/mp",
    )

    parser.parse()

    assert parser.userid == 1001
    assert parser.username == "sunny"
    assert parser.user_level == "Power User"
    assert parser.join_at == "2025-01-01 12:00:00"
    assert parser.upload == 1099511627776
    assert parser.download == 536870912000
    assert parser.ratio == 2.048
    assert parser.bonus == 12345.6
    assert parser.seeding == 30
    assert parser.seeding_size == 2147483648000
    assert parser.leeching == 1
    assert parser.leeching_size == 10737418240
    assert parser.message_unread == 1
    assert parser.message_unread_contents == [
        (
            "种子审核通过",
            "2026-07-21 16:30:00",
            "你发布的种子已审核通过。",
            "sunnypt-message:9001",
        )
    ]
    assert not any(url.endswith("/read") or url.endswith("/read-all") for url in requested_urls)


def test_site_messages_are_deduplicated_by_persisted_source(monkeypatch):
    """站点消息应使用解析器保留的消息 ID 来源标识做持久化去重。"""
    duplicate_source = "sunnypt-message:9001-dedup-test"
    MessageOper().add(
        source=duplicate_source,
        mtype=NotificationType.SiteMessage,
        title="existing",
        text="existing",
    )
    sent_messages = []
    chain = object.__new__(SiteChain)
    chain.messageoper = MessageOper()
    monkeypatch.setattr(chain, "post_message", sent_messages.append)
    userdata = SimpleNamespace(
        message_unread=2,
        message_unread_contents=[
            ("重复消息", "2026-07-21 16:30:00", "旧内容", duplicate_source),
            ("新消息", "2026-07-22 16:30:00", "新内容", "sunnypt-message:9002-dedup-test"),
        ],
    )

    chain._post_site_messages(
        site={"name": "Sunny", "url": "https://sunnypt.top/"},
        userdata=userdata,
    )

    assert len(sent_messages) == 1
    assert sent_messages[0].source == "sunnypt-message:9002-dedup-test"
    assert sent_messages[0].title == "【站点 Sunny 消息】"


def test_indexer_module_dispatches_sunnypt_search(monkeypatch):
    """IndexerModule 应把 SunnyPT 同步搜索参数交给专用 API Spider。"""
    captured = {}

    def fake_search(_self, keyword, mtype, cat, page):
        """记录 IndexerModule 传给 SunnyPT Spider 的搜索参数。"""
        captured.update({
            "keyword": keyword,
            "mtype": mtype,
            "cat": cat,
            "page": page,
        })
        return False, [{"title": "Movie.2026", "enclosure": "https://example.com/1.torrent"}]

    monkeypatch.setattr(SunnyPTSpider, "search", fake_search)
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(lambda _site, _keyword=None: True),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__indexer_statistic",
        staticmethod(lambda **_kwargs: None),
    )
    site = {
        **_build_indexer(),
        "parser": "SunnyPT",
        "pri": 1,
    }

    torrents = object.__new__(IndexerModule).search_torrents(
        site=site,
        keyword="Movie",
        mtype=MediaType.MOVIE,
        cat="401",
        page=2,
    )

    assert captured == {
        "keyword": "Movie",
        "mtype": MediaType.MOVIE,
        "cat": "401",
        "page": 2,
    }
    assert len(torrents) == 1
    assert torrents[0].title == "Movie.2026"


def test_sunnypt_site_test_uses_profile_api(monkeypatch):
    """SunnyPT 连接测试应使用 Build 配置的 profile API 和 X-API-Key。"""
    captured = {}

    def fake_get_indexer(domain: str):
        """返回包含独立 API 地址的 SunnyPT 索引配置。"""
        assert domain == "sunnypt.top"
        return {"api_url": "https://api.sunnypt.top/api/v1/mp"}

    def fake_sites_helper():
        """构造不触发动态资源保护元类的站点帮助器替身。"""
        return SimpleNamespace(get_indexer=fake_get_indexer)

    def fake_get_res(request, url: str, **_kwargs):
        """记录站点连接测试请求并返回有效用户资料。"""
        captured.update({"url": url, "headers": request._headers})
        return _FakeResponse({
            "code": 0,
            "msg": "ok",
            "data": {"download_allowed": True},
        })

    monkeypatch.setattr("app.chain.site.SitesHelper", fake_sites_helper)
    monkeypatch.setattr("app.chain.site.RequestUtils.get_res", fake_get_res)
    site = SimpleNamespace(
        domain="sunnypt.top",
        ua="MoviePilot-Test",
        apikey="sunny-secret",
        proxy=0,
        timeout=15,
    )

    state, message = SiteChain._SiteChain__sunnypt_test(site)

    assert state
    assert message == "连接成功"
    assert captured["url"] == "https://api.sunnypt.top/api/v1/mp/profile"
    assert captured["headers"]["X-API-Key"] == "sunny-secret"
    assert "Authorization" not in captured["headers"]


def test_indirect_download_does_not_log_or_cache_temporary_url(monkeypatch):
    """两段式下载不得记录或缓存包含短时凭证的真实下载地址。"""
    captured = {}
    log_messages = []

    def fake_post_res(_request, url: str, params: dict = None, **_kwargs):
        """回放 download-token 接口返回的短时下载地址。"""
        captured["token_url"] = url
        captured["token_params"] = params
        return _FakeResponse({
            "code": 0,
            "msg": "ok",
            "data": {
                "download_url": "https://sunnypt.top/api/v1/mp/download/temporary-credential",
            },
        })

    def fake_download_torrent(_helper, **kwargs):
        """记录 TorrentHelper 的失败缓存开关并返回有效种子内容。"""
        captured.update(kwargs)
        return None, b"torrent-content", "Movie", ["Movie.mkv"], ""

    def capture_log(message: str):
        """收集下载链日志以校验敏感地址不会泄露。"""
        log_messages.append(message)

    monkeypatch.setattr("app.chain.download.RequestUtils.post_res", fake_post_res)
    monkeypatch.setattr(
        "app.chain.download.TorrentHelper.download_torrent",
        fake_download_torrent,
    )
    monkeypatch.setattr(
        "app.chain.download.logger",
        SimpleNamespace(info=capture_log, error=capture_log),
    )
    enclosure = SunnyPTSpider(_build_indexer())._build_download_url(123)
    torrent = TorrentInfo(
        title="Movie.2026",
        enclosure=enclosure,
        site_ua="MoviePilot-Test",
        site_proxy=False,
    )

    content, folder, files = object.__new__(DownloadChain).download_torrent(torrent)

    assert content == b"torrent-content"
    assert folder == "Movie"
    assert files == ["Movie.mkv"]
    assert captured["cache_invalid"] is False
    assert captured["cookie"] is None
    assert captured["url"] == (
        "https://api.sunnypt.top/api/v1/mp/download/temporary-credential"
    )
    assert not any("sunny-secret" in message for message in log_messages)
    assert not any("temporary-credential" in message for message in log_messages)
