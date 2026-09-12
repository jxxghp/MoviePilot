import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import schemas
from app.api.endpoints import site as site_endpoint
from app.schemas.site import SiteUserData


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
    assert response.message == "failed"
    fake_chain.update_cookie.assert_called_once_with(
        site_info=fake_site,
        username="user",
        password="password",
        two_step_code=None,
    )


def test_set_cookie_by_body_persists_only_browser_cookie_fields():
    """浏览器取得 Cookie 后的精细 API 不应要求或覆盖完整站点配置。"""
    command = Mock()
    command.set_cookie = AsyncMock(
        return_value=SimpleNamespace(success=True, message="saved")
    )
    request = schemas.SiteCookieSet(cookie="sid=browser", ua="Browser UA")

    response = asyncio.run(
        site_endpoint.set_cookie_by_body(
            site_id=7,
            site_cookie_set=request,
            command=command,
            _=Mock(),
        )
    )

    assert response.success is True
    assert response.message == "saved"
    command.set_cookie.assert_awaited_once_with(
        site_id=7,
        cookie="sid=browser",
        ua="Browser UA",
    )


def test_refresh_userdata_reports_parser_failure_instead_of_empty_success():
    """用户解析未拿到身份时应返回业务失败，不能伪造空数据成功。"""
    fake_site = SimpleNamespace(id=1, domain="www.musopia.vip")
    fake_chain = Mock()
    fake_chain.refresh_userdata.return_value = SiteUserData(
        domain="www.musopia.vip",
        err_msg="未检测到已登陆，请检查cookies是否过期",
    )

    with patch.object(site_endpoint, "SiteChain", return_value=fake_chain), patch.object(
        site_endpoint,
        "SitesHelper",
    ) as sites_helper:
        sites_helper.return_value.get_indexer.return_value = {
            "name": "音乐乌托邦",
            "domain": "www.musopia.vip",
        }
        response = site_endpoint.refresh_userdata(
            site_id=1,
            query=SimpleNamespace(get_sync=lambda _site_id: fake_site),
            _=Mock(),
        )

    assert response.success is False
    assert response.message == "未检测到已登陆，请检查cookies是否过期"
    assert response.data is None
