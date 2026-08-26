# -*- coding: utf-8 -*-
# pylint: disable=no-name-in-module

import base64
import json
from types import SimpleNamespace

import pytest

from app.chain.site import SiteChain
from app.modules.indexer.parser.rousi import RousiSiteUserInfo
from app.modules.indexer.spider import rousi as rousi_module
from app.modules.indexer.spider.rousi import RousiSpider
from app.schemas import MediaType


class _FakeResponse:
    """构造 PeerGo 兼容 API 测试使用的最小响应对象。"""

    def __init__(self, payload: dict, status_code: int = 200):
        """保存响应数据和状态码。"""
        self._payload = payload
        self.status_code = status_code
        self.reason = "OK"

    def json(self) -> dict:
        """返回预设 JSON 数据。"""
        return self._payload


def _build_indexer(apikey: str = "rousi-secret", proxy: bool = False) -> dict:
    """构造 Rousi Pro API Spider 所需的最小站点配置。"""
    return {
        "id": "rousipro",
        "name": "Rousi Pro",
        "domain": "https://rousi.pro/",
        "apikey": apikey,
        "ua": "MoviePilot-Test",
        "proxy": proxy,
    }


def _build_user_parser() -> RousiSiteUserInfo:
    """构造无需真实网络请求的 Rousi 用户数据解析器。"""
    return RousiSiteUserInfo(
        site_name="Rousi Pro",
        url="https://rousi.pro/",
        site_cookie="",
        apikey="rousi-secret",
        token=None,
        ua="MoviePilot-Test",
    )


@pytest.fixture()
def rousi_spider(monkeypatch):
    """构造不依赖真实数据库配置的 RousiSpider。"""
    monkeypatch.setattr(rousi_module, "get_configured_system_config", lambda: None)
    return RousiSpider(_build_indexer())


def test_music_search_uses_music_category(rousi_spider):
    """音乐搜索应提交 Rousi Pro 的 music 分类参数。"""
    params = rousi_spider._RousiSpider__get_params("张学友", MediaType.MUSIC, None, 0)

    assert params["category"] == "music"


def test_user_selected_music_category_maps_to_api_name(rousi_spider):
    """用户选择音乐分类 ID 时应映射为 API 的 music 分类名。"""
    params = rousi_spider._RousiSpider__get_params("张学友", None, "5", 0)

    assert params["category"] == "music"


def test_parse_result_marks_music_torrents(rousi_spider):
    """music 分类种子应标记为音乐媒体类型。"""
    torrents = rousi_spider._RousiSpider__parse_result([
        {"id": 1, "uuid": "u1", "title": "张学友 - 他在那里 FLAC", "category": "music"},
        {"id": 2, "uuid": "u2", "title": "流浪地球", "category": "movie"},
        {"id": 3, "uuid": "u3", "title": "剧集 S01", "category": {"slug": "tv"}},
    ])

    assert [torrent["category"] for torrent in torrents] == [
        MediaType.MUSIC.value,
        MediaType.MOVIE.value,
        MediaType.TV.value,
    ]


def test_search_uses_peergo_personal_api_key_contract(monkeypatch):
    """搜索应使用个人 API Key 兼容响应并生成短时下载地址换取请求。"""
    captured = {}
    payload = {
        "code": 0,
        "message": "success",
        "data": {
            "page": 1,
            "page_size": 100,
            "total": 1,
            "total_pages": 1,
            "torrents": [{
                "category": "movie",
                "category_name": "电影",
                "created_at": "2026-06-09T12:04:15.184279Z",
                "downloads": 21,
                "id": 8461,
                "leechers": 6,
                "promotion": {
                    "down_multiplier": 0,
                    "is_active": True,
                    "until": "2026-09-21T16:36:31.008958Z",
                    "up_multiplier": 2,
                },
                "seeders": 31,
                "size": 190438784128,
                "subtitle": "电影中文副标题",
                "title": "Movie.2026.2160p.UHD.BluRay",
                "uuid": "8461",
            }],
        },
    }

    def fake_get_res(request, url: str, params: dict = None, **_kwargs):
        """记录搜索请求并回放 PeerGo MoviePilot 兼容响应。"""
        captured.update({"url": url, "params": params, "headers": request._headers})
        return _FakeResponse(payload)

    monkeypatch.setattr(rousi_module, "get_configured_system_config", lambda: None)
    monkeypatch.setattr(rousi_module, "get_runtime_setting", lambda _key: {"https": "proxy"})
    monkeypatch.setattr(rousi_module.RequestUtils, "get_res", fake_get_res)

    error, torrents = RousiSpider(_build_indexer(proxy=True)).search(
        keyword="Movie",
        mtype=MediaType.MOVIE,
        page=0,
    )

    assert not error
    assert captured == {
        "url": "https://rousi.pro/api/v1/torrents",
        "params": {
            "page": 1,
            "page_size": 100,
            "keyword": "Movie",
            "category": "movie",
        },
        "headers": {
            "Authorization": "Bearer rousi-secret",
            "Accept": "application/json",
        },
    }
    assert len(torrents) == 1
    assert torrents[0]["title"] == "Movie.2026.2160p.UHD.BluRay"
    assert torrents[0]["page_url"] == "https://rousi.pro/torrents/8461"
    assert torrents[0]["downloadvolumefactor"] == 0
    assert torrents[0]["uploadvolumefactor"] == 2
    assert torrents[0]["category"] == MediaType.MOVIE.value

    encoded_config, detail_url = torrents[0]["enclosure"].split("]", 1)
    request_config = json.loads(base64.b64decode(encoded_config[1:]).decode("utf-8"))
    assert detail_url == "https://rousi.pro/api/v1/torrents/8461"
    assert request_config == {
        "method": "get",
        "cookie": False,
        "header": {
            "Authorization": "Bearer rousi-secret",
            "Accept": "application/json",
        },
        "proxy": True,
        "result": "data.download_url",
    }


