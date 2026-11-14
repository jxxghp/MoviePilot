"""
验证 QQ Bot API 的响应边界和消息请求组装。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.qqbot import api


@pytest.fixture(autouse=True)
def isolated_token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    每个用例使用独立缓存，并由 monkeypatch 恢复原进程状态。
    """
    monkeypatch.setattr(api, "_cached_token", None)


class _RequestClient:
    """
    提供可控的 QQ API HTTP 响应。
    """

    def __init__(self, response: object) -> None:
        """
        保存本次测试的模拟响应。
        """
        self.response = response

    def post_res(self, *_args: object, **_kwargs: object) -> object:
        """
        返回预设的 POST 响应。
        """
        return self.response

    def get_res(self, *_args: object, **_kwargs: object) -> object:
        """
        返回预设的 GET 响应。
        """
        return self.response


def test_get_access_token_caches_and_reuses_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    有效的令牌响应应被缓存，并在有效期内复用。
    """
    api.clear_token_cache()
    response = SimpleNamespace(json=lambda: {"access_token": "token", "expires_in": 7200})
    client = Mock(return_value=_RequestClient(response))
    monkeypatch.setattr(api, "RequestUtils", client)

    assert api.get_access_token("app", "secret") == "token"
    assert api.get_access_token("app", "secret") == "token"
    client.assert_called_once_with(timeout=30)


@pytest.mark.parametrize("payload", [[], {"access_token": 123}])
def test_get_access_token_rejects_invalid_response(
    monkeypatch: pytest.MonkeyPatch, payload: object,
) -> None:
    """
    令牌响应不是对象或令牌不是字符串时应失败。
    """
    api.clear_token_cache()
    response = SimpleNamespace(json=lambda: payload)
    monkeypatch.setattr(api, "RequestUtils", Mock(return_value=_RequestClient(response)))

    with pytest.raises(ValueError):
        api.get_access_token("app", "secret")


def test_api_request_validates_json_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    API 请求应收窄 JSON 对象，并将 HTTP 错误转换为异常。
    """
    response = SimpleNamespace(json=lambda: {"url": "wss://gateway"}, status_code=200)
    monkeypatch.setattr(api, "RequestUtils", Mock(return_value=_RequestClient(response)))
    assert api.get_gateway_url("token") == "wss://gateway"

    error = SimpleNamespace(json=lambda: {"message": "denied"}, status_code=403)
    monkeypatch.setattr(api, "RequestUtils", Mock(return_value=_RequestClient(error)))
    with pytest.raises(ValueError, match="denied"):
        api._api_request("token", "GET", "/gateway")


def test_send_message_builds_passive_and_proactive_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    统一发送接口应按消息类型和 msg_id 选择正确的请求路径。
    """
    request = Mock(return_value={"id": "sent"})
    monkeypatch.setattr(api, "_api_request", request)

    assert api.send_message("token", "user", " hello ") == {"id": "sent"}
    assert api.send_message("token", "group", "hello", msg_type="group", msg_id="msg") == {"id": "sent"}
    assert request.call_args_list[0].args[2] == "/v2/users/user/messages"
    assert request.call_args_list[1].args[2] == "/v2/groups/group/messages"


def test_proactive_message_rejects_blank_content() -> None:
    """
    主动消息不能发送空白内容。
    """
    with pytest.raises(ValueError, match="不能为空"):
        api.send_proactive_c2c_message("token", "user", "  ")
