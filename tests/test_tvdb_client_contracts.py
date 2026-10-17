"""
TVDB 类型声明必须保留 URL、原始响应、条件请求和缓存的实际行为。
"""

import inspect
import json
from unittest.mock import Mock, call

import pytest

from app.modules.thetvdb import client


@pytest.fixture(autouse=True)
def clear_request_cache():
    """
    隔离共享请求缓存，避免测试之间复用响应或遗留缓存状态。
    """
    client.Request.make_request.cache_clear()
    yield
    client.Request.make_request.cache_clear()


@pytest.fixture
def transport(monkeypatch):
    """
    在统一 HTTP 边界注入内存响应，不建立真实连接。
    """
    request = Mock()
    monkeypatch.setattr(client, "RequestUtils", Mock(return_value=request))
    return request


def response(payload, status=200):
    """
    模拟保留原始 JSON 值的 HTTP 响应。
    """
    return Mock(status_code=status, json=Mock(return_value=payload))


@pytest.mark.parametrize("data", [{"id": 42, "nested": [None, True]}, [1, "x"], {}, [], "text", 0, False])
def test_request_preserves_raw_data_and_links(transport, data):
    """
    合法 data 包括空容器与标量；分页链接按服务端原值保存。
    """
    links = {"next": "/series?page=2", "prev": None}
    transport.get_res.return_value = response({"status": "success", "data": data, "links": links})
    request = client.Request("token")

    assert request.make_request("https://example.com/series") == data
    assert request.links is links
    transport.get_res.assert_called_once_with(
        url="https://example.com/series", headers={"Authorization": "Bearer token"},
    )


@pytest.mark.parametrize("modified", [None, False, True, "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_not_modified_preserves_response_and_previous_links(transport, modified):
    """
    304 不解码 JSON、不覆盖分页状态，条件请求头保持原字符串化规则。
    """
    reply = response(None, status=304)
    transport.get_res.return_value = reply
    request = client.Request("token")
    previous = {"next": "page-2"}
    request.links = previous

    assert request.make_request("https://example.com/series", modified) == {
        "code": 304, "message": "Not-Modified",
    }
    assert request.links is previous
    reply.json.assert_not_called()
    headers = {"Authorization": "Bearer token"}
    if modified:
        headers["If-Modified-Since"] = str(modified)
    assert transport.get_res.call_args.kwargs["headers"] == headers


@pytest.mark.parametrize("reply, message", [
    (None, "网络连接失败"),
    (response({"message": "denied"}, 401), "denied"),
    (response({"status": "failure", "data": {"id": 1}, "message": "failed"}), "failed"),
    (response({"status": "success", "data": None, "message": "empty"}), "empty"),
    (response([]), "has no attribute 'get'"),
])
def test_request_failures_keep_errors_and_are_not_cached(transport, reply, message):
    """
    网络、业务和响应形状失败仍抛 ValueError；相同地址随后可以成功。
    """
    transport.get_res.side_effect = [reply, response({"status": "success", "data": {"id": 9}})]
    request = client.Request("token")
    with pytest.raises(ValueError, match=message):
        request.make_request("https://example.com/retry")
    assert request.make_request("https://example.com/retry") == {"id": 9}
    assert transport.get_res.call_count == 2


def test_request_keeps_json_value_error_identity(transport):
    """
    原始 JSON ValueError 按既有分支重抛，不重新包装。
    """
    error = ValueError("invalid json")
    transport.get_res.return_value = Mock(status_code=200, json=Mock(side_effect=error))
    with pytest.raises(ValueError) as raised:
        client.Request("token").make_request("https://example.com/invalid")
    assert raised.value is error


def test_cache_hit_and_clear_keep_original_state_transitions(transport):
    """
    缓存命中不重放 links 副作用，清理后再次读取网络并更新分页状态。
    """
    transport.get_res.side_effect = [
        response({"status": "success", "data": {"id": 1}, "links": {"next": "a"}}),
        response({"status": "success", "data": {"id": 2}, "links": {"next": "b"}}),
        response({"status": "success", "data": {"id": 3}, "links": None}),
    ]
    api = object.__new__(client.TVDB)
    api.request = client.Request("token")
    assert api.request.make_request("https://example.com/a") == {"id": 1}
    assert api.request.make_request("https://example.com/b") == {"id": 2}
    assert api.request.make_request("https://example.com/a") == {"id": 1}
    assert api.get_req_links() == {"next": "b"}
    assert transport.get_res.call_count == 2
    api.clear_cache()
    assert api.request.make_request("https://example.com/a") == {"id": 3}
    assert api.get_req_links() is None
    assert transport.get_res.call_count == 3


@pytest.mark.parametrize("method, value, expected", [
    ("search", "中文 空格", "search/12/extended/zho?query=%E4%B8%AD%E6%96%87+%E7%A9%BA%E6%A0%BC&year=2026&flag=False"),
    ("get_updates", 123, "updates/12/extended/zho?since=123&year=2026&flag=False"),
])
def test_dynamic_queries_keep_path_extensions_and_filter_values(method, value, expected):
    """
    原 kwargs 中的路径键仍构建路径，任意过滤键按原值编码且只去掉 None。
    """
    api = object.__new__(client.TVDB)
    api.url = client.Url()
    api.request = Mock()
    getattr(api, method)(value, url_id=12, url_subsect="extended", url_lang="zho",
                         year=2026, absent=None, flag=False)
    assert api.request.make_request.call_args == call(api.url.base_url + expected)


@pytest.mark.parametrize("method", ["search", "get_updates"])
@pytest.mark.parametrize("options", [{"url_subsect": 1}, {"url_lang": 1}, {"url_sect": "duplicate"}])
def test_invalid_dynamic_path_parameters_keep_type_error(method, options):
    """
    无效路径类型与重复 section 参数仍在网络请求前失败。
    """
    api = object.__new__(client.TVDB)
    api.url = client.Url()
    api.request = Mock()
    with pytest.raises(TypeError):
        getattr(api, method)("value", **options)
    api.request.make_request.assert_not_called()


API_METHODS = [
    name for name in dir(client.TVDB)
    if (name.startswith("get_") or name.startswith("search")) and name != "get_req_links"
]


@pytest.mark.parametrize("method", API_METHODS)
def test_all_endpoint_wrappers_preserve_conditional_response(method):
    """
    每个 API 封装均透传 304 字典，不能因名义上的列表返回类型改变响应。
    """
    api = object.__new__(client.TVDB)
    api.url = client.Url()
    result = {"code": 304, "message": "Not-Modified"}
    api.request = Mock(make_request=Mock(return_value=result))
    function = getattr(api, method)
    arguments = {
        name: (42 if name in {"id", "since"} else "value")
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    }
    assert function(**arguments) is result
    api.request.make_request.assert_called_once()


@pytest.mark.parametrize("token", ["token", 42, None])
def test_auth_keeps_token_and_login_body(transport, token):
    """
    认证保留 JSON token 原值，登录参数不因类型声明发生转换。
    """
    transport.post_res.return_value = response({"data": {"token": token}})
    auth = client.Auth("https://example.com/login", "apikey", "pin")
    assert auth.get_token() == token
    assert json.loads(transport.post_res.call_args.kwargs["data"]) == {"apikey": "apikey", "pin": "pin"}
    assert transport.post_res.call_args.kwargs["headers"] == {"Content-Type": "application/json"}
