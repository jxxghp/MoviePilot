# -*- coding: utf-8 -*-
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


def _build_parser(site_cookie: str = "") -> HDDolbySiteUserInfo:
    return HDDolbySiteUserInfo(
        site_name="高清杜比",
        url="https://www.hddolby.com/",
        site_cookie=site_cookie,
        apikey="test-api-key",
        token=None,
    )


def test_hddolby_parse_user_base_info_reads_unread_count():
    parser = _build_parser()
    parser._parse_user_base_info(USER_DATA_JSON)

    assert parser.userid == "41088"
    assert parser.username == "g0m3e"
    assert parser.message_unread == 1


def test_hddolby_skips_message_body_without_cookie():
    parser = _build_parser(site_cookie="")
    parser.message_unread = 1

    parser._pase_unread_msgs()

    assert parser.message_unread_contents == []


def test_hddolby_reads_message_body_via_nexus_when_cookie_present(monkeypatch):
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
