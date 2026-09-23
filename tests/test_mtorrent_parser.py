# -*- coding: utf-8 -*-
import json

from app.modules.indexer.parser.mtorrent import MTorrentSiteUserInfo


def _build_parser() -> MTorrentSiteUserInfo:
    """
    构造 MTorrent 解析器测试实例。
    """
    return MTorrentSiteUserInfo(
        site_name="MTorrent",
        url="https://example.com/",
        site_cookie="",
        apikey="apikey",
        token=None,
    )


def test_mtorrent_seeding_skips_invalid_torrent_items(monkeypatch):
    """
    MTorrent 返回空种子对象时应跳过异常条目并只统计有效做种。
    """
    parser = _build_parser()
    parser.userid = "1"
    parser._torrent_seeding_params = {
        "pageNumber": 1,
        "pageSize": 200,
        "type": "SEEDING",
        "userid": parser.userid,
    }

    monkeypatch.setattr(
        parser,
        "_get_page_content",
        lambda **_: json.dumps({"data": {"seeder": 1}}),
    )
    html_text = json.dumps(
        {
            "code": "0",
            "data": {
                "data": [
                    {"torrent": None},
                    {"torrent": "invalid"},
                    None,
                    {"torrent": {"size": "1024", "source": "3"}},
                ]
            },
        }
    )

    next_page = parser._parse_user_torrent_seeding_info(html_text)

    assert next_page is None
    assert parser.seeding == 1
    assert parser.seeding_size == 1024
    assert parser.seeding_info == [[3, 1024]]
