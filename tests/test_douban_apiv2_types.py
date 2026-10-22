"""
验证豆瓣客户端类型边界保留响应缺失、同步异步缓存共享和签名语义。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.douban.apiv2 import DoubanApi


@pytest.mark.parametrize(
    ("method", "arguments", "verb"),
    [
        ("movie_detail", {"subject_id": "123"}, "get"),
        ("search", {"keyword": "离线测试", "ts": "20261017"}, "get"),
        ("movie_interests", {"subject_id": "123", "ts": "20261017"}, "get"),
        ("imdbid", {"imdbid": "tt123", "ts": "20261017"}, "post"),
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_sync_async_keep_response_and_cache_contract(
    monkeypatch: pytest.MonkeyPatch, method: str, arguments: dict[str, str], verb: str, missing: bool,
) -> None:
    """
    同步响应可复用到异步入口；缺失响应仍返回 None，且不可进入共享缓存。
    """
    api = object.__new__(DoubanApi)
    payload = {"id": "123", "nested": {"items": []}}
    response = None if missing else Mock(status_code=200, json=Mock(return_value=payload))
    sync_request = Mock(return_value=response)
    async_request = AsyncMock(return_value=response)
    api._request = Mock(**{f"{verb}_res": sync_request})
    monkeypatch.setattr(
        "app.modules.douban.apiv2.AsyncRequestUtils",
        Mock(return_value=Mock(**{f"{verb}_res": async_request})),
    )
    api.clear_cache()
    try:
        expected = None if missing else payload
        assert getattr(api, method)(**arguments) == expected
        assert asyncio.run(getattr(api, f"async_{method}")(**arguments)) == expected
        sync_request.assert_called_once()
        assert async_request.await_count == int(missing)
        params = sync_request.call_args.kwargs["params" if verb == "get" else "data"]
        assert params["apiKey" if verb == "get" else "apikey"]
        if verb == "get":
            assert params["_sig"]
        else:
            assert "_ts" not in params
    finally:
        api.clear_cache()


def test_get_signing_preserves_pagination_and_timestamp() -> None:
    """
    类型标注不改变签名时间戳及可空分页参数，查询参数仍原样传递。
    """
    api = object.__new__(DoubanApi)
    url, params = api._prepare_get_request("/movie/123", _ts="20261017", start=None, count=20)
    assert url.endswith("/movie/123")
    assert params["_ts"] == "20261017"
    assert params["start"] is None
    assert params["count"] == 20
    assert params["_sig"] == "8yExvTiaclGFwomN18sCsMQJt2c="
