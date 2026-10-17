"""
TMDb 成人内容开关须生成独立参数，不能损坏分页参数。
"""

import asyncio
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs

import pytest

from app.modules.themoviedb.tmdbv3api.objs.discover import Discover
from app.modules.themoviedb.tmdbv3api.objs.search import Search


@pytest.mark.parametrize("method", ["movies", "multi", "people", "tv_shows"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("adult", [None, False, True])
def test_adult_option_preserves_query_and_page(monkeypatch, method, asynchronous, adult):
    """
    双入口保持查询和页码，显式 False 与未传开关分别对应 false 与省略。
    """
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


@pytest.mark.parametrize(
    ("method", "asynchronous", "path"),
    [
        ("discover_movies", False, "/discover/movie"),
        ("discover_tv_shows", False, "/discover/tv"),
        ("async_discover_movies", True, "/discover/movie"),
        ("async_discover_tv_shows", True, "/discover/tv"),
    ],
)
def test_discover_methods_preserve_typed_query_contract(monkeypatch, method, asynchronous, path):
    """
    TMDb v3 的四个 discover 入口应将 tuple 查询交给对应请求端口。
    """
    request = (AsyncMock if asynchronous else Mock)(return_value={"id": 42})
    client = object.__new__(Discover)
    monkeypatch.setattr(client, "_async_request_obj" if asynchronous else "_request_obj", request)

    operation = getattr(client, method)
    params = (("page", 2), ("with_genres", "35"), ("test_method", method))
    result = asyncio.run(operation(params_tuple=params)) if asynchronous else operation(params)

    assert result == {"id": 42}
    request.assert_called_once()
    assert request.call_args.args[0] == path
    assert request.call_args.kwargs["key"] == "results"
    assert "page=2" in request.call_args.args[1]
    assert "with_genres=35" in request.call_args.args[1]
    assert f"test_method={method}" in request.call_args.args[1]
