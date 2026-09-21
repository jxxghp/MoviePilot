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


def test_nexus_php_pttime_homepage_parses_bonus_and_user_level():
    """
    PTT-NP 首页应从魔力值容器和 UID 标记中解析魔力值、用户等级。
    """
    parser = _build_parser()
    html_text = """
    <html>
      <body>
        <script>if(h&gt;=20||h&lt;5)return 'night';</script>
        <a href="userdetails.php?id=67537" class="EliteUser_Name"><b>Opportunity</b></a>
        [UID=67537][(初中)Elite User]
        <span class="mr5">
          <font class="fwb">魔力值(96.67魔力/小时)</font>
          [<a href="mybonus.php" class="fcb">使用&amp;说明</a>]：14419.2
        </span>
      </body>
    </html>
    """

    parser._parse_site_page(html_text)
    parser._parse_user_base_info(html_text)

    assert parser.bonus == 14419.2
    assert parser.user_level == "初中"


def test_nexus_php_bonus_fallback_ignores_unrelated_script_numbers():
    """
    魔力值兜底解析不得把页面脚本中的无关数字当作积分。
    """
    parser = _build_parser()

    parser._parse_user_traffic_info(
        "<html><body><script>if(h&gt;=20||h&lt;5)return 'night';</script></body></html>"
    )

    assert parser.bonus == 0
