from types import SimpleNamespace

import pytest

from app.api.endpoints import message as message_endpoint
from app.api.endpoints.message import is_webpush_subscription_gone
from app.runtime.config import global_vars
from app.runtime.webpush import webpush_registry
from app.schemas.message import SubscriptionMessage


@pytest.fixture(autouse=True)
def clear_push_subscriptions():
    """在每个用例前后清理跨用例共享的 Web Push 订阅。"""
    with global_vars.SUBSCRIPTIONS_LOCK:
        global_vars.SUBSCRIPTIONS.clear()
    yield
    with global_vars.SUBSCRIPTIONS_LOCK:
        global_vars.SUBSCRIPTIONS.clear()


def test_push_subscription_upserts_by_endpoint():
    """相同 endpoint 的 Web Push 订阅应更新而不是重复追加。"""
    global_vars.push_subscription(
        {"endpoint": "https://push.example/a", "keys": {"p256dh": "old"}}
    )
    global_vars.push_subscription(
        {"endpoint": "https://push.example/a", "keys": {"p256dh": "new"}}
    )

    subscriptions = global_vars.get_subscriptions()

    assert len(subscriptions) == 1
    assert subscriptions[0]["keys"]["p256dh"] == "new"


def test_remove_subscription_deletes_by_endpoint():
    """失效订阅应能按 endpoint 从全局订阅表删除。"""
    subscription = {"endpoint": "https://push.example/a", "keys": {}}
    global_vars.push_subscription(subscription)

    assert global_vars.remove_subscription(subscription)
    assert global_vars.get_subscriptions() == []


def test_global_vars_webpush_api_delegates_to_canonical_registry():
    """旧订阅 API 与 canonical WebPushRegistry 必须共享同一事实源。"""
    subscription = {"endpoint": "https://push.example/registry", "keys": {}}

    webpush_registry.upsert(subscription)

    assert global_vars.get_subscriptions() == [subscription]
    assert global_vars.remove_subscription(subscription)
    assert webpush_registry.list() == []


def test_is_webpush_subscription_gone_matches_404_and_410():
    """推送服务返回 404/410 时应识别为订阅已失效。"""
    assert is_webpush_subscription_gone(
        SimpleNamespace(response=SimpleNamespace(status_code=410))
    )
    assert is_webpush_subscription_gone(
        SimpleNamespace(response=SimpleNamespace(status=404))
    )
    assert not is_webpush_subscription_gone(
        SimpleNamespace(response=SimpleNamespace(status_code=500))
    )


def test_send_notification_reports_all_delivery_failures(monkeypatch):
    """所有浏览器通知发送失败时，接口应返回可执行的失败提示。"""
    webpush_registry.upsert(
        {"endpoint": "https://push.example/a", "keys": {"p256dh": "key"}}
    )
    monkeypatch.setattr(
        message_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(vapid_private_key="private", vapid_subject="mailto:test@example.com"),
    )

    from pywebpush import WebPushException

    def fail_delivery(**_):
        """模拟 Web Push SDK 抛出发送异常。"""
        raise WebPushException("failed")

    monkeypatch.setattr("pywebpush.webpush", fail_delivery)

    response = message_endpoint.send_notification(SubscriptionMessage(title="测试"), object())

    assert response.success is False
    assert response.message == "消息发送失败，请检查浏览器通知权限后重试"


def test_send_notification_reports_partial_delivery(monkeypatch):
    """部分设备发送成功时，接口应同时告知成功和失败数量。"""
    webpush_registry.upsert({"endpoint": "https://push.example/a", "keys": {}})
    webpush_registry.upsert({"endpoint": "https://push.example/b", "keys": {}})
    monkeypatch.setattr(
        message_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(vapid_private_key="private", vapid_subject="mailto:test@example.com"),
    )

    from pywebpush import WebPushException

    attempts = iter([None, WebPushException("failed")])

    def deliver_once(**_):
        """模拟一次成功和一次失败的设备发送。"""
        attempt = next(attempts)
        if isinstance(attempt, Exception):
            raise attempt
        return attempt

    monkeypatch.setattr("pywebpush.webpush", deliver_once)

    response = message_endpoint.send_notification(SubscriptionMessage(title="测试"), object())

    assert response.success is True
    assert response.message == "消息已发送到 1 个设备，1 个设备发送失败"
