"""
外部身份查询接受整数 TVDB ID，并保持字符串身份的路径编码。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.themoviedb.tmdbv3api.objs.find import Find


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method,identity,source,path", [
    ("find_by_tvdb_id", 353712, "tvdb_id", "353712"),
    ("find_by_tvdb_id", "353712", "tvdb_id", "353712"),
    ("find_by_imdb_id", "tt1234567", "imdb_id", "tt1234567"),
    ("find_by_freebase_mid", "/m/abc", "freebase_mid", "%2Fm%2Fabc"),
])
def test_find_preserves_external_identity_encoding(monkeypatch, asynchronous, method, identity, source, path):
    """
    整数查询不再调用 int.replace，字符串和斜杠身份仍发到同一个 TMDb 端点。
    """
    payload = {"movie_results": [], "tv_results": [{"id": 987}]}
    request = (AsyncMock if asynchronous else Mock)(return_value=payload)
    monkeypatch.setattr(Find, "_async_request_obj" if asynchronous else "_request_obj", request)
    client = object.__new__(Find)
    operation = getattr(client, f"async_{method}" if asynchronous else method)
    result = asyncio.run(operation(identity)) if asynchronous else operation(identity)
    assert result is payload
    request.assert_called_once_with(f"/find/{path}", params=f"external_source={source}")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("identity", [None, 1.5, object()])
def test_find_does_not_coerce_unsupported_identity(monkeypatch, asynchronous, identity):
    """
    只有整数 ID 新增支持；无效对象仍在请求前失败，不能被转换为路径文本。
    """
    request = AsyncMock() if asynchronous else Mock()
    monkeypatch.setattr(Find, "_async_request_obj" if asynchronous else "_request_obj", request)
    client = object.__new__(Find)
    with pytest.raises(AttributeError):
        if asynchronous:
            asyncio.run(client.async_find_by_tvdb_id(identity))
        else:
            client.find_by_tvdb_id(identity)
    request.assert_not_called()
