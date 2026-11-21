"""
按公开的星空适配协议构造页面回放，验证专站路由、容量单位及分页隔离。

页面均为合成结构，账号、Cookie 和 RSS 票据不来自真实用户。
"""

from typing import Optional
from unittest.mock import Mock

import pytest

from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.starspace import StarSpaceSiteUserInfo

BASE_URL = "https://star-space.net/"
HOME_URL = f"{BASE_URL}p_index/index.php"
DETAIL_URL = f"{BASE_URL}p_user/user_detail.php?uid=42"
SEEDING_URL = f"{BASE_URL}p_torrent/torrent_user.php?pop=8&uid=42"
NEXT_URL = f"{BASE_URL}p_torrent/torrent_user.php?page=2&pop=8&uid=42"
HOMEPAGE = """
<html><body>
  <a href="../p_user/user_detail.php?uid=42"><span class="uc1">Offline User</span></a>
  <span id="user_info"><a href="../p_user/user_upload.php?uid=42">上传：445.11 G</a></span>
  <span id="user_info_no_hover">下载：29.61 G</span>
  <a href="../p_torrent/torrent_user.php?pop=8&amp;uid=42">做种(3)</a>
  <a href="../p_torrent/torrent_user.php?pop=9&amp;uid=42">下载(2)</a>
  <a href="../p_bonus/bonus_hour.php">魔力：12,345.67</a>
  <a href="../p_login/logout.php">退出</a>
</body></html>
"""
DETAIL = """
<html><body><table>
  <tr id="tr_item"><td class="nowrap">用户名称</td><td><span>Offline User</span></td></tr>
  <tr id="tr_item"><td class="nowrap">用户等级</td><td><img alt="class6" src="../pic/class6.png"></td></tr>
  <tr id="tr_item"><td class="nowrap">加入日期</td><td>2024-01-02 03:04:05 (2年前)</td></tr>
  <tr id="tr_item"><td class="nowrap">魔力</td><td>23,456.78</td></tr>
  <tr id="tr_item"><td class="nowrap">传输</td><td><table><tr id="tr_item_min">
    <td>1 T</td><td>已完成</td><td>512 G</td><td>2.001</td>
  </tr></table></td></tr>
</table></body></html>
"""
FIRST_SEEDING_PAGE = """
<table id="table_tm">
  <tr><th>种子</th><th>分类</th><th>日期</th><th>大小</th><th>做种</th></tr>
  <tr id="tr_list"><td>One</td><td>电影</td><td>今天</td><td>2 G</td><td><a>12</a></td></tr>
  <tr class="tm_tr_top"><td>Two</td><td>电影</td><td>今天</td><td>3 GiB</td><td><a>4</a></td></tr>
</table>
<a href="torrent_user.php?pop=8&amp;uid=42&amp;page=2">下一页</a>
"""
LAST_SEEDING_PAGE = """
<table id="table_tm">
  <tr id="tr_list"><td>Three</td><td>音乐</td><td>昨天</td><td>1.5 T</td><td><a>6</a></td></tr>
</table>
"""


@pytest.fixture
def parser() -> StarSpaceSiteUserInfo:
    """
    注入无网络能力的客户端，保留真实解析生命周期。
    """
    return StarSpaceSiteUserInfo(
        "星空", HOME_URL, "ffid=42; auth=FAKECOOKIE", None, None, request_utils=Mock(),
    )


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], list[str]]:
    """
    只允许请求已登记的合成页面，错误路由会直接令用例失败。
    """
    pages = {HOME_URL: HOMEPAGE, DETAIL_URL: DETAIL, SEEDING_URL: FIRST_SEEDING_PAGE,
             NEXT_URL: LAST_SEEDING_PAGE}
    requested = []

    def read_page(_parser: StarSpaceSiteUserInfo, url: str, **_kwargs: object) -> str:
        """
        记录真实解析器请求的地址，回放对应页面。
        """
        requested.append(url)
        return pages[url]

    monkeypatch.setattr(StarSpaceSiteUserInfo, "_get_page_content", read_page)
    return pages, requested


def test_starspace_full_parse_keeps_prefixes_and_collects_all_seeding_pages(
    parser: StarSpaceSiteUserInfo, replay: tuple[dict[str, str], list[str]],
) -> None:
    """
    首页、详情和两页做种均通过正确路径读取，资料表显示值优先于首页概览。
    """
    _pages, requested = replay
    parser.parse()

    assert requested == [HOME_URL, DETAIL_URL, SEEDING_URL, NEXT_URL]
    assert parser.userid == "42" and parser.username == "Offline User"
    assert parser.user_level == "Veteran User"
    assert parser.join_at == "2024-01-02 03:04:05"
    assert parser.upload == 1024 ** 4
    assert parser.download == 512 * 1024 ** 3
    assert parser.ratio == 2.001
    assert parser.bonus == 23456.78
    assert parser.seeding == 3 and parser.leeching == 2
    assert parser.seeding_size == 5 * 1024 ** 3 + round(1.5 * 1024 ** 4)
    assert parser.seeding_info == [[12, 2 * 1024 ** 3], [4, 3 * 1024 ** 3], [6, round(1.5 * 1024 ** 4)]]
    parser._request_utils.close.assert_called_once()


