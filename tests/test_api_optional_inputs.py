"""
验证 API 可选参数及缺失业务元数据的边界行为，保持旧直接调用兼容。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api.endpoints import bangumi, download, history, mediaserver, message, search, site, tmdb, transfer
from app.domain.context import MediaInfo
from app.schemas.context import MediaInfo as MediaInfoInput
from app.schemas.context import SubtitleInfo
from app.schemas.file import FileItem
from app.schemas.transfer import ManualTransferItem
from app.schemas.types import MediaType


@pytest.mark.parametrize("endpoint_name", ["bangumi_credits", "bangumi_recommend", "bangumi_person_credits"])
@pytest.mark.parametrize(("page", "count", "expected"), [(None, None, slice(0, 20)), (2, 3, slice(3, 6))])
def test_bangumi_optional_pagination(endpoint_name, page, count, expected, monkeypatch):
    """
    演职员与推荐列表的空分页参数使用默认页，显式分页仍按原顺序截取。
    """
    values = [MediaInfo(title=f"作品 {index}") for index in range(25)]
    chain = SimpleNamespace(
        async_bangumi_credits=AsyncMock(return_value=values),
        async_bangumi_recommend=AsyncMock(return_value=values),
        async_person_credits=AsyncMock(return_value=values),
    )
    monkeypatch.setattr(bangumi, "BangumiChain", Mock(return_value=chain))
    result = asyncio.run(getattr(bangumi, endpoint_name)(1, page=page, count=count, _=None))
    selected = values[expected]
    assert result == (selected if endpoint_name == "bangumi_credits" else [value.to_dict() for value in selected])


def test_tmdb_collection_defaults_optional_pagination(monkeypatch):
    """
    合集的空分页值回退第一页，响应继续使用原有字典投影。
    """
    values = [MediaInfo(title=f"电影 {index}") for index in range(25)]
    chain = SimpleNamespace(async_tmdb_collection=AsyncMock(return_value=values))
    monkeypatch.setattr(tmdb, "TmdbChain", Mock(return_value=chain))
    result = asyncio.run(tmdb.tmdb_collection(1, page=None, count=None, _=None))
    assert result == [value.to_dict() for value in values[:20]]


def test_history_and_web_message_optional_pagination():
    """
    旧调用显式传 None 时仍向查询服务提供有效分页整数。
    """
    query = SimpleNamespace(list_transfer=AsyncMock(return_value={"list": [], "total": 0}))
    response = asyncio.run(history.transfer_history(page=None, count=None, query=query, _=None))
    query.list_transfer.assert_awaited_once_with(title=None, page=1, count=30, status=None)
    assert response.success is True
    messages = SimpleNamespace(list_web=AsyncMock(return_value=[]))
    assert asyncio.run(message.get_web_message(service=messages, page=None, count=None, _=None)) == []
    messages.list_web.assert_awaited_once_with(page=1, count=20)


@pytest.mark.parametrize("season", [None, 2])
def test_mediaserver_empty_seasons_are_a_valid_empty_result(season, monkeypatch):
    """
    已存在媒体但缺少季集合时返回空集合或指定季空列表，不抛属性错误。
    """
    chain = SimpleNamespace(media_exists=Mock(return_value=SimpleNamespace(seasons=None)))
    monkeypatch.setattr(mediaserver, "MediaServerChain", Mock(return_value=chain))
    result = mediaserver.exists(MediaInfoInput(type=MediaType.TV, title="剧集", season=season), _=None)
    assert result == ({} if season is None else {season: []})


def test_subtitle_without_download_url_stops_before_signature_or_site_lookup():
    """
    缺少下载链接的字幕直接返回可读错误，避免把 None 交给签名校验。
    """
    subtitle = SubtitleInfo(site=1, title="empty.srt")
    assert download._prepare_subtitle_download(subtitle) == (False, "字幕下载链接为空")


@pytest.mark.parametrize("site_id", [None, "1", {}])
def test_subtitle_with_invalid_site_id_cannot_leave_unsigned_url(site_id):
    """
    有链接但没有整数站点 ID 的字幕不能生成不受站点用途约束的下载链接。
    """
    with pytest.raises(ValueError, match="站点 ID"):
        search._serialize_signed_subtitle_result({"site": site_id, "enclosure": "https://example.test/sub.srt"})


@pytest.mark.parametrize("endpoint_name", ["refresh_userdata", "test_site", "site_icon", "site_category", "site_resource"])
def test_site_without_domain_never_reaches_indexer(endpoint_name, monkeypatch):
    """
    已保存但未配置域名的站点返回业务错误，不能被索引器当成全站查询。
    """
    configured_site = SimpleNamespace(domain=None)
    query = SimpleNamespace(get_sync=Mock(return_value=configured_site), get=AsyncMock(return_value=configured_site))
    helper = Mock(side_effect=AssertionError("缺少域名不应调用索引器"))
    monkeypatch.setattr(site, "SitesHelper", helper)
    endpoint = getattr(site, endpoint_name)
    if endpoint_name in {"site_category", "site_resource"}:
        with pytest.raises(HTTPException) as error:
            asyncio.run(endpoint(1, query=query, _=None))
        assert error.value.status_code == 400
        assert error.value.detail == "站点域名未配置"
    else:
        response = endpoint(1, query=query, _=None)
        if endpoint_name == "site_icon":
            response = asyncio.run(response)
        assert response.success is False
        assert response.message == "站点域名未配置"
    helper.assert_not_called()


@pytest.mark.parametrize("category", [None, [], {"movie": [{"id": 1}], "other": [{"id": 1}, {"id": 2}]}])
def test_site_category_only_projects_category_mappings(category, monkeypatch):
    """
    分类配置缺失或格式错误返回空列表，合法配置去重后保持顺序。
    """
    query = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(domain="https://example.test")))
    helper = SimpleNamespace(async_get_indexer=AsyncMock(return_value={"category": category}))
    monkeypatch.setattr(site, "SitesHelper", Mock(return_value=helper))
    result = asyncio.run(site.site_category(1, query=query, _=None))
    assert result == ([{"id": 1}, {"id": 2}] if isinstance(category, dict) else [])


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("moved", [False, True])
def test_manual_transfer_history_without_file_snapshot_returns_business_error(batch, moved):
    """
    历史记录缺少来源或移动后目标快照时拒绝重做，并指出具体记录 ID。
    """
    query = SimpleNamespace(get=Mock(return_value=SimpleNamespace(
        status=moved, mode="move" if moved else None, src_fileitem=None, dest_fileitem=None,
    )))
    request = ManualTransferItem(**({"logids": [7]} if batch else {"logid": 7}))
    items, error = transfer._resolve_manual_transfer_source_fileitems(request, query)
    assert items == []
    assert error == "整理记录缺少文件项，ID：7"


@pytest.mark.parametrize("batch", [False, True])
def test_manual_transfer_history_after_move_uses_destination_snapshot(batch):
    """
    成功移动的历史重做使用目标快照，避免重新访问已经搬走的源文件。
    """
    payload = {"storage": "local", "type": "file", "path": "/library/movie.mkv"}
    query = SimpleNamespace(get=Mock(return_value=SimpleNamespace(
        status=True, mode="move", src_fileitem=None, dest_fileitem=payload,
    )))
    request = ManualTransferItem(**({"logids": [7]} if batch else {"logid": 7}))
    items, error = transfer._resolve_manual_transfer_source_fileitems(request, query)
    assert error is None
    assert [item.path for item in items] == ["/library/movie.mkv"]


def test_manual_target_path_skips_source_without_path(monkeypatch):
    """
    缺少源路径的项被去重阶段过滤，不把当前工作目录用作整理路径匹配条件。
    """
    request = ManualTransferItem(fileitem=FileItem(storage="local", type="file", path=None))
    helper = Mock(side_effect=AssertionError("缺失路径的项不应匹配目录"))
    monkeypatch.setattr(transfer, "DirectoryHelper", helper)
    response = transfer.match_manual_transfer_target_path(request, history_query=Mock(), _=None)
    assert response.success is True
    assert response.data["target_path"] is None
    helper.assert_not_called()
