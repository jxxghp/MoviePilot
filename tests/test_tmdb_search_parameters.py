"""TMDb 成人内容开关须生成独立参数，不能损坏分页参数。"""

import asyncio
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs

import pytest

from app.modules.themoviedb.tmdbv3api.objs.search import Search


@pytest.mark.parametrize("method", ["movies", "multi", "people", "tv_shows"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("adult", [None, False, True])
def test_adult_option_preserves_query_and_page(monkeypatch, method, asynchronous, adult):
    """双入口保持查询和页码，显式 False 与未传开关分别对应 false 与省略。"""
    payload = [{"id": 42}]
    request = (AsyncMock if asynchronous else Mock)(return_value=payload)
    monkeypatch.setattr(Search, "_async_request_obj" if asynchronous else "_request_obj", request)
    client = object.__new__(Search)
    operation = getattr(client, f"async_{method}" if asynchronous else method)
    arguments = {"term": "测试 & movie", "page": 2, "adult": adult}
    result = asyncio.run(operation(**arguments)) if asynchronous else operation(**arguments)
    assert result is payload
    request.assert_called_once()
    parameters = parse_qs(request.call_args.kwargs["params"])
    expected = {"query": ["测试 & movie"], "page": ["2"]}
    if adult is not None:
        expected["include_adult"] = ["true" if adult else "false"]
    assert parameters == expected
    assert request.call_args.kwargs["key"] == "results"
