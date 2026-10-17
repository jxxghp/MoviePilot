"""
验证NexusPHP 页面结构与种子字段兼容。
"""

# -*- coding: utf-8 -*-
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
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


def test_nexus_php_depthstudio_traffic_falls_back_to_details():
    """
    首页解析失败后从详情表格补齐流量，不依赖卡片类名或实际流量。
    """
    html_text = (Path(__file__).parent / "fixtures" / "depthstudio_userdetails.html").read_text(encoding="utf-8")
    html = etree.HTML(html_text)
    homepage = etree.tostring(html.xpath("//div")[0], encoding="unicode")
    homepage = homepage.replace("info-stat", "other-style")
    parser = _build_parser()

    parser._parse_user_base_info(homepage)

    assert parser.upload == 0
    assert parser.download == 0

    parser._parse_user_detail_info(html_text)

    assert parser.upload == round(323.04 * 1024 ** 3)
    assert parser.download == round(73.54 * 1024 ** 3)
    assert parser.ratio == 4.393


def test_nexus_php_detail_traffic_preserves_nonzero_values():
    """
    详情兜底不能覆盖已有的非零流量和分享率。
    """
    parser = _build_parser()
    parser._parse_user_base_info(
        "<html><body>上传量: 1 GB 下载量: 2 GB 分享率: 0.5</body></html>"
    )
    original = parser.upload, parser.download, parser.ratio

    parser._parse_user_detail_info(
        '<table><tr><td><strong>上传量</strong>: 323.04 GB</td>'
        '<td><strong>下载量</strong>: 73.54 GB</td></tr>'
        '<tr><td><strong>分享率</strong>: 4.392</td></tr></table>'
    )

    assert (parser.upload, parser.download, parser.ratio) == original


@pytest.mark.parametrize("homepage, expected_upload, expected_download, expected_ratio", [
    ("上传量: 1 GB", 1024 ** 3, round(73.54 * 1024 ** 3), 4.392),
    ("下载量: 0 B 分享率: 0", round(323.04 * 1024 ** 3), round(73.54 * 1024 ** 3), 4.392),
    ("上传量: 0 B 下载量: 0 B 分享率: 0", round(323.04 * 1024 ** 3), round(73.54 * 1024 ** 3), 4.392),
    ("上传量: 1 GB 下载量: 2 GB 分享率: 0", 1024 ** 3, 2 * 1024 ** 3, 4.392),
])
def test_nexus_php_detail_traffic_fills_zero_fields(
    homepage, expected_upload, expected_download, expected_ratio
):
    """
    零值字段允许从详情补读，分享率优先取详情表格的显示值。
    """
    parser = _build_parser()
    parser._parse_user_base_info(f"<html><body>{homepage}</body></html>")
    detail = (Path(__file__).parent / "fixtures" / "depthstudio_userdetails.html").read_text(encoding="utf-8")

    parser._parse_user_detail_info(detail)

    assert parser.upload == expected_upload
    assert parser.download == expected_download
    assert parser.ratio == expected_ratio


def test_nexus_php_zero_ratio_uses_final_traffic_without_displayed_ratio():
    """
    详情未显示分享率时，按补读后的上传/下载量重新计算零分享率。
    """
    parser = _build_parser()
    parser._parse_user_base_info("<html><body>上传量: 0 B 下载量: 0 B 分享率: 0</body></html>")

    parser._parse_user_detail_info(
        '<table><tr><td><strong>上传量</strong>: 8 GB</td>'
        '<td><strong>下载量</strong>: 2 GB</td></tr></table>'
    )

    assert parser.upload == 8 * 1024 ** 3
    assert parser.download == 2 * 1024 ** 3
    assert parser.ratio == 4.0


