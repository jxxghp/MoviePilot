import asyncio
import io
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.endpoints import dashboard as dashboard_endpoint
from app.api.endpoints import history as history_endpoint
from app.api.endpoints import login as login_endpoint
from app.api.endpoints import plugin as plugin_endpoint
from app.api.endpoints import site as site_endpoint
from app.api.endpoints import storage as storage_endpoint
from app.api.endpoints import system as system_endpoint
from app.api.endpoints import transfer as transfer_endpoint
from app.api.endpoints import user as user_endpoint
from app.core.security import verify_resource_token
from app.db.user_oper import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.schemas.types import SystemConfigKey


def _dependency_of(func, parameter_name: str):
    """读取 FastAPI 函数参数上声明的依赖函数。"""
    return inspect.signature(func).parameters[parameter_name].default.dependency


def _build_request() -> Request:
    """构造最小测试请求。"""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/login/access-token",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


def test_system_sensitive_read_endpoints_require_superuser():
    """系统敏感读取接口必须只允许管理员访问。"""
    assert _dependency_of(system_endpoint.get_env_setting, "_") is get_current_active_superuser_async
    assert _dependency_of(system_endpoint.get_setting, "_") is get_current_active_superuser_async


def test_system_public_read_endpoints_require_active_user():
    """公开读取接口只要求登录且启用的用户。"""
    assert _dependency_of(system_endpoint.ping, "_") is get_current_active_user_async
    assert _dependency_of(system_endpoint.get_public_setting, "_") is get_current_active_user_async


def test_dashboard_endpoints_require_superuser():
    """仪表板页面相关接口必须只允许管理员访问。"""
    assert _dependency_of(dashboard_endpoint.statistic, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.storage, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.processes, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.system_info, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.downloader, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.schedule, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.transfer, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.cpu, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.memory, "_") is get_current_active_superuser
    assert _dependency_of(dashboard_endpoint.network, "_") is get_current_active_superuser


def test_plugin_dashboard_endpoints_require_superuser():
    """插件仪表板接口必须只允许管理员访问。"""
    assert _dependency_of(plugin_endpoint.plugin_dashboard_meta, "_") is get_current_active_superuser
    assert _dependency_of(plugin_endpoint.plugin_dashboard_by_key, "_") is get_current_active_superuser
    assert _dependency_of(plugin_endpoint.plugin_dashboard, "_") is get_current_active_superuser


def test_manage_page_endpoints_accept_manage_permission():
    """管理页面接口允许具备 manage 权限的普通用户访问。"""
    sync_endpoints = [
        storage_endpoint.list_files,
        storage_endpoint.mkdir,
        storage_endpoint.delete,
        storage_endpoint.download,
        storage_endpoint.image,
        storage_endpoint.rename,
        site_endpoint.update_cookie_by_body,
        site_endpoint.update_cookie,
        site_endpoint.refresh_userdata,
        history_endpoint.delete_transfer_history,
        history_endpoint.ai_redo_transfer_history,
        history_endpoint.batch_ai_redo_transfer_history,
        transfer_endpoint.match_manual_transfer_target_path,
        transfer_endpoint.manual_transfer,
        transfer_endpoint.recommend_episode_format,
    ]
    async_endpoints = [
        site_endpoint.read_sites,
        site_endpoint.add_site,
        site_endpoint.update_site,
        site_endpoint.update_sites_priority,
        site_endpoint.read_userdata_latest,
        site_endpoint.read_userdata,
        site_endpoint.site_resource,
        site_endpoint.read_site,
        site_endpoint.delete_site,
    ]

    for endpoint in sync_endpoints:
        assert _dependency_of(endpoint, "_") is get_current_active_manage_user
    for endpoint in async_endpoints:
        assert _dependency_of(endpoint, "_") is get_current_active_manage_user_async


def test_system_public_setting_allows_only_non_sensitive_keys(monkeypatch):
    """公开系统设置接口只能读取明确列入白名单的非敏感配置。"""
    calls = []

    class FakeSystemConfigOper:
        """返回测试配置值的系统配置桩。"""

        def get(self, key):
            """返回测试配置值。"""
            calls.append(key)
            return [{"path": "/downloads"}]

    monkeypatch.setattr(system_endpoint, "SystemConfigOper", FakeSystemConfigOper)

    response = asyncio.run(
        system_endpoint.get_public_setting(SystemConfigKey.Directories.value)
    )

    assert response.success is True
    assert response.data == {"value": [{"path": "/downloads"}]}
    assert calls == [SystemConfigKey.Directories]

    response = asyncio.run(system_endpoint.get_public_setting("PLUGIN_MARKET"))

    assert response.success is True
    assert response.data == {"value": system_endpoint.settings.PLUGIN_MARKET}
    assert calls == [SystemConfigKey.Directories]

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(system_endpoint.get_public_setting("API_TOKEN"))

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "配置项不存在"


