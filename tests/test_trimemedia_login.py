"""
飞牛影视（trimemedia）v2 登录协议回归测试（Issue #6328）

新版服务端废弃 v1 明文登录接口，login() 需优先使用
POST /api/v2/user/loginByPassword（密码为 SHA256 十六进制摘要），
并在 v2 接口不可用时回退旧版 v1 明文登录。
"""

import hashlib
from unittest.mock import patch

from app.modules.trimemedia.api import Api


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


def _create_api() -> Api:
    """
    构造未配置访问码的 Api 实例，跳过访问码校验流程
    """
    return Api(host="http://fn.local/v", apikey="test-api-key")


def test_login_uses_v2_login_by_password_with_sha256():
    """
    新版服务端：走 v2 接口，密码为明文密码的 SHA256 十六进制小写摘要
    """
    api = _create_api()
    with patch.object(api._request_utils, "request") as mock_request:
        mock_request.return_value = _FakeResponse(
            {"code": 0, "data": {"token": "v2-token"}}
        )
        token = api.login("admin", "secret")

    assert token == "v2-token"
    assert api.token == "v2-token"
    assert mock_request.call_count == 1
    call = mock_request.call_args
    assert call.kwargs["url"] == "http://fn.local/v/api/v2/user/loginByPassword"
    body = call.kwargs["data"]
    expected_hash = hashlib.sha256("secret".encode()).hexdigest()
    assert f'"password": "{expected_hash}"' in body
    assert '"password": "secret"' not in body


def test_login_falls_back_to_v1_when_v2_unavailable():
    """
    旧版服务端：v2 接口不可用（HTTP 失败返回 None）时回退 v1 明文登录
    """
    api = _create_api()
    with patch.object(api._request_utils, "request") as mock_request:
        mock_request.side_effect = [
            None,
            _FakeResponse({"code": 0, "data": {"token": "v1-token"}}),
        ]
        token = api.login("admin", "secret")

    assert token == "v1-token"
    assert mock_request.call_count == 2
    urls = [call.kwargs["url"] for call in mock_request.call_args_list]
    assert urls[0] == "http://fn.local/v/api/v2/user/loginByPassword"
    assert urls[1] == "http://fn.local/v/api/v1/login"
    # v1 回退仍传输明文密码
    assert '"password": "secret"' in mock_request.call_args_list[1].kwargs["data"]


def test_login_does_not_fallback_when_v2_rejects_credentials():
    """
    v2 接口存在但登录失败（账号密码错误）时直接返回 None，不回退 v1
    """
    api = _create_api()
    with patch.object(api._request_utils, "request") as mock_request:
        mock_request.return_value = _FakeResponse(
            {"code": -15, "msg": "Password Incorrect"}
        )
        token = api.login("admin", "wrong-password")

    assert token is None
    assert api.token is None
    # 新版服务端 v1 登录一律返回 -15，回退无意义，仅请求一次
    assert mock_request.call_count == 1
    assert (
        mock_request.call_args.kwargs["url"]
        == "http://fn.local/v/api/v2/user/loginByPassword"
    )


def test_request_base_path_override_does_not_affect_default_path():
    """
    base_path 参数仅影响单次请求，其余接口仍使用默认 v1 路径
    """
    api = _create_api()
    with patch.object(api._request_utils, "request") as mock_request:
        mock_request.return_value = _FakeResponse({"code": 0, "data": {}})
        api.request("/user/info")
        api.request("/user/loginByPassword", data={}, base_path="/api/v2")
        api.request("/mediadb/sum")

    urls = [call.kwargs["url"] for call in mock_request.call_args_list]
    assert urls == [
        "http://fn.local/v/api/v1/user/info",
        "http://fn.local/v/api/v2/user/loginByPassword",
        "http://fn.local/v/api/v1/mediadb/sum",
    ]