@pytest.mark.parametrize("table, expected_upload, expected_download, expected_ratio", [
    ('<tr><td><strong>上传量</strong>: 1.5 <span>TB</span></td>'
     '<td><strong>下载量</strong>: 512 <span>GB</span></td></tr>',
     round(1.5 * 1024 ** 4), 512 * 1024 ** 3, 3.0),
    ('<tr><td>上傳量</td><td>1,024 GB</td></tr><tr><td>下載量</td><td>512 GB</td></tr>'
     '<tr><td>分享率</td><td>2.001</td></tr>',
     1024 ** 4, 512 * 1024 ** 3, 2.001),
    ('<tr><td><strong>实际上传量</strong>: 1 TB</td>'
     '<td><strong>实际下载量</strong>: 1 TB</td></tr>', 0, 0, 0.0),
    ('<tr><td>上传量</td><td>0 B</td></tr><tr><td>下载量</td><td>0 B</td></tr>', 0, 0, 0.0),
    ('<tr><td>上传量</td><td>未知</td></tr><tr><td>下载量</td><td>未知</td></tr>', 0, 0, 0.0),
])
def test_nexus_php_detail_traffic_table(table, expected_upload, expected_download, expected_ratio):
    """
    按表格字段识别流量，支持嵌套单位和分列布局，并排除实际流量。
    """
    parser = _build_parser()
    parser._parse_user_base_info("<html><body></body></html>")

    parser._parse_user_detail_info(f"<html><body><table>{table}</table></body></html>")

    assert parser.upload == expected_upload
    assert parser.download == expected_download
    assert parser.ratio == expected_ratio


@pytest.mark.parametrize("download, ratio_text, expected_ratio", [
    ("512 GB", "分享率: 3.001", 3.001),
    ("512 GB", "", 3.0),
    ("0 B", "", 0.0),
])
def test_nexus_php_legacy_traffic(download, ratio_text, expected_ratio):
    """
    保留传统页面解析、分享率计算及零下载量行为。
    """
    parser = _build_parser()

    parser._parse_user_traffic_info(
        f"<html><body>上传量: 1.5 TB 下载量: {download} {ratio_text}</body></html>"
    )

    assert parser.upload == round(1.5 * 1024 ** 4)
    assert parser.download == (512 * 1024 ** 3 if download == "512 GB" else 0)
    assert parser.ratio == expected_ratio


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
    """
    UID 标记缺少通用等级或属于其他用户时，不应解析站点专用等级。
    """
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


def test_nexus_php_pterclub_profile_parses_current_seeding_table():
    """
    猫站用户详情页的“当前做种”表应按第 4、5 列统计做种体积和做种人数。
    """
    parser = _build_parser()
    parser.userid = "123"
    html_text = """
    <html><body>
      <table><tr>
        <td>当前做种</td>
        <td><table><tbody>
          <tr><td>详情</td><td><a>种子一</a></td><td>分类</td><td>1.5 TB</td><td><a>12</a></td></tr>
          <tr><td>详情</td><td><a>种子二</a></td><td>分类</td><td>700 GB</td><td><a>3</a></td></tr>
        </tbody></table></td>
      </tr></table>
    </body></html>
    """

    parser._parse_user_detail_info(html_text)

    assert parser.seeding == 2
    assert parser.seeding_size == 1.5 * 1024**4 + 700 * 1024**3
    assert parser.seeding_info == [[12, int(1.5 * 1024**4)], [3, 700 * 1024**3]]


def test_nexus_php_pterclub_full_seeding_list_uses_site_columns():
    """
    猫站完整做种页即使没有表头，也应读取第 4 列体积和第 5 列做种人数。
    """
    parser = _build_parser()
    parser._site_domain = "pterclub.net"
    parser.userid = "123"
    html_text = """
    <html><body>
      <div id="outer"><table><tbody>
        <tr><td>详情</td><td><a href="details.php?id=1">种子一</a></td><td>分类</td><td>1.5 TB</td><td><a>12</a></td></tr>
      </tbody></table>
      <p class="np-pager"><font class="gray">1</font><a href="getusertorrentlist.php?userid=123&type=seeding&page=2">下一页</a></p></div>
    </body></html>
    """

    next_page = parser._parse_user_torrent_seeding_info(html_text)

    assert parser.seeding == 1
    assert parser.seeding_size == int(1.5 * 1024**4)
    assert parser.seeding_info == [[12, int(1.5 * 1024**4)]]
    assert next_page == "getusertorrentlist.php?userid=123&type=seeding&page=2"