def test_system_ping_returns_success():
    """服务存活检测接口返回标准成功响应。"""
    response = asyncio.run(system_endpoint.ping())

    assert response.success is True


def test_login_sets_resource_token_cookie(monkeypatch):
    """登录成功时应立即写入资源 Cookie，避免插件静态文件抢先加载失败。"""

    class FakeUserChain:
        """返回登录成功用户的用户链桩。"""

        def user_authenticate(self, username, password, mfa_code=None):
            """返回认证成功结果。"""
            return True, SimpleNamespace(
                id=1,
                name=username,
                is_superuser=False,
                avatar="",
                permissions={"discovery": True},
            )

    class FakeSystemConfigOper:
        """返回已完成向导状态的系统配置桩。"""

        def get(self, key):
            """返回测试配置值。"""
            return "1"

    form_data = SimpleNamespace(username="user", password="password")
    request = _build_request()
    response = Response()

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)
    monkeypatch.setattr(login_endpoint, "SystemConfigOper", FakeSystemConfigOper)

    token = login_endpoint.login_access_token(
        request=request,
        response=response,
        form_data=form_data,
    )

    assert token.user_id == 1
    assert token.permissions == {"discovery": True}
    assert "set-cookie" in response.headers

    resource_cookie = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    payload = verify_resource_token(resource_cookie)
    assert payload.sub == 1
    assert payload.username == "user"
    assert payload.purpose == "resource"


def test_plugin_static_file_requires_resource_token_by_default(monkeypatch):
    """普通插件静态资源必须校验资源令牌。"""
    calls = []

    class FakePluginManager:
        """返回空认证提供方的插件管理器桩。"""

        def get_plugin_auth_providers(self):
            """返回插件认证入口列表。"""
            return []

    monkeypatch.setattr(plugin_endpoint, "PluginManager", FakePluginManager)
    monkeypatch.setattr(plugin_endpoint, "verify_resource_token", lambda token: calls.append(token))

    plugin_endpoint._verify_plugin_static_file_access(
        plugin_id="DemoPlugin",
        filepath="dist/remoteEntry.js",
        resource_token="resource-token",
    )

    assert calls == ["resource-token"]


def test_plugin_auth_remote_files_allow_anonymous_bootstrap(monkeypatch):
    """插件登录认证远程组件需要允许登录前匿名加载。"""
    calls = []

    class FakePluginManager:
        """返回认证插件 remote 信息的插件管理器桩。"""

        def get_plugin_auth_providers(self):
            """返回插件认证入口列表。"""
            return [
                {
                    "remote": {
                        "id": "AuthPlugin",
                        "url": "/plugin/file/AuthPlugin/dist/remoteEntry.js",
                    }
                }
            ]

    monkeypatch.setattr(plugin_endpoint, "PluginManager", FakePluginManager)
    monkeypatch.setattr(plugin_endpoint, "verify_resource_token", lambda token: calls.append(token))

    plugin_endpoint._verify_plugin_static_file_access(
        plugin_id="AuthPlugin",
        filepath="dist/remoteEntry.js",
    )
    plugin_endpoint._verify_plugin_static_file_access(
        plugin_id="AuthPlugin",
        filepath="dist/assets/chunk.js",
    )
    plugin_endpoint._verify_plugin_static_file_access(
        plugin_id="authplugin",
        filepath="dist/assets/chunk.js",
    )

    assert calls == []


def test_upload_avatar_rejects_other_user_for_non_superuser():
    """普通用户不能通过 user_id 参数修改其他用户头像。"""
    current_user = SimpleNamespace(id=1, is_superuser=False)
    upload_file = SimpleNamespace(file=io.BytesIO(b"avatar"), filename="avatar.png")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            user_endpoint.upload_avatar(
                user_id=2,
                db=object(),
                file=upload_file,
                current_user=current_user,
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "用户权限不足"
