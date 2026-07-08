from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app import schemas
from app.api.endpoints import site as site_endpoint


@pytest.fixture()
def anyio_backend():
    return "asyncio"


def test_update_cookie_by_body_uses_request_body():
    """
    POST 更新站点 Cookie 时应从请求体读取登录参数。
    """
    fake_site = SimpleNamespace(id=1, name="TestSite")
    fake_chain = Mock()
    fake_chain.update_cookie.return_value = (True, "ok")
    request = schemas.SiteCookieUpdate(username="user", password="password", code="123456")

    with patch.object(site_endpoint.Site, "get", return_value=fake_site), patch.object(
        site_endpoint, "SiteChain", return_value=fake_chain
    ):
        response = site_endpoint.update_cookie_by_body(
            site_id=1,
            site_cookie_update=request,
            db=Mock(),
            _=Mock(),
        )

    assert response.success is True
    assert response.message == "ok"
    fake_chain.update_cookie.assert_called_once_with(
        site_info=fake_site,
        username="user",
        password="password",
        two_step_code="123456",
    )


def test_update_cookie_legacy_get_keeps_query_params():
    """
    旧 GET 入口仍应兼容查询参数更新站点 Cookie。
    """
    fake_site = SimpleNamespace(id=1, name="TestSite")
    fake_chain = Mock()
    fake_chain.update_cookie.return_value = (False, "failed")

    with patch.object(site_endpoint.Site, "get", return_value=fake_site), patch.object(
        site_endpoint, "SiteChain", return_value=fake_chain
    ):
        response = site_endpoint.update_cookie(
            site_id=1,
            username="user",
            password="password",
            code=None,
            db=Mock(),
            _=Mock(),
        )

    assert response.success is False
    assert response.message == "failed"
    fake_chain.update_cookie.assert_called_once_with(
        site_info=fake_site,
        username="user",
        password="password",
        two_step_code=None,
    )


@pytest.mark.anyio
async def test_add_site_honors_indexer_default_disabled_status(monkeypatch):
    """
    资源包声明默认关闭时，新增站点应保存为未启用。
    """
    created_site = {}

    class FakeSitesHelper:
        auth_level = 2

        async def async_get_indexer(self, domain):
            assert domain == "anoneko.com"
            return {
                "name": "动漫花园",
                "public": True,
                "is_active": False,
            }

    class FakeSite:
        @staticmethod
        async def async_get_by_domain(db, domain):
            assert domain == "anoneko.com"
            return None

        def __init__(self, **kwargs):
            created_site.update(kwargs)

        def create(self, db):
            created_site["created"] = True

    send_event = AsyncMock()
    monkeypatch.setattr(site_endpoint, "SitesHelper", FakeSitesHelper)
    monkeypatch.setattr(site_endpoint, "Site", FakeSite)
    monkeypatch.setattr(site_endpoint.eventmanager, "async_send_event", send_event)

    response = await site_endpoint.add_site(
        db=Mock(),
        site_in=schemas.Site(url="https://dmhy.anoneko.com/"),
        _=Mock(),
    )

    assert response.success is True
    assert created_site["name"] == "动漫花园"
    assert created_site["public"] == 1
    assert created_site["is_active"] is False
    assert created_site["created"] is True
    send_event.assert_awaited_once()