def test_nexus_php_seeding_list_replaces_profile_fallback_without_double_counting(monkeypatch):
    """
    做种列表成功时应覆盖详情页中的部分做种统计，避免重复累加。
    """
    parser = _build_parser()
    parser.userid = "123"
    parser._torrent_seeding_page = "getusertorrentlistajax.php?userid=123&type=seeding"
    parser._parse_user_detail_info("""
        <html><body><table><tr>
          <td>当前做种</td>
          <td><table><tbody>
            <tr><td>详情</td><td>种子一</td><td>分类</td><td>1.5 TB</td><td>12</td></tr>
          </tbody></table></td>
        </tr></table></body></html>
    """)
    list_html = """
    <html><body><table class="torrents">
      <tr><td>标题</td><td>分类</td><td>大小</td><td>在做种</td></tr>
      <tr><td>种子一</td><td>分类</td><td>500 GB</td><td><b><a>7</a></b></td></tr>
    </table></body></html>
    """
    monkeypatch.setattr(parser, "_get_page_content", lambda **_: list_html)

    parser._parse_seeding_pages()

    assert parser.seeding == 1
    assert parser.seeding_size == 500 * 1024**3
    assert parser.seeding_info == [[7, 500 * 1024**3]]


def test_custom_seeding_endpoints_and_pterclub_navigation_headers():
    """
    已确认的定制站点直接使用真实入口，猫站请求头不影响其他站点。
    """
    endpoints = {
        "pterclub.net": "getusertorrentlist.php?userid=123&type=seeding",
        "pterclub.com": "getusertorrentlist.php?userid=123&type=seeding",
        "keepfrds.com": "torrents.php?option-torrents=3&userid=123",
        "pt.sjtu.edu.cn": "viewtorrentsdetail.php?id=123",
    }
    for domain, endpoint in endpoints.items():
        parser = _build_parser()
        parser._site_domain = domain
        parser._parse_site_page('<a href="userdetails.php?id=123">user</a>')
        assert parser._torrent_seeding_page == endpoint
        if domain.startswith("pterclub"):
            assert parser._torrent_seeding_headers["Accept-Language"]
            assert parser._torrent_seeding_headers["Accept"].startswith("text/html")
            assert parser._torrent_seeding_headers["Sec-Fetch-Dest"] == "document"
            assert parser._torrent_seeding_headers["Sec-Fetch-Mode"] == "navigate"
            assert parser._torrent_seeding_headers["Sec-Fetch-Site"] == "same-origin"
            assert parser._user_detail_headers == parser._torrent_seeding_headers
        else:
            assert parser._torrent_seeding_headers is None


def test_keepfrds_unmarked_columns_preserve_profile_count(monkeypatch):
    """
    朋友列表含做种时长，汇总数量与可见明细不一致时保留汇总和真实明细体积。
    """
    parser = _build_parser()
    parser._site_domain = "keepfrds.com"
    parser._parse_site_page('<a href="userdetails.php?id=123">user</a>')
    parser._parse_user_detail_info('<table><tr><td>当前做种</td><td><a>7</a></td></tr></table>')
    page = '''<table class="torrents"><tr><td>类型</td><td>标题</td><td>魔力</td>
        <td>时长</td><td>体积</td><td>人数</td></tr>
        <tr><td>电影</td><td><a>test</a></td><td>123</td><td>1月7天</td>
        <td>7.29GiB</td><td>4</td></tr></table>'''
    monkeypatch.setattr(parser, "_get_page_content", lambda **_kwargs: page)
    parser._parse_seeding_pages()
    assert parser.seeding == 7
    assert parser.seeding_size == parser.num_filesize("7.29GiB")
    assert parser.seeding_info == [[4, parser.num_filesize("7.29GiB")]]


