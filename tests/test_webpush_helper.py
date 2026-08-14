import importlib

from app.api.endpoints.message import (
    is_webpush_subscription_gone,
    is_wns_endpoint,
    webpush_options_for_endpoint,
)
from pywebpush import WebPushException


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_is_webpush_subscription_gone_for_expired_status_codes() -> None:
    for status_code in (404, 410):
        err = WebPushException("gone")
        err.response = _FakeResponse(status_code)
        assert is_webpush_subscription_gone(err)


def test_is_webpush_subscription_gone_for_other_errors() -> None:
    err = WebPushException("bad request")
    err.response = _FakeResponse(400)
    assert not is_webpush_subscription_gone(err)


def test_is_wns_endpoint_detects_windows_push_url() -> None:
    assert is_wns_endpoint("https://wns2-sg2p.notify.windows.com/w/?token=abc")
    assert not is_wns_endpoint("https://web.push.apple.com/abc")
    assert not is_wns_endpoint(None)
    assert not is_wns_endpoint("")


def test_webpush_options_for_wns_endpoint() -> None:
    options = webpush_options_for_endpoint("https://wns2-pn1p.notify.windows.com/x")
    assert options == {
        "ttl": 86400,
        "headers": {"X-WNS-Cache-Policy": "cache"},
    }


def test_webpush_options_for_non_wns_endpoint() -> None:
    assert webpush_options_for_endpoint("https://fcm.googleapis.com/fcm/send/abc") == {}


def test_legacy_webpush_path_reuses_message_endpoint() -> None:
    """旧 Web Push helper 路径应复用消息端点中的同一实现。"""
    legacy = importlib.import_module("app.helper.webpush")

    assert legacy.is_webpush_subscription_gone is is_webpush_subscription_gone
    assert legacy.webpush_options_for_endpoint is webpush_options_for_endpoint