def test_search_rejects_missing_personal_api_key(monkeypatch):
    """未配置个人 API Key 时不得向 PeerGo 发起搜索请求。"""
    monkeypatch.setattr(rousi_module, "get_configured_system_config", lambda: None)
    spider = RousiSpider(_build_indexer(apikey=""))

    error, torrents = spider.search(keyword="Movie")

    assert error
    assert torrents == []


def test_user_parser_reads_peergo_profile_with_personal_api_key(monkeypatch):
    """用户数据解析应通过个人 API Key 读取 PeerGo 兼容资料结构。"""
    captured = {}
    profile = {
        "code": 0,
        "message": "success",
        "data": {
            "id": 619,
            "username": "jxxghp",
            "level_text": "Lv.1",
            "registered_at": "2026-01-05T19:50:59.012Z",
            "uploaded": 1099511627776,
            "downloaded": 536870912000,
            "ratio": 2.048,
            "karma": 1079960,
            "seeding_leeching_data": {
                "seeding_count": 8,
                "seeding_size": 2147483648000,
                "leeching_count": 1,
                "leeching_size": 10737418240,
            },
        },
    }

    def fake_get_page_content(_self, url: str, headers: dict = None, **_kwargs):
        """记录用户资料请求并回放 PeerGo 兼容响应。"""
        captured.update({"url": url, "headers": headers})
        return json.dumps(profile)

    monkeypatch.setattr(RousiSiteUserInfo, "_get_page_content", fake_get_page_content)
    parser = RousiSiteUserInfo(
        site_name="Rousi Pro",
        url="https://rousi.pro/",
        site_cookie="",
        apikey="rousi-secret",
        token=None,
        ua="MoviePilot-Test",
    )

    parser.parse()

    assert captured["url"] == "https://rousi.pro/api/v1/profile"
    assert captured["headers"]["Authorization"] == "Bearer rousi-secret"
    assert parser.userid == 619
    assert parser.username == "jxxghp"
    assert parser.user_level == "Lv.1"
    assert parser.join_at == "2026-01-05 19:50:59"
    assert parser.upload == 1099511627776
    assert parser.download == 536870912000
    assert parser.ratio == 2.05
    assert parser.bonus == 1079960
    assert parser.seeding == 8
    assert parser.seeding_size == 2147483648000
    assert parser.leeching == 1
    assert parser.leeching_size == 10737418240


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ([], "用户数据响应结构无效"),
        ({"code": 1, "message": 619}, "619"),
        ({"code": 1, "message": None}, "未知错误"),
    ],
)
def test_user_parser_normalizes_profile_errors(
    payload: object,
    expected_error: str,
) -> None:
    """无效资料响应必须给调用方稳定的字符串错误消息。"""
    parser = _build_user_parser()

    parser._parse_user_base_info(json.dumps(payload))

    assert parser.err_msg == expected_error


@pytest.mark.parametrize("registered_at", [None, 619, {"unexpected": "value"}])
def test_user_parser_ignores_non_string_registration_time(
    registered_at: object,
) -> None:
    """非字符串注册时间不得被强转为看似有效的日期输入。"""
    parser = _build_user_parser()
    payload = {
        "code": 0,
        "data": {
            "registered_at": registered_at,
        },
    }

    parser._parse_user_base_info(json.dumps(payload))

    assert parser.join_at is None


def test_site_connectivity_uses_peergo_personal_api_key(monkeypatch):
    """Rousi 连接测试应以 Bearer 个人 API Key 请求兼容资料接口。"""
    captured = {}

    def fake_get_res(request, url: str, **_kwargs):
        """记录连接测试请求并返回有效用户资料。"""
        captured.update({"url": url, "headers": request._headers})
        return _FakeResponse({"code": 0, "message": "success", "data": {"id": 619}})

    monkeypatch.setattr("app.chain.site.RequestUtils.get_res", fake_get_res)
    site = SimpleNamespace(
        url="https://rousi.pro/",
        apikey="rousi-secret",
        proxy=0,
        timeout=15,
    )
    chain = object.__new__(SiteChain)
    chain.runtime_config = SimpleNamespace(proxy=None)

    state, message = chain._SiteChain__rousi_test(site)

    assert state
    assert message == "连接成功"
    assert captured["url"] == "https://rousi.pro/api/v1/profile"
    assert captured["headers"]["Authorization"] == "Bearer rousi-secret"


def test_site_connectivity_rejects_missing_personal_api_key():
    """Rousi 连接测试应在请求前报告个人 API Key 缺失。"""
    site = SimpleNamespace(url="https://rousi.pro/", apikey="", proxy=0, timeout=15)
    chain = object.__new__(SiteChain)

    state, message = chain._SiteChain__rousi_test(site)

    assert not state
    assert message == "未配置个人 API Key"