def test_pterclub_profile_size_overrides_partial_paginated_list(monkeypatch):
    """
    猫站汇总体积优先于分页明细累加，翻页继续沿用导航头且不重复汇总。
    """
    parser = _build_parser()
    parser._site_domain = "pterclub.net"
    parser._parse_site_page('<a href="userdetails.php?id=123">user</a>')
    parser._parse_user_detail_info('<table><tr><td>做种大小</td><td>5.522 TB</td></tr></table>')
    page = '''<h1>正在做种的种子 (共 470 个)</h1><table class="torrents">
        <tr><td>类型</td><td>标题</td><td>时间</td><td>大小</td><td>人数</td></tr>
        <tr><td>电影</td><td><a>test</a></td><td>昨天</td><td>1GB</td><td>4</td></tr></table>'''
    calls = []

    def get_page(**kwargs):
        """
        回放分页内容并记录请求参数。
        """
        calls.append(kwargs)
        if len(calls) == 1:
            return page + '<a href="getusertorrentlist.php?page=1">下一页</a>'
        return page

    monkeypatch.setattr(parser, "_get_page_content", get_page)
    parser._parse_seeding_pages()
    assert parser.seeding == 470
    assert parser.seeding_size == parser.num_filesize("5.522 TB")
    assert len(parser.seeding_info) == 2
    assert len(calls) == 2
    assert calls[1]["headers"]["Sec-Fetch-Mode"] == "navigate"
    assert "userid=123" in calls[1]["url"]


def test_putao_summary_only_does_not_follow_global_torrent_list(monkeypatch):
    """
    葡萄只取明确做种计数，不推算体积或扫描全站列表。
    """
    parser = _build_parser()
    parser._site_domain = "pt.sjtu.edu.cn"
    parser._parse_site_page('<a href="userdetails.php?id=123">user</a>')
    calls = []

    def get_page(**kwargs):
        """
        回放葡萄异步汇总响应。
        """
        calls.append(kwargs)
        return '<div>当前做种 21 完成种子数 2 未完成种子数 138</div>'

    monkeypatch.setattr(parser, "_get_page_content", get_page)
    parser._parse_seeding_pages()
    assert len(calls) == 1
    assert calls[0]["url"].endswith("viewtorrentsdetail.php?id=123")
    assert parser.seeding == 21
    assert parser.seeding_size == 0
    assert parser.seeding_info == []


def test_nexus_empty_seeding_response_logs_warning(monkeypatch):
    """
    接口失败为空时应提示可诊断警告，并保留详情页汇总。
    """
    parser = _build_parser()
    parser.userid = "123"
    parser.seeding = 7
    parser.seeding_size = 1024
    warnings = []
    monkeypatch.setattr("app.modules.indexer.parser.nexus_php.logger.warning", warnings.append)
    monkeypatch.setattr(parser, "_get_page_content", lambda **_kwargs: "")
    parser._parse_seeding_pages()
    assert parser.seeding == 7
    assert parser.seeding_size == 1024
    assert any("未返回内容" in warning for warning in warnings)


def test_pterclub_headers_reach_http_transport_and_failures_are_visible(monkeypatch):
    """
    导航头实际传递给统一 HTTP 客户端，404/403 警告不暴露 Cookie 或用户标识。
    """
    from types import SimpleNamespace

    parser = _build_parser()
    parser._site_domain = "pterclub.net"
    parser._parse_site_page('<a href="userdetails.php?id=123">user</a>')
    calls, warnings = [], []

    def get_response(**kwargs):
        """
        回放 HTTP 失败响应，验证传输层参数。
        """
        calls.append(kwargs)
        return SimpleNamespace(status_code=404, text="File not found.")

    monkeypatch.setattr(parser._request_utils, "get_res", get_response)
    monkeypatch.setattr("app.modules.indexer.parser.logger.warning", warnings.append)
    assert parser._get_page_content(url="https://pterclub.net/getusertorrentlist.php?userid=123",
                                    headers=parser._torrent_seeding_headers) == ""
    assert calls[0]["headers"]["Sec-Fetch-Site"] == "same-origin"
    assert any("HTTP 404" in warning for warning in warnings)
    assert all("userid" not in warning for warning in warnings)
    monkeypatch.setattr(parser._request_utils, "get_res",
                        lambda **_kwargs: SimpleNamespace(status_code=403, text="Forbidden"))
    monkeypatch.setattr("app.modules.indexer.parser.RequestUtils.get_decoded_html_content",
                        lambda *_args: "Forbidden")
    parser._get_page_content(url="https://pterclub.net/", headers=parser._torrent_seeding_headers)
    assert any("HTTP 403" in warning for warning in warnings)
