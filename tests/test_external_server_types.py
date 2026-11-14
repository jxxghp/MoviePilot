"""
中心服务 JSON 边界与同步异步传输的类型收窄回归。
"""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from requests import Response

from app.adapters.external import server
from app.adapters.external.server import MoviePilotServerHelper


def _response(payload: object) -> Response:
    """
    构造真实响应容器，避免请求外部服务。
    """
    response = Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode()
    return response


@pytest.mark.parametrize("payload", [None, "invalid", 3, [], [1]])
def test_mapping_rejects_non_object(payload: object) -> None:
    """
    非对象 JSON 不得冒充中心服务对象结果。
    """
    with pytest.raises(ValueError, match="JSON 对象"):
        server._response_mapping(_response(payload))


@pytest.mark.parametrize("payload", [None, {}, "invalid", [1], [{"id": 1}, None]])
def test_list_rejects_non_object_records(payload: object) -> None:
    """
    列表必须同时满足外层数组和每条记录为对象的契约。
    """
    with pytest.raises(ValueError, match="JSON 对象列表"):
        MoviePilotServerHelper._handle_list_response(_response(payload))


def test_json_objects_preserve_nested_values() -> None:
    """
    合法对象和列表保留嵌套字段与空值，不投影或丢弃未知字段。
    """
    payload = {"unknown": [None, {"enabled": True}], "code": 0}
    assert server._response_mapping(_response(payload)) == payload
    assert MoviePilotServerHelper._handle_list_response(_response([payload])) == [payload]
    assert MoviePilotServerHelper._handle_list_response(_response([])) == []


@pytest.mark.asyncio
async def test_invalid_rating_and_report_keep_failure_fallbacks(monkeypatch) -> None:
    """
    无效评分和识别上报对象仍由既有失败出口处理。
    """
    monkeypatch.setattr(MoviePilotServerHelper, "async_plugin_ratings", AsyncMock(return_value=_response({"plugin": 3})))
    monkeypatch.setattr(MoviePilotServerHelper, "async_plugin_rating", AsyncMock(return_value=_response([])))
    monkeypatch.setattr(MoviePilotServerHelper, "async_rate_plugin", AsyncMock(return_value=_response([])))
    assert await MoviePilotServerHelper.async_get_plugin_ratings() == {}
    assert (await MoviePilotServerHelper.async_get_plugin_rating("plugin"))["rating_count"] == 0
    assert await MoviePilotServerHelper.async_submit_plugin_rating("plugin", 4.0) is None
    assert MoviePilotServerHelper._parse_recognize_report_response(_response([])) is False


@pytest.mark.parametrize("params", [None, {}, {"share_id": 1}])
@pytest.mark.asyncio
async def test_delete_preserves_omitted_and_explicit_params(monkeypatch, params) -> None:
    """
    未传参数使用传输默认值，显式空对象和筛选字段均原样发送。
    """
    response = _response({"code": 0})
    sync_client = Mock()
    sync_client.delete_res.return_value = response
    async_client = Mock()
    async_client.delete_res = AsyncMock(return_value=response)
    monkeypatch.setattr(server, "RequestUtils", Mock(return_value=sync_client))
    monkeypatch.setattr(server, "AsyncRequestUtils", Mock(return_value=async_client))
    monkeypatch.setattr(MoviePilotServerHelper, "build_headers", Mock(return_value={}))
    monkeypatch.setattr(server, "get_runtime_setting", Mock(return_value=None))
    url = "https://server.invalid/share"
    assert MoviePilotServerHelper._delete(url, params=params) is response
    assert await MoviePilotServerHelper._async_delete(url, params=params) is response
    kwargs = {} if params is None else {"params": params}
    sync_client.delete_res.assert_called_once_with(url, **kwargs)
    async_client.delete_res.assert_awaited_once_with(url, **kwargs)


def test_typed_cache_keeps_query_results_and_invalidation(monkeypatch) -> None:
    """
    包装后的统计查询重复读取走缓存，失效后重新调用传输端口。
    """
    monkeypatch.setattr(server, "get_runtime_setting", Mock(return_value=True))
    source = Mock(side_effect=[_response({"count": 1}), _response({"count": 2})])
    monkeypatch.setattr(MoviePilotServerHelper, "plugin_statistic", source)
    server._clear_server_cache(MoviePilotServerHelper.get_plugin_statistic)
    try:
        assert MoviePilotServerHelper.get_plugin_statistic() == {"count": 1}
        assert MoviePilotServerHelper.get_plugin_statistic() == {"count": 1}
        assert source.call_count == 1
        server._clear_server_cache(MoviePilotServerHelper.get_plugin_statistic)
        assert MoviePilotServerHelper.get_plugin_statistic() == {"count": 2}
        assert source.call_count == 2
    finally:
        server._clear_server_cache(MoviePilotServerHelper.get_plugin_statistic)
