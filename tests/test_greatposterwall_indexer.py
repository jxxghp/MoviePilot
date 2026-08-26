from app.adapters.system import rust as rust_accel
from app.modules.indexer.parser.gazelle import GazelleSiteUserInfo
from app.modules.indexer.spider import SiteSpider

GPW_HTML = """
<html><body>
<table class="TableTorrent Table" id="torrent_table"><tbody>
  <tr class="TableTorrent-rowTitle Table-row torrent_checked">
    <td class="Table-cell is-name" colspan="3">
      <div class="TableTorrent-title">
        <span class="TableTorrent-titleActions">
          <a href="/torrents.php?action=download&amp;id=115362">DL</a>
        </span>
        <a data-tooltip="Nobody.2025.1080p.WEB-DL.H.265-Example-GROUP.mkv"
           href="/torrents.php?id=8064&amp;torrentid=115362">
          <span class="TorrentTitle">
            <span class="TorrentTitle-item resolution">1080p</span>
            <span class="TorrentTitle-item is-releaseGroup">Example-GROUP</span>
          </span>
        </a>
      </div>
    </td>
    <td class="Table-cell TableTorrent-cellStat TableTorrent-cellStatTime">
      <span title="Aug 24 2026, 22:51">today</span>
    </td>
    <td class="Table-cell TableTorrent-cellStat TableTorrent-cellStatSize">1.50 GiB</td>
    <td class="Table-cell TableTorrent-cellStat TableTorrent-cellStatSnatches">12</td>
    <td class="Table-cell TableTorrent-cellStat TableTorrent-cellStatSeeders u-colorRatio00">1,234</td>
    <td class="Table-cell TableTorrent-cellStat TableTorrent-cellStatLeechers">2</td>
  </tr>
</tbody></table>
</body></html>
"""


def _gpw_indexer() -> dict:
    """构造与站点资源一致的 GPW 搜索配置。"""
    detail_selector = 'a[href*="torrents.php?id="][href*="torrentid="]'
    return {
        "id": "greatposterwall",
        "name": "海豹",
        "domain": "https://greatposterwall.com/",
        "search": {
            "paths": [
                {"path": "torrents.php?searchstr={keyword}", "method": "get"}
            ]
        },
        "torrents": {
            "list": {
                "selector": "table#torrent_table > tbody > tr.TableTorrent-rowTitle"
            },
            "fields": {
                "id": {
                    "selector": detail_selector,
                    "attribute": "href",
                    "filters": [
                        {"name": "re_search", "args": [r"torrentid=(\d+)", 1]}
                    ],
                },
                "title_default": {"selector": f"{detail_selector} > span.TorrentTitle"},
                "title_optional": {
                    "selector": detail_selector,
                    "attribute": "data-tooltip",
                    "optional": True,
                },
                "title": {
                    "text": "{% if fields['title_optional'] %}{{ fields['title_optional'] }}"
                            "{% else %}{{ fields['title_default'] }}{% endif %}"
                },
                "details": {"selector": detail_selector, "attribute": "href"},
                "download": {
                    "selector": 'a[href*="torrents.php?action=download"]',
                    "attribute": "href",
                },
                "size": {"selector": "td.TableTorrent-cellStatSize"},
                "seeders": {"selector": "td.TableTorrent-cellStatSeeders"},
                "leechers": {"selector": "td.TableTorrent-cellStatLeechers"},
                "grabs": {"selector": "td.TableTorrent-cellStatSnatches"},
                "date_elapsed": {
                    "selector": "td.TableTorrent-cellStatTime > span",
                    "attribute": "title",
                    "optional": True,
                },
                "date": {
                    "text": "{% if fields['date_elapsed'] %}{{ fields['date_elapsed'] }}{% else %}now{% endif %}",
                    "filters": [{"name": "dateparse", "args": "%b %d %Y, %H:%M"}],
                },
            },
        },
    }


