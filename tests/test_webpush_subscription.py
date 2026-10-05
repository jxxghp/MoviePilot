from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.security import access
from app.api.context import get_host_runtime, get_sync_session
from app.api.endpoints import message as message_endpoint
from app.api.endpoints.message import is_webpush_subscription_gone
from app.application.security.token import create_access_token
from app.runtime.config import global_vars, settings
from app.runtime.webpush import webpush_registry
from app.schemas.message import SubscriptionMessage
from app.schemas.token import TokenPayload


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

    from app.adapters.network.webpush import WebPushDeliveryError

    def fail_delivery(**_):
        """模拟推送服务拒收。"""
        raise WebPushDeliveryError("failed")

    monkeypatch.setattr("app.adapters.network.webpush.send_webpush", fail_delivery)

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

    from app.adapters.network.webpush import WebPushDeliveryError

    attempts = iter([None, WebPushDeliveryError("failed")])

    def deliver_once(**_):
        """模拟一次成功和一次失败的设备发送。"""
        attempt = next(attempts)
        if isinstance(attempt, Exception):
            raise attempt
        return attempt

    monkeypatch.setattr("app.adapters.network.webpush.send_webpush", deliver_once)

    response = message_endpoint.send_notification(SubscriptionMessage(title="测试"), object())

    assert response.success is True
    assert response.message == "消息已发送到 1 个设备，1 个设备发送失败"


def _webpush_send_client(monkeypatch, user) -> tuple[TestClient, list[dict]]:
    """构造挂载真实消息路由的客户端，并记录实际发出的浏览器通知。"""
    deliveries = []
    webpush_registry.upsert({"endpoint": "https://push.example/a", "keys": {}})
    monkeypatch.setattr(access, "_token_identity_validator", lambda _payload: None)
    monkeypatch.setattr(
        message_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(vapid_private_key="private", vapid_subject="mailto:test@example.com"),
    )
    monkeypatch.setattr(
        "app.adapters.network.webpush.send_webpush",
        lambda **kwargs: deliveries.append(kwargs),
    )
    repository = SimpleNamespace(get_by_id=lambda user_id: user if user_id == user.id else None)
    app = FastAPI()
    app.include_router(message_endpoint.router, prefix="/message")
    app.dependency_overrides[get_host_runtime] = lambda: SimpleNamespace(
        authentication=SimpleNamespace(user_repository=lambda _db: repository)
    )
    app.dependency_overrides[get_sync_session] = lambda: object()
    return TestClient(app), deliveries


def _bearer(user) -> dict[str, str]:
    """为测试用户生成登录 JWT 请求头。"""
    token = create_access_token(
        userid=user.id,
        username=user.name,
        super_user=user.is_superuser,
    )
    return {"Authorization": f"Bearer {token}"}


def _user(*, superuser: bool) -> SimpleNamespace:
    """构造当前用户依赖所需的最小用户投影。"""
    return SimpleNamespace(id=7, name="tester", is_active=True, is_superuser=superuser)


def test_send_notification_rejects_regular_user_without_delivery(monkeypatch):
    """普通用户调用 webpush/send 应返回 403，且不向任何订阅发送通知。"""
    user = _user(superuser=False)
    client, deliveries = _webpush_send_client(monkeypatch, user)

    with client:
        response = client.post(
            "/message/webpush/send",
            json={"title": "测试", "url": "https://example.com"},
            headers=_bearer(user),
        )

    assert response.status_code == 403
    assert deliveries == []


def test_send_notification_allows_superuser(monkeypatch):
    """超级管理员登录后可以向已订阅浏览器发送通知。"""
    user = _user(superuser=True)
    client, deliveries = _webpush_send_client(monkeypatch, user)

    with client:
        response = client.post(
            "/message/webpush/send",
            json={"title": "测试"},
            headers=_bearer(user),
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert len(deliveries) == 1
    assert deliveries[0]["subscription"]["endpoint"] == "https://push.example/a"


def test_send_notification_allows_api_token(monkeypatch):
    """使用 API_TOKEN 的外部集成按超级管理员身份继续可以发送通知。"""
    user = _user(superuser=True)
    client, deliveries = _webpush_send_client(monkeypatch, user)
    monkeypatch.setattr(settings, "API_TOKEN", "webpush-test-api-token")
    monkeypatch.setattr(
        access,
        "_superuser_token_payload_provider",
        lambda: TokenPayload(sub=user.id, username=user.name, super_user=True, purpose="authentication"),
    )

    with client:
        response = client.post(
            "/message/webpush/send",
            json={"title": "测试"},
            headers={"X-API-KEY": "webpush-test-api-token"},
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert len(deliveries) == 1
