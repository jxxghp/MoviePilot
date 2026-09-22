# -*- coding: utf-8 -*-
from urllib.parse import parse_qs, urlsplit

from lxml import etree

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
    PTT-NP 首页应从魔力值容器和 UID 标记中解析魔力值、通用用户等级。
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
    assert parser.user_level == "Elite User"


def test_nexus_php_pttime_marker_requires_common_level_for_current_user():
    """UID 标记缺少通用等级或属于其他用户时，不应解析站点专用等级。"""
    parse_marker = NexusPhpSiteUserInfo._parse_user_level_marker

    assert parse_marker("[UID=67537][(初中)]", "67537") is None
    assert parse_marker("[UID=123][(初中)Elite User]", "67537") is None


def test_nexus_php_bonus_fallback_ignores_unrelated_script_numbers():
    """
    魔力值兜底解析不得把页面脚本中的无关数字当作积分。
    """
    parser = _build_parser()

    parser._parse_user_traffic_info(
        "<html><body><script>if(h&gt;=20||h&lt;5)return 'night';</script></body></html>"
    )

    assert parser.bonus == 0


def test_nexus_php_bonus_reads_value_nested_in_mybonus_link():
    """
    憨憨等站点将魔力值嵌在 mybonus 链接内的元素中，且页面公告里存在无关的“魔力值…：数字”文本。
    """
    parser = _build_parser()
    html_text = """
    <html>
      <body>
        <div>
          视工作情况会给予魔力值、邀请码等奖励。具体处理措施如下：
          1、补偿所有用户100000憨豆；（已经发放完成）
        </div>
        <div class="flex flex-row items-center">
          <img src="styles/HHan/icons/icon-bean.svg" alt="憨豆">
          <a href="mybonus.php">
            <div class="text-sm flex flex-wrap break-all">13,853,582</div>
          </a>
        </div>
      </body>
    </html>
    """

    parser._parse_user_traffic_info(html_text)

    assert parser.bonus == 13853582.0


def test_nexus_php_uuid_user_details_link_parses_uuid_user():
    """
    NexusPHP v1.10+ 使用 UUID 标识用户的站点应解析用户详情链接，并使用 useruuid 请求做种列表。
    """
    parser = _build_parser()
    html_text = """
    <html>
      <body>
        <table id="info_block"><tr><td>
          欢迎回来, <span class="nowrap"><a href="https://mua.xloli.cc/userdetails.php?uuid=53d25816-f7b7-4372-b58d-6f445acfe090" class='User_Name'><b>Andreas</b></a></span>
          <font class='color_bonus'>魔力值 </font>[<a href="mybonus.php">使用</a>]: 105.5
        </td></tr></table>
      </body>
    </html>
    """

    parser._parse_site_page(html_text)
    parser._parse_user_base_info(html_text)

    assert parser.userid == "53d25816-f7b7-4372-b58d-6f445acfe090"
    assert parser._user_detail_page == "userdetails.php?uuid=53d25816-f7b7-4372-b58d-6f445acfe090"
    assert parser._torrent_seeding_page == (
        "getusertorrentlistajax.php?useruuid=53d25816-f7b7-4372-b58d-6f445acfe090&type=seeding"
    )
    assert parser.username == "Andreas"
    assert parser.bonus == 105.5


def test_nexus_php_numeric_user_details_link_keeps_userid_param():
    """
    传统数字用户 ID 的站点应继续使用 userid 请求做种列表。
    """
    parser = _build_parser()
    html_text = '<html><body><a href="userdetails.php?id=67537"><b>Opportunity</b></a></body></html>'

    parser._parse_site_page(html_text)

    assert parser.userid == "67537"
    assert parser._torrent_seeding_page == "getusertorrentlistajax.php?userid=67537&type=seeding"


def test_nexus_php_uuid_seeding_js_link_uses_useruuid():
    """
    用户详情页的做种 JS 调用携带 UUID 时，应改用 useruuid 参数请求做种列表。
    """
    parser = _build_parser()
    # 首页未解析到用户链接时，做种地址为空，需要从用户详情页的 JS 调用中补齐
    parser._torrent_seeding_page = None
    html_text = """
    <html>
      <body>
        <a href="javascript: getusertorrentlistajax('53d25816-f7b7-4372-b58d-6f445acfe090', 'seeding', 'ka1'); klappe_news('a1')">[显示/隐藏]</a>
      </body>
    </html>
    """

    parser._fixup_torrent_seeding_page(etree.HTML(html_text))

    assert parser.userid == "53d25816-f7b7-4372-b58d-6f445acfe090"
    assert parser._torrent_seeding_page == (
        "getusertorrentlistajax.php?useruuid=53d25816-f7b7-4372-b58d-6f445acfe090&type=seeding"
    )


def test_nexus_php_uuid_seeding_next_page_uses_useruuid():
    """
    UUID 站点的做种下一页地址缺少用户标识时，应补齐 useruuid 而不是 userid。
    """
    parser = _build_parser()
    parser.userid = "53d25816-f7b7-4372-b58d-6f445acfe090"
    html_text = """
    <html>
      <body>
        <table class="torrents">
          <tr><td>标题</td><td>大小</td><td>在做种</td></tr>
        </table>
        <a href="getusertorrentlistajax.php?page=2&type=seeding">下一页</a>
      </body>
    </html>
    """

    next_page = parser._parse_user_torrent_seeding_info(html_text, multi_page=True)
    query_params = parse_qs(urlsplit(next_page).query)

    assert query_params["useruuid"] == ["53d25816-f7b7-4372-b58d-6f445acfe090"]
    assert "userid" not in query_params
    assert query_params["type"] == ["seeding"]
