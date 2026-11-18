"""
验证站点认证信息处理与认证流程。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.endpoints import site as site_endpoint
from app.application.site.auth import (
    normalize_site_auth_config,
    normalize_site_auth_params,
)
from app.scheduler import reconcile as scheduler_reconcile
from app.schemas.site import SiteAuth
from app.schemas.types import SystemConfigKey
from app.startup.initializers import modules as module_initializer

HAIDAN_AUTH_SITES = {
    "haidan": {
        "params": {
            "id": {"name": "用户ID"},
            "passkey": {"name": "密钥"},
        }
    }
}

AUTH_SITES = {
    **HAIDAN_AUTH_SITES,
    "hdkyl": {"params": {"uid": {"name": "用户ID"}, "passkey": {"name": "密钥"}}},
}


def test_normalization_removes_other_site_credentials_without_mutating_input() -> None:
    """
    切换站点遗留的前缀凭据被过滤，当前站点和未知扩展字段继续兼容。
    """
    params = {"id": 123, "passkey": "current", "HDKYL_UID": 456, "HDKYL_PASSKEY": "old", "extra": "value"}
    original = dict(params)

    assert normalize_site_auth_params("haidan", params, AUTH_SITES) == {
        "HAIDAN_ID": 123, "HAIDAN_PASSKEY": "current", "extra": "value",
    }
    assert params == original


@pytest.mark.parametrize("site,params", [(None, None), ("unknown", {"field": "value"})])
def test_unrecognized_auth_configuration_keeps_legacy_parameters(site, params) -> None:
    """
    缺失或尚未识别的资源配置不能删除调用方提供的扩展参数。
    """
    assert normalize_site_auth_params(site, params, AUTH_SITES) == (params or {})


def test_normalize_site_auth_params_accepts_resource_field_names() -> None:
    """
    站点原始字段名应转换为认证资源读取的环境变量式键名。
    """
    assert normalize_site_auth_params(
        "haidan",
        {"id": 63632, "passkey": "secret"},
        HAIDAN_AUTH_SITES,
    ) == {"HAIDAN_ID": 63632, "HAIDAN_PASSKEY": "secret"}


def test_normalize_site_auth_config_preserves_unknown_fields() -> None:
    """
    认证配置中的扩展字段不能因参数规范化而丢失。
    """
    assert normalize_site_auth_config(
        {
            "site": "haidan",
            "params": {"id": 63632, "passkey": "secret", "extra": "value"},
        },
        HAIDAN_AUTH_SITES,
    ) == {
        "site": "haidan",
        "params": {
            "HAIDAN_ID": 63632,
            "HAIDAN_PASSKEY": "secret",
            "extra": "value",
        },
    }


def _patch_auth_endpoint(monkeypatch, helper: Mock, config: Mock) -> None:
    """
    替换认证端点的外部运行时依赖。
    """
    monkeypatch.setattr(site_endpoint, "SitesHelper", Mock(return_value=helper))
    monkeypatch.setattr(site_endpoint, "get_configured_system_config", Mock(return_value=config))
    monkeypatch.setattr(site_endpoint, "get_plugin_manager", Mock(return_value=Mock()))
    monkeypatch.setattr(site_endpoint, "get_scheduler", Mock(return_value=Mock()))
    monkeypatch.setattr(site_endpoint, "init_commands", Mock())
    monkeypatch.setattr(site_endpoint, "register_plugin_api", Mock())


def test_auth_site_normalizes_parameters_before_check_and_persist(monkeypatch) -> None:
    """
    认证端点应以规范化键名调用资源并只持久化成功配置。
    """
    helper = Mock()
    helper.get_authsites.return_value = AUTH_SITES
    helper.check_user.return_value = (True, "haidan")
    config = Mock()
    _patch_auth_endpoint(monkeypatch, helper, config)

    response = site_endpoint.auth_site(
        SiteAuth(site="haidan", params={"id": 63632, "passkey": "secret", "HDKYL_PASSKEY": "old"}),
        SimpleNamespace(),
    )

    assert response.success is True
    helper.check_user.assert_called_once_with(
        "haidan",
        {"HAIDAN_ID": 63632, "HAIDAN_PASSKEY": "secret"},
    )
    config.set.assert_called_once_with(
        SystemConfigKey.UserSiteAuthParams,
        {
            "site": "haidan",
            "params": {"HAIDAN_ID": 63632, "HAIDAN_PASSKEY": "secret"},
        },
    )


def test_auth_site_does_not_persist_failed_parameters(monkeypatch) -> None:
    """
    失败认证不能覆盖已有配置并屏蔽环境变量认证。
    """
    helper = Mock()
    helper.get_authsites.return_value = HAIDAN_AUTH_SITES
    helper.check_user.return_value = (False, "api parameter loss")
    config = Mock()
    _patch_auth_endpoint(monkeypatch, helper, config)

    response = site_endpoint.auth_site(
        SiteAuth(site="haidan", params={"id": 63632, "passkey": "secret"}),
        SimpleNamespace(),
    )

    assert response.success is False
    config.set.assert_not_called()


def test_startup_auth_prefers_environment_configuration(monkeypatch) -> None:
    """
    设置环境变量时启动认证应从环境读取，而不是使用旧持久化配置。
    """
    helper = Mock()
    helper.auth_level = 1
    helper.get_authsites.return_value = HAIDAN_AUTH_SITES
    helper.check_user.return_value = (True, "haidan")
    config = Mock()
    config.get.return_value = {
        "site": "haidan",
        "params": {"id": 63632, "passkey": "stale"},
    }

    monkeypatch.setattr(module_initializer, "SitesHelper", Mock(return_value=helper))
    monkeypatch.setattr(
        module_initializer,
        "get_configured_system_config",
        Mock(return_value=config),
    )
    monkeypatch.setattr(module_initializer, "get_runtime_setting", lambda key: "haidan")

    module_initializer.user_auth()

    helper.check_user.assert_called_once_with()


def test_startup_auth_filters_other_site_credentials_in_saved_configuration(monkeypatch) -> None:
    """
    启动时只按保存的站点认证，混存的其他站点前缀不能进入资源调用。
    """
    helper = Mock(auth_level=1)
    helper.get_authsites.return_value = AUTH_SITES
    helper.check_user.return_value = (False, "请求超时")
    config = Mock()
    config.get.return_value = {
        "site": "haidan",
        "params": {"id": 123, "passkey": "current", "HDKYL_UID": 456, "HDKYL_PASSKEY": "old"},
    }
    monkeypatch.setattr(module_initializer, "SitesHelper", Mock(return_value=helper))
    monkeypatch.setattr(module_initializer, "get_configured_system_config", Mock(return_value=config))
    monkeypatch.setattr(module_initializer, "get_runtime_setting", lambda _key: "")

    module_initializer.user_auth()

    helper.check_user.assert_called_once_with(
        site="haidan", params={"HAIDAN_ID": 123, "HAIDAN_PASSKEY": "current"},
    )
    config.set.assert_not_called()


def test_scheduler_auth_prefers_environment_configuration(monkeypatch) -> None:
    """
    定时认证应与启动认证使用相同的环境变量优先级。
    """
    helper = Mock()
    helper.auth_level = 1
    helper.get_authsites.return_value = HAIDAN_AUTH_SITES
    helper.check_user.return_value = (True, "haidan")
    config = Mock()
    config.get.return_value = {
        "site": "haidan",
        "params": {"id": 63632, "passkey": "stale"},
    }
    scheduler_config = SimpleNamespace(site_link="")
    owner = scheduler_reconcile.SchedulerReconcileOwner()
    owner._auth_count = 0
    owner._auth_message = False
    owner._auth_plugin_routes_pending = False
    owner._scheduler_services = Mock(return_value=Mock())
    owner.init_plugin_jobs = Mock()

    monkeypatch.setattr(scheduler_reconcile, "SitesHelper", Mock(return_value=helper))
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_configured_system_config",
        Mock(return_value=config),
    )
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_scheduler_runtime_config",
        Mock(return_value=scheduler_config),
    )
    monkeypatch.setattr(scheduler_reconcile, "get_runtime_setting", lambda key: "haidan")
    monkeypatch.setattr(scheduler_reconcile, "get_plugin_manager", Mock(return_value=Mock()))
    monkeypatch.setattr(scheduler_reconcile, "register_plugin_api", Mock())

    owner.user_auth()

    helper.check_user.assert_called_once_with()
