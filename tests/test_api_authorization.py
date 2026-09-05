import asyncio
import inspect
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.deps import (
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user,
    get_current_active_user_async,
)
from app.api.endpoints import dashboard as dashboard_endpoint
from app.api.endpoints import history as history_endpoint
from app.api.endpoints import login as login_endpoint
from app.api.endpoints import plugin as plugin_endpoint
from app.api.endpoints import rule as rule_endpoint
from app.api.endpoints import site as site_endpoint
from app.api.endpoints import storage as storage_endpoint
from app.api.endpoints import system as system_endpoint
from app.api.endpoints import transfer as transfer_endpoint
from app.api.endpoints import user as user_endpoint
from app.application.security.token import decode_access_token
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
    assert _dependency_of(system_endpoint.query_settings, "_") is get_current_active_superuser_async
    assert _dependency_of(system_endpoint.update_settings, "_") is get_current_active_superuser_async
    assert _dependency_of(system_endpoint.list_database_backups, "_") is get_current_active_superuser_async
    assert _dependency_of(system_endpoint.create_database_backup, "_") is get_current_active_superuser_async
    assert _dependency_of(system_endpoint.verify_database_backup, "_") is get_current_active_superuser_async
    assert _dependency_of(system_endpoint.delete_database_backup, "_") is get_current_active_superuser_async


def test_system_public_read_endpoints_require_active_user():
    """公开读取接口只要求登录且启用的用户。"""
    assert _dependency_of(system_endpoint.ping, "_") is get_current_active_user_async
    assert _dependency_of(system_endpoint.get_public_setting, "_") is get_current_active_user_async
    assert _dependency_of(storage_endpoint.storage_options, "_") is get_current_active_user


def test_rule_query_and_mutation_endpoints_keep_separate_permissions():
    """规则查询允许活动用户，规则定义修改仍只允许管理员。"""
    read_endpoints = [
        rule_endpoint.query_builtin_rules,
        rule_endpoint.query_custom_rules,
        rule_endpoint.query_rule_groups,
    ]
    mutation_endpoints = [
        rule_endpoint.add_custom_rule,
        rule_endpoint.reorder_custom_rules,
        rule_endpoint.update_custom_rule,
        rule_endpoint.delete_custom_rule,
        rule_endpoint.add_rule_group,
        rule_endpoint.reorder_rule_groups,
        rule_endpoint.update_rule_group,
        rule_endpoint.delete_rule_group,
    ]

    for endpoint in read_endpoints:
        assert _dependency_of(endpoint, "_") is get_current_active_user_async
    for endpoint in mutation_endpoints:
        assert _dependency_of(endpoint, "_") is get_current_active_superuser_async


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
    assert _dependency_of(plugin_endpoint.plugin_capabilities, "_") is get_current_active_superuser_async
    assert _dependency_of(plugin_endpoint.plugin_data_summary, "_") is get_current_active_superuser_async
    assert _dependency_of(plugin_endpoint.reload_plugin, "_") is get_current_active_superuser


def test_site_destructive_commands_require_superuser():
    """CookieCloud 同步和站点重置必须保持超级管理员边界。"""
    assert _dependency_of(site_endpoint.cookie_cloud_sync, "_") is get_current_active_superuser_async
    assert _dependency_of(site_endpoint.reset, "_") is get_current_active_superuser_async


def test_transfer_history_clear_requires_superuser():
    """清空全部旧整理历史必须保持超级管理员边界。"""
    assert _dependency_of(history_endpoint.clear_transfer_history, "_") is get_current_active_superuser
    assert _dependency_of(history_endpoint.empty_transfer_history, "_") is get_current_active_superuser


def test_manage_page_endpoints_accept_manage_permission():
    """管理页面接口允许具备 manage 权限的普通用户访问。"""
    sync_endpoints = [
        storage_endpoint.directory_settings,
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
        site_endpoint.read_sites_by_media_type,
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

    monkeypatch.setattr(
        system_endpoint,
        "get_configured_system_config",
        lambda: FakeSystemConfigOper(),
    )

    response = asyncio.run(system_endpoint.get_public_setting(SystemConfigKey.Directories.value))

    assert response.success is True
    assert response.data == {"value": [{"path": "/downloads"}]}
    assert calls == [SystemConfigKey.Directories]

    response = asyncio.run(system_endpoint.get_public_setting("PLUGIN_MARKET"))

    assert response.success is True
    assert response.data == {"value": system_endpoint.get_runtime_settings().get("PLUGIN_MARKET")}
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
                permissions={"discovery": True, "features": {}},
            )

    form_data = SimpleNamespace(username="user", password="password")
    request = _build_request()
    response = Response()

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)
    token = login_endpoint.login_access_token(
        request=request,
        response=response,
        form_data=form_data,
    )

    assert token.user_id == 1
    assert token.permissions == {"discovery": True, "features": {}}
    assert "set-cookie" in response.headers

    resource_cookie = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    payload = decode_access_token(resource_cookie, "resource")
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

    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", FakePluginManager)
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

    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", FakePluginManager)
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
                service=SimpleNamespace(),
                file=upload_file,
                current_user=current_user,
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "用户权限不足"


def test_upload_avatar_returns_filename_in_data(monkeypatch):
    """头像上传成功时应通过 data 返回文件名，message 只保留消息文本。"""

    fake_user = SimpleNamespace()
    current_user = SimpleNamespace(id=1, is_superuser=False)
    upload_file = SimpleNamespace(file=io.BytesIO(b"avatar"), filename="avatar.png")

    class FakeService:
        """记录头像查询和更新的用户服务桩。"""

        async def get_by_id(self, user_id: int):
            """按 ID 返回测试用户。"""
            assert user_id == 1
            return fake_user

        async def update(self, user_id: int, values: dict[str, str]):
            """记录用户头像更新。"""
            assert user_id == 1
            fake_user.values = values
            return fake_user

    fake_service = FakeService()
    response = asyncio.run(
        user_endpoint.upload_avatar(
            user_id=1,
            service=fake_service,
            file=upload_file,
            current_user=current_user,
        )
    )

    assert response.success is True
    assert response.data == {"filename": "avatar.png"}
    assert response.message == ""
    assert not hasattr(response, "message_i18n")
    assert fake_user.values == {"avatar": "data:image/ico;base64,b'YXZhdGFy'"}
