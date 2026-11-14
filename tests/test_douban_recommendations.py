"""
豆瓣推荐从 HTTP 响应到媒体投影的同步异步回归。
"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.douban import DoubanModule
from app.modules.douban.apiv2 import DoubanApi


@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("shape", ["list", "mapping", "empty_list", "empty_mapping", "missing"])
def test_recommendations_preserve_native_lists_and_compatible_mappings(
    monkeypatch: pytest.MonkeyPatch, kind: str, async_mode: bool, shape: str,
) -> None:
    """
    实际客户端解码列表和分页字典后，四个模块入口均保留推荐顺序且不污染缓存。
    """
    records = [
        {"id": "100", "type": kind, "title": "第一条推荐"},
        {"id": "101", "type": kind, "title": "第二条推荐"},
    ]
    payloads = {
        "list": records,
        "mapping": {"items": records},
        "empty_list": [],
        "empty_mapping": {},
        "missing": None,
    }
    payload = payloads[shape]
    original = deepcopy(payload)
    response = None if payload is None else Mock(status_code=200, json=Mock(return_value=payload))
    api = object.__new__(DoubanApi)
    api._request = Mock(get_res=Mock(return_value=response))
    async_request = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "app.modules.douban.apiv2.AsyncRequestUtils",
        Mock(return_value=Mock(get_res=async_request)),
    )
    module = object.__new__(DoubanModule)
    module.doubanapi = api
    method = f"{'async_' if async_mode else ''}douban_{kind}_recommend"
    api.clear_cache()
    try:
        result = getattr(module, method)("42")
        result = asyncio.run(result) if async_mode else result
        expected_ids = ["100", "101"] if shape in {"list", "mapping"} else []
        expected_titles = [record["title"] for record in records] if expected_ids else []
        assert [media.douban_id for media in result] == expected_ids
        assert [media.title for media in result] == expected_titles
        assert payload == original
        request = async_request if async_mode else api._request.get_res
        request.assert_called_once()
        assert request.call_args.kwargs["url"].endswith(f"/{kind}/42/recommendations")
    finally:
        api.clear_cache()
