"""
公共类型声明须保留站点解析器的分页、空结果及初始化兼容行为。
"""

import json
from unittest.mock import Mock

import pytest

from app.modules.indexer.parser import SiteParserBase
from app.modules.indexer.parser.mtorrent import MTorrentSiteUserInfo
from app.modules.indexer.parser.nexus_audiences import NexusAudiencesSiteUserInfo
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo


@pytest.fixture
def parser():
    """
    使用受控请求客户端，测试不创建或发送真实 HTTP 请求。
    """
    return NexusPhpSiteUserInfo("test", "https://example.com/", None, None, None,
                                request_utils=Mock())


@pytest.mark.parametrize("next_page", [None, False, ""])
def test_seeding_pagination_distinguishes_empty_path_from_terminal(parser, monkeypatch, next_page):
    """
    空路径仍触发第二页请求，None 与 False 均立即结束。
    """
    parser.userid = 42
    parser._torrent_seeding_page = "seeding"
    request = Mock(return_value="page")
    decode = Mock(side_effect=[next_page, None])
    monkeypatch.setattr(parser, "_get_page_content", request)
    monkeypatch.setattr(parser, "_parse_user_torrent_seeding_info", decode)

    SiteParserBase._parse_seeding_pages(parser)

    count = 2 if next_page == "" else 1
    assert request.call_count == decode.call_count == count
    if count == 2:
        assert request.call_args.kwargs["url"] == "https://example.com/seeding"
        assert decode.call_args.kwargs["multi_page"] is True


@pytest.mark.parametrize("message", [None, (None, None, None), ("标题", "日期", "正文")])
def test_unread_message_missing_result_keeps_failure_boundary(parser, monkeypatch, message):
    """
    无结果仍抛出解包错误，合法三元组按原样进入待通知清单。
    """
    parser.message_unread = 1
    parser._sys_mail_unread_page = None

    def collect(_text, links):
        """
        模拟一页未读消息链接。
        """
        links.append("message/1")
        return None

    monkeypatch.setattr(parser, "_get_page_content", Mock(return_value="page"))
    monkeypatch.setattr(parser, "_parse_message_unread_links", collect)
    monkeypatch.setattr(parser, "_parse_message_content", Mock(return_value=message))
    if message is None:
        with pytest.raises(TypeError, match="cannot unpack non-iterable NoneType object"):
            SiteParserBase._pase_unread_msgs(parser)
        assert parser.message_unread_contents == []
    else:
        SiteParserBase._pase_unread_msgs(parser)
        assert parser.message_unread_contents == [message]


@pytest.mark.parametrize("page_number", [1, 1.5, "1"])
def test_mtorrent_next_page_preserves_numeric_increment_and_invalid_input(monkeypatch, page_number):
    """
    页码按既有加法递增，保留参数对象；字符串不会被隐式转为数字。
    """
    parser = MTorrentSiteUserInfo("test", "https://example.com/", None, "key", None,
                                  request_utils=Mock())
    parameters = {"pageNumber": page_number, "pageSize": 200, "type": "SEEDING"}
    parser._torrent_seeding_params = parameters
    monkeypatch.setattr(parser, "_get_page_content", Mock(return_value='{"data":{"seeder":2}}'))
    payload = json.dumps({"code": "0", "data": {"data": [
        {"torrent": {"size": "1024", "source": "3"}}
    ]}})
    if isinstance(page_number, str):
        with pytest.raises(TypeError):
            parser._parse_user_torrent_seeding_info(payload)
        assert parameters["pageNumber"] == page_number
    else:
        assert parser._parse_user_torrent_seeding_info(payload) == ""
        assert parameters["pageNumber"] == page_number + 1
    assert parser._torrent_seeding_params is parameters


@pytest.mark.parametrize("use_keywords", [False, True])
def test_audiences_constructor_preserves_base_arguments(use_keywords):
    """
    显式构造签名继续接受父类全部位置参数与关键字参数。
    """
    client = Mock()
    arguments = {
        "site_name": "test", "url": "https://example.com/", "site_cookie": None,
        "apikey": None, "token": None, "request_utils": client, "ua": "agent",
        "emulate": True, "proxy": None, "api_url": "https://api.example.com/",
    }
    parser = (NexusAudiencesSiteUserInfo(**arguments) if use_keywords
              else NexusAudiencesSiteUserInfo(*arguments.values()))
    assert parser._request_utils is client
    assert parser._ua == "agent" and parser._emulate is True and parser._proxy is None
    assert parser._api_url == arguments["api_url"]
    assert parser._user_mail_unread_page == "messages.php?action=viewmailbox&box=1&unread=yes"
    assert parser._sys_mail_unread_page is None
