"""
验证高清视界用户汇总 API 的字段、失败隔离和禁止页面回退合同。
"""

import json
from unittest.mock import Mock

import pytest
from requests import Response

import app.modules.indexer.parser.hdarea as hdarea
from app.chain.site import SiteChain
from app.domain import site as site_rules
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo

# 账号和统计均为合成值，不从真实用户资料生成测试夹具。
PROFILE = {
    "id": 42, "username": "synthetic-user", "class": 4, "class_name": "Power User", "enabled": True,
    "added": "2020-01-02 03:04:05", "uploaded": 5368709120, "downloaded": 1073741824,
    "share_ratio": 5.0, "seedbonus": 12.5, "seeding_count": 2, "seeding_size": 2147483648,
    "leeching_count": 1, "uploaded_torrents_count": 7, "completed_torrents_count": 9,
    "unread_messages_count": 3,
}


def _response(data, status=200, code=0, success=True):
    """
    回放用户 API 的标准信封并独立验证响应关闭。
    """
    response = Response()
    response.status_code = status
    response._content = json.dumps({"success": success, "code": code, "message": "OK", "data": data}).encode()
    response.encoding = "utf-8"
    response.close = Mock()
    return response


@pytest.fixture
def boundary(monkeypatch):
    """
    替换客户端与等待，保留完整资料校验及只读统计投影。
    """
    monkeypatch.setattr(hdarea, "site_request_config", lambda _url: {"id": 7370, "domain": "hdarea.club"})
    monkeypatch.setattr(hdarea, "wait_for_site_request", Mock())
    monkeypatch.setattr(hdarea, "get_runtime_setting", lambda _key: {"https": "proxy"})
    request = Mock(get_res=Mock(return_value=_response(PROFILE)))
    parser = hdarea.HDAreaSiteUserInfo(
        site_name="高清视界", url="https://hdarea.club/", site_cookie="unused-cookie", apikey=" synthetic-key ",
        token="unused-token", request_utils=request, ua="HDArea-Test", proxy=True, emulate=True,
    )
    return parser, request


def test_profile_uses_one_key_only_get_and_maps_aggregate_fields(boundary):
    """
    用户等级、流量、魔力、做种体积及未读数量只取汇总 API，不访问 PHP 或私信正文。
    """
    parser, request = boundary
    parser.parse()
    assert parser.err_msg is None
    assert (parser.userid, parser.username, parser.user_level) == (42, "synthetic-user", "Power User")
    assert (parser.upload, parser.download, parser.ratio, parser.bonus) == (5368709120, 1073741824, 5, 12.5)
    assert (parser.seeding, parser.seeding_size, parser.leeching) == (2, 2147483648, 1)
    assert (parser.uploaded, parser.completed, parser.message_unread) == (7, 9, 3)
    assert parser.join_at == "2020-01-02 03:04:05"
    assert parser.seeding_info == [] and parser.message_unread_contents == []
    request.get_res.assert_called_once_with(
        url="https://hdarea.club/api/v1/user/profile", cookies=None,
        headers={"X-API-Key": "synthetic-key", "Accept": "application/json", "User-Agent": "HDArea-Test"},
        proxies={"https": "proxy"}, timeout=60, allow_redirects=False,
    )
    request.close.assert_called_once()
    request.get_res.return_value.close.assert_called_once()
    assert parser._emulate is False and parser._site_cookie is None


def test_profile_honors_configured_api_and_disables_legacy_pages(boundary):
    """
    自定义 API 地址只影响资料来源，不复用默认网页或其他页面入口。
    """
    parser, request = boundary
    parser._api_url = "https://api.custom.test/v1/"
    parser.parse()
    assert request.get_res.call_args.kwargs["url"] == "https://api.custom.test/v1/user/profile"
    assert parser._user_detail_page is parser._user_traffic_page is parser._torrent_seeding_page is None


@pytest.mark.parametrize("field,value", [
    ("id", 0), ("id", True), ("username", " "), ("username", 42), ("enabled", False),
    ("uploaded", None), ("downloaded", -1), ("seedbonus", "nan"), ("share_ratio", "inf"),
    ("class_name", ["invalid"]),
    ("seeding_count", 1.5), ("seeding_size", -1), ("unread_messages_count", True),
])
def test_invalid_profile_does_not_publish_partial_identity(boundary, field, value):
    """
    身份、流量或统计无效时不公开用户 ID，也不留下已投影的半份数据。
    """
    parser, request = boundary
    data = dict(PROFILE)
    data[field] = value
    request.get_res.return_value = _response(data)
    parser.parse()
    assert parser.userid is None and parser.err_msg
    assert parser.upload == parser.download == parser.seeding == parser.seeding_size == 0
    request.get_res.assert_called_once()
    request.get_res.return_value.close.assert_called_once()


@pytest.mark.parametrize("status,code,success", [(401, 401, False), (403, 403, False), (429, 429, False),
                                                (500, 0, True), (200, 0, False), (200, 3, True)])
def test_profile_http_and_business_failures_are_explicit(boundary, monkeypatch, status, code, success):
    """
    HTTP 与业务失败不持久化统计；限流保存为同站点共享冷却。
    """
    parser, request = boundary
    response = _response(PROFILE, status, code, success)
    request.get_res.return_value = response
    defer = Mock()
    monkeypatch.setattr(hdarea, "defer_site_request", defer)
    parser.parse()
    assert parser.userid is None and parser.err_msg
    assert defer.called is (status == 429)
    response.close.assert_called_once()
    request.close.assert_called_once()


@pytest.mark.parametrize("status", [200, 500])
def test_non_json_or_missing_profile_response_is_rejected(boundary, status):
    """
    非 JSON 和空 500 错误保留接口失败，不泄露原始响应或异常正文。
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
    assert parser.userid is None and parser.err_msg == "无法连接 HDArea API"


@pytest.mark.parametrize("download,ratio,expected", [(1073741824, None, 5), (0, None, 0), (0, 0, 0)])
def test_missing_ratio_uses_traffic_without_infinity(boundary, download, ratio, expected):
    """
    站点未提供有限分享率时按流量计算，零下载量沿用零值合同。
    """
    parser, request = boundary
    request.get_res.return_value = _response(dict(PROFILE, downloaded=download, share_ratio=ratio))
    parser.parse()
    assert parser.userid == 42 and parser.ratio == expected


def test_missing_key_disables_fallback_and_persistence(boundary, monkeypatch):
    """
    资料失败不触发 NexusPHP 探测，刷新链不保存无身份的数据。
    """
    parser, request = boundary
    parser.apikey = None
    constructor = Mock(return_value=parser)
    constructor.schema = hdarea.SiteSchema.HDArea
    module = object.__new__(IndexerModule)
    module._site_schemas = [constructor, NexusPhpSiteUserInfo]
    fallback = Mock(side_effect=AssertionError("不应回退 Cookie 页面"))
    monkeypatch.setattr(NexusPhpSiteUserInfo, "parse", fallback)
    site = {"name": "高清视界", "url": "https://hdarea.club/", "schema": "HDArea", "public": False}
    userdata = module.refresh_userdata(site)
    assert userdata.userid is None and userdata.err_msg == site_rules.HDAREA_KEY_REQUIRED
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
