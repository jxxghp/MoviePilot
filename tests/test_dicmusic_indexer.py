from typing import Any, NoReturn
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from app.adapters.system import rust as rust_accel
from app.modules.indexer.spider import SiteSpider
from app.schemas.types import MediaType

DICMUSIC_HTML = """
<html><body>
<table class="torrent_table" id="torrent_table"><tbody>
  <tr class="torrent_checked torrent">
    <td class="td_info" colspan="3">
      <span class="torrent_links_block">
        <a href="/torrents.php?action=download&amp;id=456">DL</a>
        <a href="/torrents.php?action=usetoken&amp;id=456">FL</a>
        <a href="/reportsv2.php?action=report&amp;id=456">RP</a>
      </span>
      <a href="/artist.php?id=789">BTS (방탄소년단 / 防弹少年团)</a> -
      <a href="/torrents.php?id=123&amp;torrentid=456">ARIRANG</a>
      [2026] [专辑]
      <div class="tags">K-Pop</div>
    </td>
    <td class="td_time nobr"><span title="Aug 26 2026, 08:43">today</span></td>
    <td class="td_size number_column nobr">1.00 GiB</td>
    <td class="td_snatched number_column m_td_right">24</td>
    <td class="td_seeders number_column m_td_right">8</td>
    <td class="td_leechers number_column m_td_right">2</td>
  </tr>
</tbody></table>
</body></html>
"""


def _dicmusic_indexer() -> dict[str, Any]:
    """构造与 DIC Music 资源配置一致的非分组 Gazelle 索引器。"""
    detail_selector = 'a[href*="torrents.php?id="][href*="torrentid="]'
    return {
        "id": "dicmusic",
        "name": "海豚",
        "domain": "https://dicmusic.com/",
        "media_type": "music",
        "schema": "Gazelle",
        "search": {
            "paths": [{"path": "torrents.php", "method": "get"}],
            "params": {
                "searchsubmit": 1,
                "group_results": 0,
                "searchstr": "{keyword}",
            },
        },
        "browse": {
            "path": "torrents.php?searchsubmit=1&group_results=0&page={page}",
        },
        "torrents": {
            "list": {"selector": "table#torrent_table > tbody > tr.torrent"},
            "fields": {
                "id": {
                    "selector": detail_selector,
                    "attribute": "href",
                    "filters": [
                        {"name": "re_search", "args": [r"torrentid=(\d+)", 1]}
                    ],
                },
                "title": {
                    "selector": "td.td_info",
                    "remove": "span, div.tags",
                },
                "details": {"selector": detail_selector, "attribute": "href"},
                "download": {
                    "selector": 'a[href*="torrents.php?action=download"]',
                    "attribute": "href",
                },
                "size": {"selector": "td.td_size"},
                "seeders": {"selector": "td.td_seeders"},
                "leechers": {"selector": "td.td_leechers"},
                "grabs": {"selector": "td.td_snatched"},
                "date_elapsed": {
                    "selector": "td.td_time > span",
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


def test_dicmusic_browse_requests_ungrouped_torrent_rows() -> None:
    """DIC Music 无关键词浏览也应显式关闭 Gazelle 结果分组。"""
    spider = SiteSpider(_dicmusic_indexer(), page=2)

    browse_url = spider._SiteSpider__get_search_url()
    parsed_url = urlparse(browse_url)
    query = parse_qs(parsed_url.query)

    assert parsed_url.path == "/torrents.php"
    assert query == {
        "searchsubmit": ["1"],
        "group_results": ["0"],
        "page": ["2"],
    }


def test_dicmusic_search_uses_complete_music_title_in_python_fallback() -> None:
    """DIC Music Python 兜底解析应从非分组行保留完整音乐名。"""
    with patch(
        "app.modules.indexer.spider.rust_accel.parse_indexer_torrents",
        return_value=None,
    ):
        results = SiteSpider(
            _dicmusic_indexer(), keyword="ARIRANG", mtype=MediaType.MUSIC
        ).parse(DICMUSIC_HTML)

    assert len(results) == 1
    assert results[0]["title"] == (
        "BTS (방탄소년단 / 防弹少年团) - ARIRANG [2026] [专辑]"
    )
    assert results[0]["category"] == MediaType.MUSIC.value
    assert results[0]["page_url"].endswith("torrents.php?id=123&torrentid=456")
    assert results[0]["enclosure"].endswith("torrents.php?action=download&id=456")


def test_dicmusic_search_uses_complete_music_title_in_rust_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIC Music Rust 解析应与 Python 解析共享完整音乐名。"""
    if not rust_accel.is_available():
        pytest.skip("moviepilot_rust 扩展未安装")

    monkeypatch.setattr(rust_accel, "is_enabled", lambda: True)

    def fail_python_fallback(*_args: object, **_kwargs: object) -> NoReturn:
        """Rust 解析异常回退时让测试显式失败。"""
        raise AssertionError("DIC Music Rust 解析不应回退 Python")

    monkeypatch.setattr(SiteSpider, "get_info", fail_python_fallback)

    results = SiteSpider(
        _dicmusic_indexer(), keyword="ARIRANG", mtype=MediaType.MUSIC
    ).parse(DICMUSIC_HTML)

    assert len(results) == 1
    assert results[0]["title"] == (
        "BTS (방탄소년단 / 防弹少年团) - ARIRANG [2026] [专辑]"
    )
    assert results[0]["category"] == MediaType.MUSIC.value
