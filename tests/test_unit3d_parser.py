# -*- coding: utf-8 -*-
from app.modules.indexer.parser.unit3d import Unit3dSiteUserInfo

USER_BASE_HTML = """
<html>
  <body>
    <a href="/users/monica/settings">设置</a>
    <a href="/bonus/earnings">积分 1,234.5</a>
  </body>
</html>
"""


def _build_parser() -> Unit3dSiteUserInfo:
    """构造一个用于测试的 Unit3D 解析器。"""
    return Unit3dSiteUserInfo(
        site_name="莫妮卡",
        url="https://udm.example/",
        site_cookie="test-cookie",
        apikey=None,
        token=None,
    )


def test_unit3d_user_name_is_used_as_valid_userid() -> None:
    """解析用户设置链接后，应允许详情页和做种页继续请求。"""
    parser = _build_parser()

    parser._parse_user_base_info(USER_BASE_HTML)

    assert parser.username == "monica"
    assert parser.userid == "monica"
    assert parser._has_valid_userid()
    assert parser._user_detail_page == "/users/monica"
    assert parser._torrent_seeding_page.startswith("/users/monica/active?")
    assert parser.bonus == 1234.5
