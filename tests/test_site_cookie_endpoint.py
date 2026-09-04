from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import schemas
from app.api.endpoints import site as site_endpoint


def test_update_cookie_by_body_uses_request_body():
    """
    POST 更新站点 Cookie 时应从请求体读取登录参数。
    """
    fake_site = SimpleNamespace(id=1, name="TestSite")
    fake_chain = Mock()
    fake_chain.update_cookie.return_value = (True, "ok")
    request = schemas.SiteCookieUpdate(username="user", password="password", code="123456")

    with patch.object(site_endpoint, "SiteChain", return_value=fake_chain):
        response = site_endpoint.update_cookie_by_body(
            site_id=1,
            site_cookie_update=request,
            query=SimpleNamespace(get_sync=lambda _site_id: fake_site),
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

    with patch.object(site_endpoint, "SiteChain", return_value=fake_chain):
        response = site_endpoint.update_cookie(
            site_id=1,
            username="user",
            password="password",
            code=None,
            query=SimpleNamespace(get_sync=lambda _site_id: fake_site),
            _=Mock(),
        )

    assert response.success is False
    assert response.message == "操作失败，请稍后重试"
    fake_chain.update_cookie.assert_called_once_with(
        site_info=fake_site,
        username="user",
        password="password",
        two_step_code=None,
    )
