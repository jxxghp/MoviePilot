"""
验证企业微信 access_token 的有效期判断，以及令牌失效后的强制重取边界。
"""

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.modules.wechat.wechat import WeChat
from app.runtime import execution

_PROXY = "http://proxy.local"
_SEND_URL_PREFIX = f"{_PROXY}/cgi-bin/message/send?access_token="

_CONFIG = {
    "name": "wechat-test",
    "WECHAT_CORPID": "corpid",
    "WECHAT_APP_SECRET": "secret",
    "WECHAT_APP_ID": "1000002",
    "WECHAT_PROXY": _PROXY,
}


def _token_response(token: str) -> Mock:
    """
    构造 gettoken 响应，字段与企业微信返回保持一致。

    :param token: 响应中携带的访问令牌
    :return: 行为等价于 200 响应的替身对象
    """
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "errcode": 0,
        "errmsg": "ok",
        "access_token": token,
        "expires_in": 7200,
    }
    return response


def _send_response(errcode: int) -> Mock:
    """
    构造 message/send 响应，只保留判定需要的错误码。

    :param errcode: 企业微信接口返回的错误码
    :return: 行为等价于 200 响应的替身对象
    """
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"errcode": errcode, "errmsg": f"errcode-{errcode}"}
    return response


def _create_client(request_utils: Mock, token: str = "token-1") -> WeChat:
    """
    在替身 HTTP 层上创建客户端，避免构造过程发出真实请求。

    :param request_utils: 被替换的 RequestUtils 类替身
    :param token: 构造阶段 gettoken 返回的令牌
    :return: 已完成初始化的企业微信客户端
    """
    request_utils.return_value.get_res.return_value = _token_response(token)
    request_utils.return_value.post.return_value = _send_response(0)
    return WeChat(**_CONFIG)


def _skip_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    去掉重试装饰器的退避等待，让用例只验证重试次数与结果。

    :param monkeypatch: pytest 提供的属性替换夹具
    """
    monkeypatch.setattr(
        execution,
        "time",
        SimpleNamespace(sleep=lambda *_: None, time=time.time, monotonic=time.monotonic),
    )


def test_stale_token_is_refreshed_before_send() -> None:
    """
    令牌空闲数天后发送消息时必须先重取，不受 timedelta.seconds 按天回绕的影响。
    """
    with patch("app.modules.wechat.wechat.RequestUtils") as request_utils:
        client = _create_client(request_utils)
        # 8 天 1 小时 43 分：timedelta.seconds 只剩 6180 秒，小于有效期 7200 秒
        client._access_token = "token-stale"
        client._access_token_time = datetime.now() - timedelta(days=8, hours=1, minutes=43)
        client._expires_in = 7200
        request_utils.return_value.get_res.return_value = _token_response("token-fresh")

        assert client.send_msg(title="标题", text="内容", userid="user-1") is True

    posted_url = request_utils.return_value.post.call_args.kwargs["url"]
    assert posted_url.startswith(_SEND_URL_PREFIX)
    assert posted_url == f"{_SEND_URL_PREFIX}token-fresh"
    assert request_utils.return_value.get_res.call_count == 2


@pytest.mark.parametrize("errcode", [40014, 41001, 42001])
def test_invalid_token_is_refreshed_and_sent_again(
    monkeypatch: pytest.MonkeyPatch, errcode: int
) -> None:
    """
    接口返回令牌失效类错误码时应强制重取令牌并重试发送。

    :param monkeypatch: pytest 提供的属性替换夹具
    :param errcode: 企业微信返回的令牌失效类错误码
    """
    _skip_retry_backoff(monkeypatch)
    with patch("app.modules.wechat.wechat.RequestUtils") as request_utils:
        client = _create_client(request_utils)
        post = request_utils.return_value.post
        post.side_effect = [_send_response(errcode), _send_response(0)]
        request_utils.return_value.get_res.return_value = _token_response("token-fresh")

        assert client.send_msg(title="标题", text="内容", userid="user-1") is True

    assert post.call_count == 2
    assert post.call_args.kwargs["url"] == f"{_SEND_URL_PREFIX}token-fresh"
    assert request_utils.return_value.get_res.call_count == 2


def test_other_errors_do_not_refresh_token() -> None:
    """
    非令牌类错误码只记录失败，不触发令牌重取。
    """
    with patch("app.modules.wechat.wechat.RequestUtils") as request_utils:
        client = _create_client(request_utils)
        request_utils.return_value.post.return_value = _send_response(40003)

        assert client.send_msg(title="标题", text="内容", userid="user-1") is False

    assert request_utils.return_value.post.call_count == 1
    assert request_utils.return_value.get_res.call_count == 1
