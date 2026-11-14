"""
片单接口必须从 JSON 字典读取创建结果，并保留双入口一致性。
"""

import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.modules.themoviedb.tmdbv3api.exceptions import TMDbException
from app.modules.themoviedb.tmdbv3api.objs.list import List


@pytest.mark.parametrize("asynchronous", [False, True])
def test_list_creation_reads_id_and_preserves_false_item_status(monkeypatch, asynchronous):
    """
    创建片单读取 list_id；电影尚未加入时原样返回 False。
    """
    request = (AsyncMock if asynchronous else Mock)(side_effect=[
        {"success": True, "list_id": 735}, {"item_present": False},
    ])
    monkeypatch.setattr(List, "_async_request_obj" if asynchronous else "_request_obj", request)
    client = List(language="zh-CN")
    client.session_id = "session"
    try:
        if asynchronous:
            created = asyncio.run(client.async_create_list("收藏", "说明"))
            present = asyncio.run(client.async_check_item_status(735, 12))
        else:
            created = client.create_list("收藏", "说明")
            present = client.check_item_status(735, 12)
        assert created == 735
        assert present is False
        assert request.call_args_list == [
            call("/list", params="session_id=session", method="POST", json={
                "name": "收藏", "description": "说明", "language": "zh-CN",
            }),
            call("/list/735/item_status", params="movie_id=12"),
        ]
    finally:
        client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("payload,error", [({}, KeyError), ([], TMDbException), (None, TMDbException)])
def test_list_status_rejects_missing_required_data(monkeypatch, asynchronous, payload, error):
    """
    缺失状态字段仍抛 KeyError，非对象响应不能冒充有效片单状态。
    """
    request = (AsyncMock if asynchronous else Mock)(return_value=payload)
    monkeypatch.setattr(List, "_async_request_obj" if asynchronous else "_request_obj", request)
    client = List()
    try:
        with pytest.raises(error):
            if asynchronous:
                asyncio.run(client.async_check_item_status(735, 12))
            else:
                client.check_item_status(735, 12)
    finally:
        client.close()
