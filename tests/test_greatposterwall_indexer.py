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
        <a href="/torrents.php?id=8064&amp;torrentid=115362">
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
                "title": {"selector": f"{detail_selector} > span.TorrentTitle"},
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


def test_greatposterwall_search_parses_tabletorrent_row(monkeypatch):
    """GPW 搜索结果应从 TableTorrent 行提取完整种子信息。"""
    monkeypatch.setattr(
        "app.modules.indexer.spider.rust_accel.parse_indexer_torrents",
        lambda **_: None,
    )

    results = SiteSpider(_gpw_indexer(), keyword="Example").parse(GPW_HTML)

    assert len(results) == 1
    assert results[0]["title"] == "1080p Example-GROUP"
    assert results[0]["page_url"].endswith("torrents.php?id=8064&torrentid=115362")
    assert results[0]["enclosure"].endswith("torrents.php?action=download&id=115362")
    assert results[0]["size"] == 1610612736
    assert results[0]["seeders"] == 1234
    assert results[0]["peers"] == 2
    assert results[0]["grabs"] == 12
    assert results[0]["pubdate"] == "2026-08-24 22:51:00"


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
