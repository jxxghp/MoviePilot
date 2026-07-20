# -*- coding: utf-8 -*-
from app.modules.indexer import parser as parser_module
from app.modules.indexer.parser.torrent_leech import TorrentLeechSiteUserInfo
from app.utils.string import StringUtils


PROFILE_VIEW_HTML = """
<table class="table table-bordered profileViewTable">
  <tr>
    <td>
      <div class="profile-details text-left">
        <div class="profile-username profile-details-item">example_user</div>
      </div>
    </td>
    <td>
      <div class="profile-info">
        <div class="profile-uploaded">
          uploaded:
          <span class="profile-info-details profile-uploaded-details">41.54 GB</span>
        </div>
        <div class="profile-downloaded">
          downloaded:
          <span class="profile-info-details">10.16 GB</span>
        </div>
        <div class="profile-ratio">
          ratio:
          <span class="profile-info-details profile-ratio-details">4.089</span>
        </div>
      </div>
    </td>
  </tr>
  <tr><td>Username</td><td>example_user</td></tr>
  <tr><td>Class</td><td>Registered</td></tr>
  <tr><td>Registration date</td><td>Sunday 4th September 2022</td></tr>
</table>
<span class="total-TL-points">123.45</span>
"""


def _build_parser(site_cookie: str = "session=masked") -> TorrentLeechSiteUserInfo:
    """
    构造 TorrentLeech 解析器测试实例

    :param site_cookie: 测试 Cookie
    :return: TorrentLeech 解析器
    """
    return TorrentLeechSiteUserInfo(
        site_name="TorrentLeech",
        url="https://www.torrentleech.me/",
        site_cookie=site_cookie,
        apikey=None,
        token=None,
    )


def test_torrent_leech_refresh_prefers_topbar_user_and_parses_profile_once(monkeypatch):
    """
    首页含其他用户链接时应使用顶栏用户名，并只请求一次资料页完成用户数据解析
    """
    parser = _build_parser()
    requested_urls = []

    def fake_get_page_content(url: str, **_) -> str:
        """返回离线页面并记录解析器请求地址"""
        requested_urls.append(url)
        if url == "https://www.torrentleech.me/":
            return """
            <html><body>
              <a href="/profile/other_user/view">other user</a>
              <span class="centerTopBar">
                <span class="link" onclick="window.location.href='/profile/example_user/view'">
                  example_user
                </span>
                <span onclick="window.location.href='/profile/example_user/notifications'"></span>
              </span>
            </body></html>
            """
        if url == "https://www.torrentleech.me/profile/example_user/view":
            return PROFILE_VIEW_HTML
        if url == "https://www.torrentleech.me/profile/example_user/seeding":
            return "<html><body><table><tbody></tbody></table></body></html>"
        return ""

    def fake_parse_logged_in(_: str) -> bool:
        """将离线首页视为已登录页面"""
        return True

    monkeypatch.setattr(parser_module.settings, "SITE_MESSAGE", False)
    monkeypatch.setattr(parser, "_get_page_content", fake_get_page_content)
    monkeypatch.setattr(parser, "_parse_logged_in", fake_parse_logged_in)

    parser.parse()

    assert parser.userid == "example_user"
    assert parser.username == "example_user"
    assert parser.upload == StringUtils.num_filesize("41.54 GB")
    assert parser.download == StringUtils.num_filesize("10.16 GB")
    assert parser.ratio == 4.089
    assert parser.user_level == "Registered"
    assert parser.join_at == "2022-09-04 00:00:00"
    assert parser.bonus == 123.45
    assert requested_urls.count("https://www.torrentleech.me/profile/example_user/view") == 1
    assert not any("/profile/None/" in url for url in requested_urls)


def test_torrent_leech_falls_back_to_generic_profile_link_without_topbar():
    """
    首页没有新版顶栏时应继续兼容通用资料链接中的用户 ID
    """
    parser = _build_parser()

    parser._parse_site_page('<a href="/profile/42/marketplace">Profile</a>')

    assert parser.userid == "42"
    assert parser._user_detail_page == "profile/42/view"
    assert parser._torrent_seeding_page == "profile/42/seeding"
