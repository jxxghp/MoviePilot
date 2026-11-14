# -*- coding: utf-8 -*-
"""
验证 HDDolby 用户信息、未读消息与无有效用户标识时的请求边界。
"""

from unittest.mock import Mock

import pytest

from app.modules.indexer.parser.hddolby import HDDolbySiteUserInfo
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo

USER_DATA_JSON = """
{
  "status": 0,
  "data": [{
    "id": "41088",
    "username": "g0m3e",
    "added": "2023-12-18 13:58:19",
    "class": "5",
    "uploaded": "1000",
    "downloaded": "500",
    "seedbonus": "100.0",
    "unread_messages": "1"
  }]
}
"""


def _build_parser(site_cookie: str = "", url: str = "https://www.hddolby.com/", **kwargs) -> HDDolbySiteUserInfo:
    """
    构造隔离 HTTP 会话、允许分别指定网页域名和 API 地址的杜比用户数据解析器。
    """
    return HDDolbySiteUserInfo(
        site_name="高清杜比",
        url=url,
        site_cookie=site_cookie,
        apikey="test-api-key",
        token=None,
        request_utils=Mock(),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("api_url", "expected_base"),
    [
        (None, "https://api.hddolby.com/api/v1/"),
        ("", "https://api.hddolby.com/api/v1/"),
        ("https://www.uhddolby.com/api/v1", "https://www.uhddolby.com/api/v1/"),
        ("https://www.uhddolby.com/api/v1/", "https://www.uhddolby.com/api/v1/"),
        ("https://api.hddolby.com/api/v1", "https://api.hddolby.com/api/v1/"),
        ("https://api.hddolby.com/api/v1/", "https://api.hddolby.com/api/v1/"),
        ("https://custom-api.test:8443/dolby/v1/", "https://custom-api.test:8443/dolby/v1/"),
    ],
)
def test_userdata_uses_resource_api_url_for_profile_and_seeding(monkeypatch, api_url, expected_base):
    """
    用户资料和做种统计使用同一资源 API 地址，保留 API Key 并关闭隔离会话。
    """
    parser = _build_parser(api_url=api_url)
    get_page = Mock(side_effect=[
        USER_DATA_JSON,
        '{"status": 0, "data": [{"size": 2048, "seeders": 3}]}',
    ])
    monkeypatch.setattr(parser, "_get_page_content", get_page)

    parser.parse()

    assert [call.kwargs["url"] for call in get_page.call_args_list] == [
        f"{expected_base}user/data", f"{expected_base}user/peers",
    ]
    assert parser._addition_headers == {"x-api-key": "test-api-key"}
    assert parser.userid == "41088"
    assert parser.upload == 1000
    assert parser.download == 500
    assert parser.seeding == 1
    assert parser.seeding_size == 2048
    parser._request_utils.close.assert_called_once_with()


def test_migrated_page_domain_keeps_old_api_for_userdata(monkeypatch):
    """
    网页迁移后仍从旧域 API 读取资料和做种统计，不修改网页地址或代理选择。
    """
    proxy = {"https": "socks5h://proxy.test:1080"}
    parser = _build_parser(url="https://www.uhddolby.com/", api_url="https://api.hddolby.com/api/v1", proxy=proxy)
    get_page = Mock(side_effect=[USER_DATA_JSON, '{"status": 0, "data": []}'])
    monkeypatch.setattr(parser, "_get_page_content", get_page)

    parser.parse()

    assert [call.kwargs["url"] for call in get_page.call_args_list] == [
        "https://api.hddolby.com/api/v1/user/data", "https://api.hddolby.com/api/v1/user/peers",
    ]
    assert parser.userid == "41088"
    assert parser._site_url == "https://www.uhddolby.com/"
    assert parser._proxy == proxy


def test_hddolby_parse_user_base_info_reads_unread_count():
    """
    用户基础信息解析保留用户标识、名称和未读消息数量。
    """
    parser = _build_parser()
    parser._parse_user_base_info(USER_DATA_JSON)

    assert parser.userid == "41088"
    assert parser.username == "g0m3e"
    assert parser.message_unread == 1


def test_hddolby_skips_message_body_without_cookie():
    """
    缺少 Cookie 时不获取未读消息正文。
    """
    parser = _build_parser(site_cookie="")
    parser.message_unread = 1

    parser._pase_unread_msgs()

    assert parser.message_unread_contents == []


def test_hddolby_reads_message_body_via_nexus_when_cookie_present(monkeypatch):
    """
    存在 Cookie 时通过 NexusPHP 解析流程读取未读消息正文。
    """
    parser = _build_parser(site_cookie="c_secure_uid=1")
    parser.message_unread = 1

    def fake_pase_unread_msgs(self):
        self.message_unread_contents = [
            ("测试标题", "2026-07-21 10:00:00", "测试正文"),
        ]

    monkeypatch.setattr(NexusPhpSiteUserInfo, "_pase_unread_msgs", fake_pase_unread_msgs)

    parser._pase_unread_msgs()

    assert len(parser.message_unread_contents) == 1
    head, date, content = parser.message_unread_contents[0]
    assert head == "测试标题"
    assert date == "2026-07-21 10:00:00"
    assert content == "测试正文"


def test_parser_skips_detail_and_seeding_requests_without_valid_userid(monkeypatch):
    """
    无法取得有效用户标识时，不请求用户详情和做种列表。
    """
    class DummyParser(HDDolbySiteUserInfo):
        """
        模拟已登录但用户标识缺失的站点解析器，检测多余请求。
        """
        def _parse_site_page(self, html_text: str):
            self._user_detail_page = "userdetails.php?id="
            self._user_basic_page = None
            self._user_traffic_page = None
            self._torrent_seeding_page = "getusertorrentlistajax.php?userid="

        def _parse_user_base_info(self, html_text: str):
            self.userid = None

        def _parse_user_traffic_info(self, html_text: str):
            raise AssertionError("should not request traffic page without valid userid")

        def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False):
            raise AssertionError("should not request seeding list without valid userid")

        def _parse_logged_in(self, html_text):
            return True

        def _parse_message_unread_links(self, html_text: str, msg_links: list):
            return None

        def _parse_message_content(self, html_text):
            return None, None, None

    parser = DummyParser(
        site_name="高清杜比",
        url="https://www.hddolby.com/",
        site_cookie="",
        apikey="test-api-key",
        token=None,
    )
    calls = []

    def fake_get_page_content(**kwargs):
        calls.append(kwargs.get("url", ""))
        return ""

    monkeypatch.setattr(parser, "_get_page_content", fake_get_page_content)

    parser.parse()

    assert not any("userdetails.php?id=" in call for call in calls)
    assert not any("getusertorrentlistajax.php?userid=" in call for call in calls)
