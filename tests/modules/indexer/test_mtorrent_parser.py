"""
验证 MTorrent 用户资料的站点语义与做种条目容错。
"""

# -*- coding: utf-8 -*-
import json

import pytest

from app.modules.indexer import IndexerModule
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


@pytest.mark.parametrize(
    ("role_fields", "expected_level"),
    [
        ({"role": "13", "roleName": "工作组-组长"}, "工作组-组长"),
        ({"role": "13"}, "Moderator"),
        ({"role": "13", "roleName": ""}, "Moderator"),
        ({"role": "13", "roleName": None}, "Moderator"),
        ({}, "User"),
    ],
)
def test_mtorrent_profile_preserves_site_role_name(role_fields, expected_level):
    """
    优先使用站点等级名，未提供名称时兼容原有角色表及默认等级。
    """
    parser = _build_parser()
    parser._parse_user_base_info(json.dumps({"code": "0", "data": role_fields}))

    assert parser.user_level == expected_level


@pytest.mark.parametrize(
    ("ratio_fields", "expected_ratio"),
    [
        ({}, None),
        ({"shareRate": None}, None),
        ({"shareRate": 0}, 0),
        ({"shareRate": 1.23}, 1.23),
        ({"shareRate": "2.5"}, 2.5),
    ],
)
def test_mtorrent_profile_ratio_survives_userdata_projection(monkeypatch, ratio_fields, expected_ratio):
    """
    通过真实解析器和用户数据模型验证空分享率、真实零值与数值的投影及 JSON 序列化。
    """
    profile = {
        "code": "0",
        "data": {
            "id": 1001,
            "username": "site-user",
            "role": "13",
            "roleName": "工作组-组长",
            "memberCount": {"uploaded": "1099511627776", "downloaded": "0", **ratio_fields},
        },
    }

    def parse_profile(parser):
        """
        回放脱敏资料响应，避免真实网络请求。
        """
        parser._parse_user_base_info(json.dumps(profile))

    monkeypatch.setattr(MTorrentSiteUserInfo, "parse", parse_profile)
    monkeypatch.setattr(IndexerModule, "_site_schemas", [MTorrentSiteUserInfo])
    module = object.__new__(IndexerModule)
    result = module.refresh_userdata({
        "name": "MTorrent", "url": "https://example.com/", "schema": "MTorrent", "public": False,
    })

    assert result.ratio == expected_ratio
    assert result.download == 0
    assert result.user_level == "工作组-组长"
    assert json.loads(result.model_dump_json())["ratio"] == expected_ratio


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
