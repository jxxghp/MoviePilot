from types import SimpleNamespace

import pytest

from app.api.endpoints.message import is_webpush_subscription_gone
from app.runtime.config import global_vars


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
