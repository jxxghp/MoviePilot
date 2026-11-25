"""
验证Web Push 订阅参数与推送请求构造。
"""

import importlib

from app.adapters.network.webpush import WebPushDeliveryError
from app.api.endpoints.message import (
    is_webpush_subscription_gone,
    is_wns_endpoint,
    webpush_options_for_endpoint,
)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_is_webpush_subscription_gone_for_expired_status_codes() -> None:
    """
    推送返回 404 或 410 时，将订阅判断为已失效。
    """
    for status_code in (404, 410):
        err = WebPushDeliveryError("gone", response=_FakeResponse(status_code))
        assert is_webpush_subscription_gone(err)


def test_is_webpush_subscription_gone_for_other_errors() -> None:
    """
    其他投递错误不能误判为订阅失效并触发删除。
    """
    err = WebPushDeliveryError("bad request", response=_FakeResponse(400))
    assert not is_webpush_subscription_gone(err)


def test_is_wns_endpoint_detects_windows_push_url() -> None:
    """
    仅 Windows 推送地址被识别为 WNS 端点，空地址安全返回否。
    """
    assert is_wns_endpoint("https://wns2-sg2p.notify.windows.com/w/?token=abc")
    assert not is_wns_endpoint("https://web.push.apple.com/abc")
    assert not is_wns_endpoint(None)
    assert not is_wns_endpoint("")


def test_webpush_options_for_wns_endpoint() -> None:
    """
    WNS 推送附带一天有效期与缓存请求头，兼容其投递要求。
    """
    options = webpush_options_for_endpoint("https://wns2-pn1p.notify.windows.com/x")
    assert options == {
        "ttl": 86400,
        "headers": {"X-WNS-Cache-Policy": "cache"},
    }


def test_webpush_options_for_non_wns_endpoint() -> None:
    """
    其他推送端点不附加 WNS 专用选项。
    """
    assert webpush_options_for_endpoint("https://fcm.googleapis.com/fcm/send/abc") == {}


def test_legacy_webpush_path_reuses_message_endpoint() -> None:
    """
    旧 Web Push helper 路径应复用消息端点中的同一实现。
    """
    legacy = importlib.import_module("app.helper.webpush")

    assert legacy.is_webpush_subscription_gone is is_webpush_subscription_gone
    assert legacy.webpush_options_for_endpoint is webpush_options_for_endpoint
