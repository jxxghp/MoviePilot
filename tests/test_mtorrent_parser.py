# -*- coding: utf-8 -*-
import json

import pytest

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


def test_mtorrent_seeding_skips_invalid_torrent_items():
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


def test_mtorrent_seeding_paginates_from_list_metadata(monkeypatch):
    """应按做种列表的分页元数据读取全部种子，不依赖 tracker 私有接口。"""
    parser = _build_parser()
    parser.userid = "1"
    parser._torrent_seeding_params = {
        "pageNumber": 1,
        "pageSize": 2,
        "type": "SEEDING",
        "userid": parser.userid,
    }
    monkeypatch.setattr(
        parser,
        "_get_page_content",
        lambda **_: pytest.fail("解析做种列表时不应请求 tracker 私有接口"),
    )

    first_page = json.dumps(
        {
            "code": "0",
            "data": {
                "total": 3,
                "totalPages": 2,
                "data": [
                    {"torrent": {"size": "1024", "source": "3"}},
                    {"torrent": {"size": "2048", "source": "4"}},
                ],
            },
        }
    )
    second_page = json.dumps(
        {
            "code": "0",
            "data": {
                "total": 3,
                "totalPages": 2,
                "data": [{"torrent": {"size": "4096", "source": "5"}}],
            },
        }
    )

    assert parser._parse_user_torrent_seeding_info(first_page) == ""
    assert parser._torrent_seeding_params["pageNumber"] == 2
    assert parser._parse_user_torrent_seeding_info(second_page) is None
    assert parser.seeding == 3
    assert parser.seeding_size == 7168
    assert parser.seeding_info == [[3, 1024], [4, 2048], [5, 4096]]


def test_mtorrent_seeding_uses_page_size_when_totals_are_missing():
    """接口未返回总页数时，满页后继续读取并在空页结束。"""
    parser = _build_parser()
    parser._torrent_seeding_params = {"pageNumber": 1, "pageSize": 2}
    full_page = json.dumps(
        {
            "code": "0",
            "data": {
                "data": [
                    {"torrent": {"size": "1024", "source": "3"}},
                    {"torrent": {"size": "2048", "source": "4"}},
                ]
            },
        }
    )
    empty_page = json.dumps({"code": "0", "data": {"data": []}})

    assert parser._parse_user_torrent_seeding_info(full_page) == ""
    assert parser._torrent_seeding_params["pageNumber"] == 2
    assert parser._parse_user_torrent_seeding_info(empty_page) is None
