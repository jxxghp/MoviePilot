"""
验证可乐会员数据接口的字段、失败隔离和禁止页面回退合同。
"""

import datetime
import json
from unittest.mock import Mock

import pytest
from requests import Response

import app.modules.indexer.parser.colapt as colapt
from app.chain.site import SiteChain
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.modules.indexer.spider.colapt import KEY_REQUIRED

# 账号和统计均为合成值，不从真实用户资料生成测试夹具。
STATS = {
    "user": {"id": 42, "username": "synthetic-user", "avatar_url": None}, "joined_at": "2020-01-02T03:04:05Z",
    "raw_uploaded_bytes": "6442450944", "raw_downloaded_bytes": "2147483648",
    "billed_uploaded_bytes": "5368709120", "billed_downloaded_bytes": "1073741824",
    "completions": 9, "seeding": 2, "leeching": 1, "seeding_bytes": "2147483648",
    "totals_updated_at": "2026-11-22T00:00:00Z", "projection_updated_at": None,
    "ratio": "5.0000", "points_balance": "12.5000", "class_name": "工作组-组长", "unread_messages": 3,
}


def _response(data, status=200):
    """
    回放会员数据接口并独立验证响应关闭。
    """
    response = Response()
    response.status_code = status
    response._content = json.dumps(data).encode()
    response.encoding = "utf-8"
    response.close = Mock()
    return response


@pytest.fixture
def boundary(monkeypatch):
    """
    替换客户端与等待，保留完整资料校验及只读统计投影。
    """
    monkeypatch.setattr(colapt, "site_request_config", lambda _url: {"id": 9101, "domain": "colapt.com"})
    monkeypatch.setattr(colapt, "wait_for_site_request", Mock())
    monkeypatch.setattr(colapt, "get_runtime_setting", lambda _key: {"https": "proxy"})
    request = Mock(get_res=Mock(return_value=_response(STATS)))
    parser = colapt.ColaPTSiteUserInfo(
        site_name="可乐", url="https://www.colapt.com/", site_cookie="unused-cookie", apikey=" synthetic-key ",
        token="unused-token", request_utils=request, ua="ColaPT-Test", proxy=True, emulate=True,
    )
    return parser, request


def test_stats_use_one_key_only_get_and_map_billed_fields(boundary):
    """
    等级、计费流量、瓶盖、做种体积及未读数量只取会员数据接口，不访问网页或站内信正文。
    """
    parser, request = boundary
    parser.parse()
    assert parser.err_msg is None
    assert (parser.userid, parser.username, parser.user_level) == (42, "synthetic-user", "工作组-组长")
    assert (parser.upload, parser.download, parser.ratio, parser.bonus) == (5368709120, 1073741824, 5, 12.5)
    assert (parser.seeding, parser.seeding_size, parser.leeching) == (2, 2147483648, 1)
    assert (parser.completed, parser.message_unread) == (9, 3)
    expected_join = datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc).astimezone()
    assert parser.join_at == expected_join.strftime("%Y-%m-%d %H:%M:%S")
    assert parser.seeding_info == [] and parser.message_unread_contents == []
    request.get_res.assert_called_once_with(
        url="https://www.colapt.com/api/v1/me/stats", cookies=None,
        headers={"X-API-Key": "synthetic-key", "Accept": "application/json", "User-Agent": "ColaPT-Test"},
        proxies={"https": "proxy"}, timeout=60, allow_redirects=False,
    )
    request.close.assert_called_once()
    request.get_res.return_value.close.assert_called_once()
    assert parser._emulate is False and parser._site_cookie is None


def test_stats_honor_configured_api_and_disable_legacy_pages(boundary):
    """
    自定义 API 地址只影响资料来源，不复用网页或其他页面入口。
    """
    parser, request = boundary
    parser._api_url = "https://api.custom.test/v1/"
    parser.parse()
    assert request.get_res.call_args.kwargs["url"] == "https://api.custom.test/v1/me/stats"
    assert parser._user_detail_page is parser._user_traffic_page is parser._torrent_seeding_page is None


@pytest.mark.parametrize("ratio,points,class_name,expected", [
    (None, "0.0000", None, (None, 0, None)),
    ("0.1234", "-3.5000", "新人", (0.1234, -3.5, "新人")),
])
def test_infinite_ratio_negative_points_and_unranked_class(boundary, ratio, points, class_name, expected):
    """
    无计费下载时分享率保持为空而非 0，瓶盖允许负数，未定级等级为空。
    """
    parser, request = boundary
    request.get_res.return_value = _response(dict(STATS, ratio=ratio, points_balance=points, class_name=class_name))
    parser.parse()
    assert parser.userid == 42 and (parser.ratio, parser.bonus, parser.user_level) == expected


