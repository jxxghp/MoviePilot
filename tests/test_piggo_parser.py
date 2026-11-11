"""
猪猪实验室资料完整性、分页失败与禁止页面回退的离线回归。
"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from requests import Response

import app.modules.indexer.parser.piggo as piggo
from app.chain.site import SiteChain
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo

# 仅使用合成账号与合成令牌，容量样例来自公开接口文档。
PROFILE = {"user": {"id": 42, "username": "synthetic-user", "enabled": True,
                    "class": {"id": 8, "name": "主管"}, "stats": {"uploaded": "2.50 GB", "downloaded": "1.00 GB", "bonus": 12.5}}}


def _response(data, status=200, code=0):
    response = Response()
    response.status_code = status
    response._content = json.dumps({"code": code, "data": data, "msg": "ok"}).encode()
    response.encoding = "utf-8"
    response.close = Mock()
    return response


def _page(kind, items=None, page=1, total=None, per_page=100):
    items = [] if items is None else items
    total = len(items) if total is None else total
    return {"kind": kind, "items": items, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page), "total": total}


@pytest.fixture
def boundary(monkeypatch):
    """
    替换资料传输和等待边界，所有错误仍经过真实信封和分页校验。
    """
    monkeypatch.setattr(piggo, "site_request_config", lambda _url: {"id": 7274, "domain": "piggo.me"})
    monkeypatch.setattr(piggo, "wait_for_site_request", Mock())
    monkeypatch.setattr(piggo, "get_runtime_setting", lambda _key: {"https": "proxy"})
    request = Mock()
    request.post_res.side_effect = [_response(PROFILE), _response(_page("seeding")), _response(_page("leeching"))]
    parser = piggo.PiggoSiteUserInfo(
        site_name="猪猪", url="https://piggo.me/", site_cookie="unused-cookie", apikey=" synthetic-key ",
        token="unused-token", request_utils=request, ua="Piggo-Test", proxy=True, emulate=True,
    )
    return parser, request


def test_complete_profile_and_all_torrent_pages_are_published(boundary):
    """
    做种和下载的末页全部成功后才公布统计，文本容量转换为字节，分享率按流量计算。
    """
    parser, request = boundary
    seeding = [{"id": 1, "size": 1024, "seeders": 4}, {"id": 2, "size": 2048, "seeders": 7}]
    leeching = [{"id": 3, "size": 8192, "seeders": 2}, {"id": 4, "size": 4096, "seeders": 1}]
    responses = [_response(PROFILE),
                 _response(_page("seeding", seeding[:1], total=2, per_page=1)),
                 _response(_page("seeding", seeding[1:], page=2, total=2, per_page=1)),
                 _response(_page("leeching", leeching[:1], total=2, per_page=1)),
                 _response(_page("leeching", leeching[1:], page=2, total=2, per_page=1))]
    request.post_res.side_effect = responses
    parser.parse()
    assert parser.err_msg is None
    assert (parser.userid, parser.username, parser.user_level) == (42, "synthetic-user", "主管")
    assert (parser.upload, parser.download, parser.ratio, parser.bonus) == (2684354560, 1073741824, 2.5, 12.5)
    assert (parser.seeding, parser.seeding_size, parser.seeding_info) == (2, 3072, [[4, 1024], [7, 2048]])
    assert (parser.leeching, parser.leeching_size) == (2, 12288)
    assert parser.join_at is None and parser.message_unread == 0
    bodies = [call.kwargs["json"] for call in request.post_res.call_args_list]
    assert bodies == [{}, {"kind": "seeding", "page": 1, "per_page": 100}, {"kind": "seeding", "page": 2, "per_page": 100},
                      {"kind": "leeching", "page": 1, "per_page": 100}, {"kind": "leeching", "page": 2, "per_page": 100}]
    for call in request.post_res.call_args_list:
        assert call.kwargs["url"].startswith("https://api.piggo.me/v1/api/me")
        assert call.kwargs["headers"]["Authorization"] == "Bearer synthetic-key"
        assert call.kwargs["cookies"] is None and call.kwargs["allow_redirects"] is False
    request.close.assert_called_once()
    for response in responses:
        response.close.assert_called_once()


@pytest.mark.parametrize("part", ["uploaded", "downloaded"])
def test_missing_traffic_fails_without_partial_identity(boundary, part):
    """
    缺少流量字段不能保存为零或让 KeyError 逃出资料入口。
    """
    parser, request = boundary
    profile = deepcopy(PROFILE)
    del profile["user"]["stats"][part]
    request.post_res.side_effect = [_response(profile)]
    parser.parse()
    assert parser.userid is None and parser.err_msg
    assert parser.upload == parser.download == parser.seeding == 0
    request.post_res.assert_called_once()
    request.close.assert_called_once()


@pytest.mark.parametrize("change", [{"page": 1}, {"total": 3}, {"kind": "leeching"}, {"items": []},
                                    {"items": [{"id": 1, "size": 1024, "seeders": 2}]}])
def test_changed_or_incomplete_second_page_discards_entire_refresh(boundary, change):
    """
    错页、变化的总数、错类、缺条目或重复 ID 都不能留下半份统计。
    """
    parser, request = boundary
    first = _page("seeding", [{"id": 1, "size": 1024, "seeders": 2}], total=2, per_page=1)
    second = _page("seeding", [{"id": 2, "size": 2048, "seeders": 3}], page=2, total=2, per_page=1)
    second.update(change)
    request.post_res.side_effect = [_response(PROFILE), _response(first), _response(second)]
    parser.parse()
    assert parser.userid is None and "分页" in parser.err_msg
    assert parser.upload == parser.download == parser.seeding == parser.seeding_size == parser.leeching == 0
    assert parser.seeding_info == []
    request.close.assert_called_once()


def test_last_leeching_page_failure_does_not_save_valid_seeding(boundary):
    """
    下载列表失败也必须拒绝整个统计快照，不能仅保存已成功的做种数据。
    """
    parser, request = boundary
    request.post_res.side_effect = [_response(PROFILE), _response(_page("seeding", [{"id": 1, "size": 1024, "seeders": 2}])),
                                    _response({}, status=401, code=401)]
    parser.parse()
    assert parser.userid is None and "令牌" in parser.err_msg
    assert parser.seeding == parser.upload == 0


def test_failed_api_parser_never_falls_back_or_persists(boundary, monkeypatch):
    """
    资料 API 失败后禁止探测 PHP 页面，SiteChain 不写入空白或半份用户数据。
    """
    parser, request = boundary
    parser.apikey = None
    parser.token = None
    constructor = Mock(return_value=parser)
    constructor.schema = piggo.SiteSchema.Piggo
    module = object.__new__(IndexerModule)
    module._site_schemas = [constructor, NexusPhpSiteUserInfo]
    fallback = Mock(side_effect=AssertionError("猪猪 API 失败不能回退页面"))
    monkeypatch.setattr(NexusPhpSiteUserInfo, "parse", fallback)
    site = {"name": "猪猪", "url": "https://piggo.me/", "schema": "Piggo", "public": False}
    userdata = module.refresh_userdata(site)
    assert userdata.userid is None and "长期存取令牌" in userdata.err_msg
    fallback.assert_not_called()
    request.post_res.assert_not_called()
    chain = object.__new__(SiteChain)
    chain.run_module = Mock(return_value=userdata)
    chain.site_repository = Mock()
    assert chain.refresh_userdata(site) is userdata
    chain.site_repository.update_userdata.assert_not_called()


@pytest.mark.parametrize("field,value", [("id", True), ("id", -1), ("username", 42), ("username", " "),
                                         ("enabled", False), ("stats", []), ("class", {}), ("class", []),
                                         ("stats", {"uploaded": "invalid", "downloaded": 0}),
                                         ("stats", {"uploaded": 0, "downloaded": 0, "bonus": "nan"}),
                                         ("stats", {"uploaded": 0, "downloaded": 0, "bonus": []}),
                                         ("stats", {"uploaded": 0, "downloaded": 0, "bonus": {"invalid": 1}})])
def test_invalid_profile_is_not_saved_as_valid_user(boundary, field, value):
    """
    外部资料的身份、等级、容量和有限魔力值必须有效，不传播原始账户字段。
    """
    parser, request = boundary
    profile = deepcopy(PROFILE)
    profile["user"][field] = value
    request.post_res.side_effect = [_response(profile)]
    parser.parse()
    assert parser.userid is None and parser.err_msg
    assert parser.seeding_info == [] and parser.upload == 0


@pytest.mark.parametrize("pages", [0, 1])
def test_empty_torrents_and_zero_download_are_valid(boundary, pages):
    """
    空做种、空下载列表不是失败；零下载量的分享率沿用零值合同。
    """
    parser, request = boundary
    profile = deepcopy(PROFILE)
    profile["user"]["stats"]["downloaded"] = 0
    request.post_res.side_effect = [_response(profile), _response(dict(_page("seeding"), pages=pages)),
                                    _response(dict(_page("leeching"), pages=pages))]
    parser.parse()
    assert parser.userid == 42 and parser.err_msg is None
    assert parser.ratio == parser.seeding == parser.leeching == 0


@pytest.mark.parametrize("status,code", [(429, 429), (403, 0), (200, 422)])
def test_profile_api_errors_are_explicit_and_responses_close(boundary, monkeypatch, status, code):
    """
    用户 API 的内层公告和限流失败均能展示原因，不进入种子分页或保存统计。
    """
    parser, request = boundary
    response = _response({"code": "notice_unread", "notice_gate": {"title": "公告"}} if status == 403 else {}, status, code)
    request.post_res.side_effect = [response]
    defer = Mock()
    monkeypatch.setattr(piggo, "defer_site_request", defer)
    parser.parse()
    assert parser.userid is None and parser.err_msg
    assert defer.called is (status == 429)
    request.post_res.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.parametrize("change", [{"pages": 9}, {"per_page": 101}, {"per_page": 0}, {"total": -1}, {"page": 0},
                                    {"items": None}, {"items": [{}]}, {"items": [None]},
                                    {"items": [{"id": 1, "size": -1}]}, {"items": [{"id": 1, "size": 1, "seeders": "invalid"}]}])
def test_malformed_torrent_page_discards_profile(boundary, change):
    """
    无效元数据、错类型或坏整数不能被视为完整空列表。
    """
    parser, request = boundary
    data = _page("seeding", [{"id": 1, "size": 1, "seeders": 1}])
    data.update(change)
    request.post_res.side_effect = [_response(PROFILE), _response(data)]
    parser.parse()
    assert parser.userid is None and "分页" in parser.err_msg
