# -*- coding: utf-8 -*-
from urllib.parse import parse_qs, urlsplit

from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo


def _build_parser() -> NexusPhpSiteUserInfo:
    """
    构造 NexusPHP 解析器测试实例。
    """
    return NexusPhpSiteUserInfo(
        site_name="NexusPHP",
        url="https://example.com/",
        site_cookie="",
        apikey=None,
        token=None,
    )


def test_nexus_php_seeding_next_page_stops_when_userid_missing():
    """
    userid 未识别且下一页也缺少 userid 时应停止翻页而不是抛出异常。
    """
    parser = _build_parser()
    html_text = """
    <html>
      <body>
        <table class="torrents">
          <tr><td>标题</td><td>大小</td><td>在做种</td></tr>
        </table>
        <a href="getusertorrentlistajax.php?page=2">下一页</a>
      </body>
    </html>
    """

    next_page = parser._parse_user_torrent_seeding_info(html_text, multi_page=True)

    assert next_page is None


def test_nexus_php_seeding_next_page_checks_userid_parameter_name():
    """
    下一页链接缺少 userid 参数时，即使链接中包含用户 ID 字符串也应补齐 userid。
    """
    parser = _build_parser()
    parser.userid = "12"
    html_text = """
    <html>
      <body>
        <table class="torrents">
          <tr><td>标题</td><td>大小</td><td>在做种</td></tr>
        </table>
        <a href="getusertorrentlistajax.php?page=12&type=seeding">下一页</a>
      </body>
    </html>
    """

    next_page = parser._parse_user_torrent_seeding_info(html_text, multi_page=True)
    query_params = parse_qs(urlsplit(next_page).query)

    assert query_params["page"] == ["12"]
    assert query_params["type"] == ["seeding"]
    assert query_params["userid"] == ["12"]