def test_missing_seeding_bytes_keeps_zero(boundary):
    """
    做种体积缺失时记为零，不影响其他统计。
    """
    parser, request = boundary
    request.get_res.return_value = _response({key: value for key, value in STATS.items() if key != "seeding_bytes"})
    parser.parse()
    assert parser.userid == 42 and parser.seeding_size == 0


@pytest.mark.parametrize("field,value", [
    ("user", None), ("user", {"id": 0, "username": "u"}), ("user", {"id": True, "username": "u"}),
    ("user", {"id": 42, "username": " "}), ("user", {"id": 42, "username": 42}), ("joined_at", None),
    ("billed_uploaded_bytes", None), ("billed_downloaded_bytes", "-1"), ("billed_uploaded_bytes", 1.5),
    ("ratio", "inf"), ("ratio", 5), ("points_balance", "nan"), ("class_name", ["invalid"]),
    ("seeding", 1.5), ("seeding_bytes", "-1"), ("unread_messages", True), ("joined_at", "yesterday"),
])
def test_invalid_stats_do_not_publish_partial_identity(boundary, field, value):
    """
    身份、流量或统计无效时不公开用户 ID，也不留下已投影的半份数据。
    """
    parser, request = boundary
    data = dict(STATS)
    data[field] = value
    request.get_res.return_value = _response(data)
    parser.parse()
    assert parser.userid is None and parser.err_msg
    assert parser.upload == parser.download == parser.seeding == parser.seeding_size == 0
    request.get_res.assert_called_once()
    request.get_res.return_value.close.assert_called_once()


@pytest.mark.parametrize("status,code,expected", [(401, "invalid_credentials", "已失效"),
                                                 (403, "api_key_scope_denied", "读取"),
                                                 (429, "rate_limited", "限流"),
                                                 (503, "service_unavailable", "HTTP 503")])
def test_stats_http_failures_are_explicit(boundary, monkeypatch, status, code, expected):
    """
    HTTP 与错误码失败不持久化统计；限流保存为同站点共享冷却。
    """
    parser, request = boundary
    response = _response({"error": {"code": code, "message": "synthetic-key", "request_id": "r", "fields": {}}},
                         status)
    request.get_res.return_value = response
    defer = Mock()
    monkeypatch.setattr(colapt, "defer_site_request", defer)
    parser.parse()
    assert parser.userid is None and expected in parser.err_msg and "synthetic-key" not in parser.err_msg
    assert defer.called is (status == 429)
    response.close.assert_called_once()
    request.close.assert_called_once()


@pytest.mark.parametrize("status", [200, 502])
def test_non_json_or_missing_stats_response_is_rejected(boundary, status):
    """
    非 JSON 和网关错误保留接口失败，不泄露原始响应或异常正文。
    """
    parser, request = boundary
    response = _response({}, status)
    response._content = b"<html>synthetic-key</html>"
    request.get_res.return_value = response
    parser.parse()
    assert parser.userid is None and "synthetic-key" not in parser.err_msg
    response.close.assert_called_once()
    request.get_res.return_value = None
    parser.parse()
    assert parser.userid is None and parser.err_msg == "无法连接 ColaPT API"


def test_missing_key_disables_fallback_and_persistence(boundary, monkeypatch):
    """
    资料失败不触发 NexusPHP 探测，刷新链不保存无身份的数据。
    """
    parser, request = boundary
    parser.apikey = None
    constructor = Mock(return_value=parser)
    constructor.schema = colapt.SiteSchema.ColaPT
    module = object.__new__(IndexerModule)
    module._site_schemas = [constructor, NexusPhpSiteUserInfo]
    fallback = Mock(side_effect=AssertionError("不应回退 Cookie 页面"))
    monkeypatch.setattr(NexusPhpSiteUserInfo, "parse", fallback)
    site = {"name": "可乐", "url": "https://www.colapt.com/", "schema": "ColaPT", "public": False}
    userdata = module.refresh_userdata(site)
    assert userdata.userid is None and userdata.err_msg == KEY_REQUIRED
    fallback.assert_not_called()
    request.get_res.assert_not_called()
    chain = object.__new__(SiteChain)
    chain.run_module = Mock(return_value=userdata)
    chain.site_repository = Mock()
    assert chain.refresh_userdata(site) is userdata
    chain.site_repository.update_userdata.assert_not_called()


def test_required_page_hooks_do_not_access_unsupported_pages(boundary):
    """
    基类要求的页面入口保持无操作，不能增加消息或做种页面请求。
    """
    parser, request = boundary
    parser._parse_user_traffic_info("")
    parser._parse_user_detail_info("")
    assert parser._parse_user_torrent_seeding_info("") is None
    assert parser._parse_message_unread_links("", []) is None
    assert parser._parse_message_content("") == (None, None, None)
    request.get_res.assert_not_called()
