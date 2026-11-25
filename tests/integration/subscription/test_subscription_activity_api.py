"""
业务活动 API 概要、按需详情和鉴权边界测试。
"""

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import get_async_session, get_host_runtime
from app.api.dependencies.auth import get_current_active_user_async
from app.api.endpoints.subexecution import router
from app.application.subscription.activity import SubscriptionActivity


def _client():
    """
    装配真实响应路由与可核验归属参数的活动端口。
    """
    event = SubscriptionActivity(uuid4().hex, uuid4().hex, "refresh", "automatic", "empty", "2026-10-12T01:00:00Z",
                                 details={"reason": "没有相关资源", "resources": [{"title": "Example", "decision": "rejected"}], "rules": {"resolution": "2160p"}},
                                 tracking={"related": ["private-internal-key"]})
    calls = []

    async def list_accessible(subscription_id, username, superuser, **query):
        """
        普通用户读取其他订阅时与不存在返回相同结果。
        """
        calls.append((subscription_id, username, superuser, query))
        return ([event], None) if subscription_id == 1 else None

    async def get_accessible(subscription_id, event_id, username, superuser, **query):
        """
        按订阅和事件的共同身份返回完整详情。
        """
        calls.append((subscription_id, username, superuser, query))
        return event if subscription_id == 1 and event_id == event.event_id else None

    app = FastAPI()
    app.include_router(router, prefix="/subscribe")
    app.dependency_overrides[get_async_session] = lambda: None
    app.dependency_overrides[get_host_runtime] = lambda: SimpleNamespace(subscription=SimpleNamespace(
        activity_repository=SimpleNamespace(list_accessible=list_accessible, get_accessible=get_accessible)))
    return TestClient(app), app, event, calls


def test_activity_api_requires_authentication_and_validates_queries():
    """
    概要和详情均需登录，类型、时间范围和游标必须验证。
    """
    client, app, event, _calls = _client()
    assert client.get("/subscribe/1/activity").status_code == 401
    assert client.get(f"/subscribe/1/activity/{event.event_id}").status_code == 401
    app.dependency_overrides[get_current_active_user_async] = lambda: SimpleNamespace(name="owner", is_superuser=False)
    for query in ("kind=debug", "days=0", "days=366", "limit=101", "before=bad%20cursor"):
        assert client.get(f"/subscribe/1/activity?{query}").status_code == 422


def test_activity_summary_is_lightweight_and_details_are_owner_scoped():
    """
    列表只含概要，完整详情不含内部身份或去重键且使用相同归属校验。
    """
    client, app, event, calls = _client()
    app.dependency_overrides[get_current_active_user_async] = lambda: SimpleNamespace(name="owner", is_superuser=False)
    response = client.get("/subscribe/1/activity?history=true&days=7&kind=refresh")
    assert response.status_code == 200
    summary = response.json()["data"]["items"][0]
    assert summary["details_loaded"] is False
    assert summary["details"] == {"reason": "没有相关资源"}
    assert "activity_key" not in summary and "tracking" not in summary
    detail_response = client.get(f"/subscribe/1/activity/{event.event_id}?history=true")
    detail = detail_response.json()["data"]
    assert detail["details_loaded"] is True and detail["details"]["rules"]["resolution"] == "2160p"
    assert "private-internal-key" not in detail_response.text
    assert calls[0][:3] == calls[1][:3] == (1, "owner", False)
    assert calls[0][3]["history"] is True and calls[1][3]["history"] is True
    assert client.get("/subscribe/2/activity").status_code == 404
    assert client.get(f"/subscribe/2/activity/{event.event_id}").status_code == 404
    assert client.get("/subscribe/1/activity/unknown").status_code == 404
