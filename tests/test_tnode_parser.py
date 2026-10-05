# -*- coding: utf-8 -*-
"""TNode 用户信息刷新顺序与失败保护回归测试。"""

import json
from unittest.mock import Mock

import pytest
from requests import Response

from app.adapters.network.http import RequestUtils
from app.modules.indexer.parser.tnode import TNodeSiteUserInfo
from app.runtime.config import settings

SITE_URL = "https://tnode.example/"
PROFILE_URL = f"{SITE_URL}api/user/getMainInfo"
SEEDING_URL = f"{SITE_URL}api/user/listTorrentActivity?id=&type=seeding&page=1&size=20000"
HOME_HTML = '<html><head><meta name="x-csrf-token" content="test-csrf"></head></html>'


@pytest.fixture
def user_info() -> dict:
    """构造不含真实账号信息的 TNode 用户资料。"""
    return {
        "id": 42,
        "username": "test-user",
        "class": {"name": "Power User"},
        "regTime": "2023-01-02 03:04:05",
        "upload": 4096,
        "download": 1024,
        "bonus": 12.5,
        "unreadAdmin": 1,
        "unreadInbox": 2,
        "unreadSystem": 3,
    }


@pytest.fixture
def pages(user_info: dict) -> dict[str, str]:
    """提供完整解析流程所需的离线首页、资料和做种响应。"""
    return {
        SITE_URL: HOME_HTML,
        PROFILE_URL: json.dumps({"status": 200, "data": user_info}),
        f"{SITE_URL}index.php": HOME_HTML,
        SEEDING_URL: json.dumps({
            "status": 200,
            "data": {"torrents": [{"size": 1024, "seeding": 2}, {"size": 2048, "seeding": 3}]},
        }),
    }


@pytest.fixture
def parser(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]) -> TNodeSiteUserInfo:
    """只替换 HTTP 边界，保留真实解析、请求头合并和会话关闭行为。"""
    client = Mock(spec=RequestUtils)

    def get_response(url: str, **_kwargs) -> Response:
        """拒绝未声明的请求，并返回离线 HTTP 响应。"""
        response = Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response._content = pages[url].encode("utf-8")
        return response

    client.get_res.side_effect = get_response
    monkeypatch.setattr(settings, "SITE_MESSAGE", False)
    return TNodeSiteUserInfo(
        site_name="TNode",
        url=SITE_URL,
        site_cookie="session=test-cookie",
        apikey=None,
        token=None,
        request_utils=client,
    )


def test_tnode_fetches_current_user_before_seeding(parser: TNodeSiteUserInfo) -> None:
    """空用户 ID 时仍应获取当前会话资料，之后继续刷新做种数据。"""
    assert parser.userid is None

    parser.parse()

    assert parser.userid == 42
    assert parser.username == "test-user"
    assert parser.user_level == "Power User"
    assert parser.join_at == "2023-01-02 03:04:05"
    assert parser.upload == 4096
    assert parser.download == 1024
    assert parser.ratio == 4
    assert parser.bonus == 12.5
    assert parser.message_unread == 6
    assert parser.seeding == 2
    assert parser.seeding_size == 3072
    assert parser.seeding_info == [[2, 1024], [3, 2048]]

    calls = parser._request_utils.get_res.call_args_list
    urls = [call.kwargs["url"] for call in calls]
    assert urls.count(PROFILE_URL) == 1
    assert urls.index(PROFILE_URL) < urls.index(SEEDING_URL)
    for call in calls:
        if call.kwargs["url"] in (PROFILE_URL, SEEDING_URL):
            assert call.kwargs["headers"]["X-CSRF-TOKEN"] == "test-csrf"
            assert call.kwargs["cookies"] == "session=test-cookie"
    parser._request_utils.close.assert_called_once_with()


@pytest.mark.parametrize("payload", ["", "<html>Login</html>", '{"status": 401}'])
def test_tnode_failed_profile_skips_seeding(
    parser: TNodeSiteUserInfo, pages: dict[str, str], payload: str,
) -> None:
    """资料为空、非 JSON 或业务失败时，不应继续请求做种接口。"""
    pages[PROFILE_URL] = payload

    parser.parse()

    urls = [call.kwargs["url"] for call in parser._request_utils.get_res.call_args_list]
    assert urls.count(PROFILE_URL) == 1
    assert SEEDING_URL not in urls
    assert parser.userid is None
    assert parser.username is None
    assert parser.seeding == 0
    parser._request_utils.close.assert_called_once_with()


@pytest.mark.parametrize("userid", [None, "", " ", 0, "0"])
def test_tnode_invalid_userid_skips_seeding(
    parser: TNodeSiteUserInfo, pages: dict[str, str], user_info: dict, userid,
) -> None:
    """即使资料接口成功，缺失或无效的用户 ID 仍应阻止做种请求。"""
    user_info["id"] = userid
    pages[PROFILE_URL] = json.dumps({"status": 200, "data": user_info})

    parser.parse()

    urls = [call.kwargs["url"] for call in parser._request_utils.get_res.call_args_list]
    assert urls.count(PROFILE_URL) == 1
    assert SEEDING_URL not in urls
    assert not parser._has_valid_userid()
    assert parser.seeding == 0
    parser._request_utils.close.assert_called_once_with()


def test_tnode_missing_csrf_skips_user_requests(parser: TNodeSiteUserInfo, pages: dict[str, str]) -> None:
    """首页缺少 CSRF 时，不应请求未经配置的资料或做种地址。"""
    pages[SITE_URL] = "<html>Login</html>"

    parser.parse()

    urls = [call.kwargs["url"] for call in parser._request_utils.get_res.call_args_list]
    assert urls == [SITE_URL, f"{SITE_URL}index.php"]
    assert parser.userid is None
    assert parser.seeding == 0
    parser._request_utils.close.assert_called_once_with()