def test_starspace_indexer_uses_declared_schema_without_reloading_parsers(
    monkeypatch: pytest.MonkeyPatch, replay: tuple[dict[str, str], list[str]],
) -> None:
    """
    资源声明可经真实索引模块生成用户统计，不动态重载其他站点解析器。
    """
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(module, "_site_schemas", [StarSpaceSiteUserInfo])

    result = module.refresh_userdata({
        "schema": "StarSpace", "name": "星空", "url": BASE_URL,
        "userdata_path": "p_index/index.php", "cookie": "ffid=42; auth=FAKECOOKIE", "public": False,
    })

    assert result is not None and not result.err_msg
    assert result.userid == "42" and result.seeding == 3
    assert result.upload == 1024 ** 4 and result.ratio == 2.001
    assert replay[1] == [HOME_URL, DETAIL_URL, SEEDING_URL, NEXT_URL]


@pytest.mark.parametrize("href", [
    "../p_user/user_detail.php?uid=42", "/p_user/user_detail.php?uid=42&view=1",
    "https://star-space.net/p_user/user_detail.php?uid=42",
])
def test_starspace_user_link_preserves_prefixed_detail_route(parser: StarSpaceSiteUserInfo, href: str) -> None:
    """
    相对和绝对个人链接均保留 p_user 前缀，不拼接不存在的 userdetails.php。
    """
    parser._parse_site_page(f'<a href="{href}"><span>Offline User</span></a>')
    assert parser.userid == "42" and parser.username == "Offline User"
    assert parser._user_detail_page == "p_user/user_detail.php?uid=42"
    assert parser._torrent_seeding_page == "p_torrent/torrent_user.php?pop=8&uid=42"


@pytest.mark.parametrize("href", [
    "../p_user/edit_user.php", "../p_user/user_detail.php?uid=0",
    "../p_user/user_detail.php?uid=abc", "../p_user/user_detail.php?uid=1&uid=2",
    "https://unrelated.example/p_user/user_detail.php?uid=42",
    "http://star-space.net/p_user/user_detail.php?uid=42",
    "../p_user/user_detail.php.bak?uid=42",
])
def test_starspace_does_not_infer_identity_from_cookie_or_unrelated_links(
    parser: StarSpaceSiteUserInfo, href: str,
) -> None:
    """
    资料链接无效时不能从 Cookie、脚本或其他站点的用户编号推断成功。
    """
    parser._parse_site_page(f'<a href="{href}">Offline User</a><script>var uid=42;</script>')
    assert parser.userid is None
    assert parser._user_detail_page is None and parser._torrent_seeding_page is None


@pytest.mark.parametrize("value,expected", [
    ("445.11 G", round(445.11 * 1024 ** 3)), ("2 GB", 2 * 1024 ** 3),
    ("2 GiB", 2 * 1024 ** 3), ("1,024 M", 1024 ** 3), ("0 B", 0),
    ("512", 512), ("", None), ("--", None), ("2 GBB", None),
])
def test_starspace_size_units_are_normalized_once(value: str, expected: Optional[int]) -> None:
    """
    单位只补齐一次 B，并区分明确的零值与未提供的容量。
    """
    assert StarSpaceSiteUserInfo._parse_size(value) == expected


def test_starspace_homepage_overview_and_missing_profile_keep_available_values(parser: StarSpaceSiteUserInfo) -> None:
    """
    概览中的无 B 单位和魔力可独立解析，空详情不会清空已取得的流量。
    """
    parser._parse_site_page(HOMEPAGE)
    parser._parse_user_base_info(HOMEPAGE)
    parser._parse_user_detail_info("")
    assert parser.upload == round(445.11 * 1024 ** 3)
    assert parser.download == round(29.61 * 1024 ** 3)
    assert parser.bonus == 12345.67
    assert parser.seeding == 3 and parser.leeching == 2


@pytest.mark.parametrize("href", [
    "https://unrelated.example/p_torrent/torrent_user.php?page=2", "/p_rss/rss_create.php",
    "http://star-space.net/p_torrent/torrent_user.php?page=2",
    "?pop=9&uid=42&page=2", "?pop=8&uid=99&page=2", "?pop=8&uid=42",
])
def test_starspace_seeding_rejects_unrelated_or_repeated_next_page(parser: StarSpaceSiteUserInfo, href: str) -> None:
    """
    下一页不能切换站点、用户或下载列表，也不能回到已请求的首页。
    """
    parser._parse_site_page(HOMEPAGE)
    page = FIRST_SEEDING_PAGE.split("</table>")[0] + f'</table><a rel="next" href="{href}">下一页</a>'
    assert parser._parse_user_torrent_seeding_info(page) is None
    assert parser.seeding == 2


def test_starspace_seeding_detects_cycle_and_does_not_guess_seeders(parser: StarSpaceSiteUserInfo) -> None:
    """
    循环链接只安排一次；页面未提供做种人数时不伪造人数分布。
    """
    parser._parse_site_page(HOMEPAGE)
    assert parser._parse_user_torrent_seeding_info(FIRST_SEEDING_PAGE) == NEXT_URL
    page = LAST_SEEDING_PAGE.replace("<td><a>6</a></td>", "<td>--</td>")
    page += f'<a rel="next" href="{NEXT_URL}">下一页</a>'
    assert parser._parse_user_torrent_seeding_info(page, multi_page=True) is None
    assert parser.seeding == 3
    assert len(parser.seeding_info) == 2


def test_starspace_invalid_login_stops_before_detail_and_seeding(
    parser: StarSpaceSiteUserInfo, replay: tuple[dict[str, str], list[str]],
) -> None:
    """
    登录页即使保留 Cookie 编号，也只能读取一次首页并报告未登录。
    """
    pages, requested = replay
    pages[HOME_URL] = '<form action="login_act.php"><input name="username"></form>'
    parser.parse()
    assert requested == [HOME_URL]
    assert parser.userid is None and parser.err_msg