def _build_parser() -> GazelleSiteUserInfo:
    """构造 Gazelle 用户数据解析器测试实例。"""
    return GazelleSiteUserInfo(
        site_name="海豹",
        url="https://greatposterwall.com/",
        site_cookie="",
        apikey=None,
        token=None,
    )


def test_greatposterwall_python_search_prefers_complete_filename(monkeypatch):
    """GPW Python 解析应优先使用详情链接携带的完整文件名。"""
    monkeypatch.setattr(
        "app.modules.indexer.spider.rust_accel.parse_indexer_torrents",
        lambda **_: None,
    )

    results = SiteSpider(_gpw_indexer(), keyword="Example").parse(GPW_HTML)

    assert len(results) == 1
    assert results[0]["title"] == "Nobody.2025.1080p.WEB-DL.H.265-Example-GROUP.mkv"
    assert results[0]["page_url"].endswith("torrents.php?id=8064&torrentid=115362")
    assert results[0]["enclosure"].endswith("torrents.php?action=download&id=115362")
    assert results[0]["size"] == 1610612736
    assert results[0]["seeders"] == 1234
    assert results[0]["peers"] == 2
    assert results[0]["grabs"] == 12
    assert results[0]["pubdate"] == "2026-08-24 22:51:00"


def test_greatposterwall_rust_search_prefers_complete_filename(monkeypatch):
    """GPW Rust 解析应使用与 Python 相同的完整文件名配置。"""
    monkeypatch.setattr(rust_accel, "is_enabled", lambda: True)

    def fail_python_fallback(*_args, **_kwargs):
        """Rust 返回空值时阻止测试静默落入 Python 路径。"""
        raise AssertionError("GPW Rust 解析不应回退 Python")

    monkeypatch.setattr(SiteSpider, "get_info", fail_python_fallback)

    results = SiteSpider(_gpw_indexer(), keyword="Nobody").parse(GPW_HTML)

    assert results[0]["title"] == "Nobody.2025.1080p.WEB-DL.H.265-Example-GROUP.mkv"


def test_greatposterwall_search_falls_back_to_visible_title(monkeypatch):
    """详情链接缺少完整文件名时应保留可见规格标题作为回退。"""
    monkeypatch.setattr(
        "app.modules.indexer.spider.rust_accel.parse_indexer_torrents",
        lambda **_: None,
    )
    html_without_tooltip = GPW_HTML.replace(
        ' data-tooltip="Nobody.2025.1080p.WEB-DL.H.265-Example-GROUP.mkv"',
        "",
    )

    results = SiteSpider(_gpw_indexer(), keyword="Nobody").parse(html_without_tooltip)

    assert results[0]["title"] == "1080p Example-GROUP"


def test_greatposterwall_seeding_uses_semantic_stat_cells():
    """GPW 标题列使用 colspan 时，做种统计仍应按语义 class 正确配对。"""
    parser = _build_parser()

    next_page = parser._parse_user_torrent_seeding_info(GPW_HTML)

    assert next_page is None
    assert parser.seeding == 1
    assert parser.seeding_size == 1610612736
    assert parser.seeding_info == [[1234, 1610612736]]


def test_gazelle_seeding_keeps_legacy_column_fallback():
    """没有语义 class 的传统 Gazelle 表格仍应按既有列号统计做种。"""
    parser = _build_parser()
    html_text = """
    <table id="torrent_table">
      <tr><td>Name</td><td>Type</td><td>Time</td><td>Size</td><td>Snatches</td><td>Seeders</td><td>Leechers</td></tr>
      <tr><td>Example</td><td>Movie</td><td>today</td><td>2 GiB</td><td>3</td><td>45</td><td>1</td></tr>
    </table>
    """

    parser._parse_user_torrent_seeding_info(html_text)

    assert parser.seeding == 1
    assert parser.seeding_size == 2147483648
    assert parser.seeding_info == [[45, 2147483648]]
