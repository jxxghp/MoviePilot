import pytest

from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.speedapp import SpeedAppSiteUserInfo


def _build_parser() -> SpeedAppSiteUserInfo:
    """构造不发起网络请求的 SpeedApp 解析器。"""
    return SpeedAppSiteUserInfo(
        site_name="SpeedApp",
        url="https://speedapp.io/",
        site_cookie="",
        apikey="",
        token="",
    )


def test_speedapp_schema_is_registered_and_uses_declared_pages() -> None:
    """SpeedApp schema 应能被模块发现，并只声明已确认的资料页面。"""
    parser = _build_parser()

    assert parser.site_schema() is SiteSchema.SpeedApp
    parser._parse_site_page("")
    assert parser._user_basic_page == "profile"
    assert parser._user_detail_page == "profile/menu-stats"
    assert parser._user_traffic_page is None
    assert parser._torrent_seeding_page is None


def test_speedapp_parser_recognizes_profile_link_as_logged_in() -> None:
    """SpeedApp 首页只有用户资料入口时也应识别为已登录。"""
    parser = _build_parser()

    assert parser._parse_logged_in('<a href="/profile/tester">tester</a>')


def test_speedapp_parser_reads_profile_and_menu_stats() -> None:
    """SpeedApp 资料页和菜单统计应映射到站点用户数据字段。"""
    parser = _build_parser()
    parser._parse_user_base_info(
        """
        <html><body>
          <div id="kt_profile_aside">
            <a href="/profile/tester">tester</a>
          </div>
          <div class="card-body pt-4"><div class="align-items-center"><div class="text-muted">Power User</div></div></div>
          <div id="notifications-oc-toggle"><div class="btn"><span class="label-danger">7</span></div></div>
          <dl>
            <dt>Uploaded</dt><dd>1.5 TB</dd>
            <dt>Downloaded</dt><dd>500 GB</dd>
            <dt>Ratio</dt><dd>3.00</dd>
            <dt>Signup date</dt><dd>2026-01-02 03:04:05</dd>
            <dt>Bonus points</dt><dd><b>12.5</b><b>2.25 TB</b></dd>
          </dl>
        </body></html>
        """
    )
    parser._parse_user_detail_info(
        """
        <html><body>
          <a href="/shop" title="Bonus points">12,345</a>
          <a href="/snatch/seeding" title="Currently seeding torrents">42</a>
        </body></html>
        """
    )

    assert parser.userid == "tester"
    assert parser.username == "tester"
    assert parser.user_level == "Power User"
    assert parser.upload == 1.5 * 1024 ** 4
    assert parser.download == 500 * 1024 ** 3
    assert parser.ratio == 3.0
    assert parser.join_at == "2026-01-02 03:04:05"
    assert parser.bonus == 12345
    assert parser.seeding == 42
    assert parser.seeding_size == 2.25 * 1024 ** 4
    assert parser.message_unread == 7


def test_speedapp_parse_fetches_only_declared_profile_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整解析流程应按资源声明读取资料和菜单统计页面。"""
    parser = _build_parser()
    pages = {
        "https://speedapp.io/": '<a href="/profile/tester">tester</a>',
        "https://speedapp.io/profile": """
            <div id="kt_profile_aside"><a href="/profile/tester">tester</a></div>
            <dl><dt>Uploaded</dt><dd>1 GB</dd><dt>Downloaded</dt><dd>1 GB</dd></dl>
        """,
        "https://speedapp.io/profile/menu-stats": """
            <a href="/shop" title="Bonus points">10</a>
            <a href="/snatch/seeding" title="Currently seeding torrents">2</a>
        """,
    }
    calls = []

    def fake_get_page_content(url: str, **_kwargs: object) -> str:
        """按绝对地址回放 SpeedApp 页面。"""
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(parser, "_get_page_content", fake_get_page_content)
    monkeypatch.setattr(
        "app.modules.indexer.parser.get_runtime_setting",
        lambda key: False if key == "SITE_MESSAGE" else None,
    )

    parser.parse()

    assert calls == ["https://speedapp.io/", "https://speedapp.io/profile", "https://speedapp.io/profile/menu-stats"]
    assert parser.userid == "tester"
    assert parser.bonus == 10
    assert parser.seeding == 2
