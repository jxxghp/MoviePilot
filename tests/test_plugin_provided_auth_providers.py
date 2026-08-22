"""分身级登录入口旧钩子 `get_auth_providers()` 的组装与废弃门禁。

登录入口本身已并入服务实例族，配置扇出那条来源与并族守护测试同在
`tests/test_auth_service_family_merge.py`。本文件只盯住旧钩子：它随 v2 发行版到达过
社区，因此走三阶段废弃框架而不是直接删除——功能照常、触达告警一次、阶段推进到默认
关闭后整体失效、标识列入 DEPRECATION_ENABLED 可临时恢复。
"""

import dataclasses
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


class _LegacyAuthPlugin:
    """实现分身级旧钩子的插件桩。"""

    plugin_name = "旧式认证插件"

    def __init__(self, legacy=None, enabled=True):
        self._legacy = legacy
        self._enabled = enabled

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_auth_providers(self):
        """返回旧式认证提供方；未配置时返回 None 表示未提供。"""
        return self._legacy


class _VueLegacyAuthPlugin(_LegacyAuthPlugin):
    """按 vue 模式渲染的旧钩子插件桩。"""

    plugin_version = "2.0.0"

    def get_render_mode(self):
        """声明本插件按 vue 模式渲染，编译产物位于 dist/assets。"""
        return "vue", "dist/assets"


def test_legacy_hook_still_produces_entries() -> None:
    """只实现旧钩子的插件照常经投影收敛。"""
    plugin = _LegacyAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers[0]["id"] == "legacy_only_provider"


def test_legacy_hook_defaults_id_and_name_when_fields_are_omitted() -> None:
    """旧钩子未给出 id/name 时回落为 plugin:<实例键> 与插件展示名。"""
    plugin = _LegacyAuthPlugin(legacy=[{"icon": "mdi-login"}])
    projection = PluginProjection({"DemoAuth": plugin})

    providers = projection.auth_providers()

    assert providers[0]["id"] == "plugin:DemoAuth"
    assert providers[0]["name"] == "旧式认证插件"
    assert providers[0]["enabled"] is True
    assert providers[0]["instance_key"] == "DemoAuth"


def test_legacy_hook_in_vue_mode_carries_login_component_and_remote(
    monkeypatch, plugin_manager: PluginManager
) -> None:
    """vue 模式下旧钩子的入口同样带 AuthPage 组件与联邦远程入口。"""
    projection = PluginProjection(
        {"DemoAuth": _VueLegacyAuthPlugin(legacy=[{"id": "oidc"}])},
        remote_entry_factory=lambda key, dist, version: f"/plugin/file/{key}/{dist}",
    )

    providers = projection.auth_providers()

    assert providers[0]["component"] == "AuthPage"
    assert providers[0]["remote"]["id"] == "DemoAuth"
    assert providers[0]["remote"]["version"] == _VueLegacyAuthPlugin.plugin_version


def test_legacy_hook_emits_deprecation_warning_once(monkeypatch) -> None:
    """触达旧式 get_auth_providers() 必须触发一次废弃告警，重复投影不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin = _LegacyAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    projection.auth_providers()
    projection.auth_providers()

    hits = [m for m in emitted if "get_auth_providers" in m]
    assert len(hits) == 1


def test_legacy_hook_points_at_the_service_instance_replacement(monkeypatch) -> None:
    """废弃告警要指向并族后的替代写法，而不是已经删掉的那条声明钩子。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    projection = PluginProjection({"Demo": _LegacyAuthPlugin(legacy=[{"id": "x"}])})

    projection.auth_providers()

    hits = [m for m in emitted if "get_auth_providers" in m]
    assert "provides_service_instances()" in hits[0]
    assert "provides_auth_providers" not in hits[0]


def test_legacy_hook_stops_when_stage_disabled(monkeypatch) -> None:
    """废弃阶段推进到 DISABLED 后，旧钩子不再对组装结果生效。"""
    key = "plugin.get_auth_providers"
    disabled_notice = dataclasses.replace(
        notices_module.NOTICES[key], stage=notices_module.DeprecationStage.DISABLED
    )
    monkeypatch.setitem(notices_module.NOTICES, key, disabled_notice)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    plugin = _LegacyAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers == []


def test_legacy_hook_restored_via_deprecation_enabled(monkeypatch) -> None:
    """DISABLED 阶段下标识被列入 DEPRECATION_ENABLED 后，旧钩子应恢复生效。"""
    key = "plugin.get_auth_providers"
    disabled_notice = dataclasses.replace(
        notices_module.NOTICES[key], stage=notices_module.DeprecationStage.DISABLED
    )
    monkeypatch.setitem(notices_module.NOTICES, key, disabled_notice)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", lambda: frozenset({key}))
    plugin = _LegacyAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers[0]["id"] == "legacy_only_provider"


def test_legacy_hook_exception_does_not_break_other_plugins() -> None:
    """旧钩子抛异常时不应阻断其它插件的入口。"""

    class _BrokenLegacyPlugin(_LegacyAuthPlugin):
        """旧钩子抛异常的插件桩。"""

        def get_auth_providers(self):
            """模拟旧钩子实现出错。"""
            raise RuntimeError("获取认证提供方时出错")

    projection = PluginProjection({
        "Broken": _BrokenLegacyPlugin(),
        "Ok": _LegacyAuthPlugin(legacy=[{"id": "ok_provider"}]),
    })

    providers = projection.auth_providers()

    assert [provider["id"] for provider in providers] == ["ok_provider"]


def test_disabled_plugin_contributes_no_legacy_entry() -> None:
    """停用的插件不产出登录入口。"""
    plugin = _LegacyAuthPlugin(legacy=[{"id": "legacy_only_provider"}], enabled=False)
    projection = PluginProjection({"Demo": plugin})

    assert projection.auth_providers() == []
