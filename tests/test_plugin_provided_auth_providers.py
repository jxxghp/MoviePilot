"""插件声明登录认证提供方链路测试：契约校验、vue 模式配置界面与旧钩子废弃门禁。"""

import dataclasses
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.declaration import AuthProviderDeclaration
from app.runtime.extensions.plugin import auth_provider_capabilities as auth_provider_contract
from app.runtime.extensions.plugin.projection import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager

# 一份合法的配置界面：组件树加默认数据二元组，形状与 get_form() 相同
_VALID_CONFIG_FORM = (
    [{"component": "VTextField", "props": {"model": "client_id", "label": "Client ID"}}],
    {"client_id": ""},
)


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


# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


def test_contract_accepts_declaration_without_config_surface() -> None:
    """未声明任何配置界面的声明合规。"""
    declaration = AuthProviderDeclaration(id="oidc", name="OIDC 登录")

    assert auth_provider_contract.auth_provider_declaration_violation(declaration) is None


def test_contract_accepts_bare_dict_declaration() -> None:
    """插件直接交出字段字典而不包 AuthProviderDeclaration 时同样合规。"""
    violation = auth_provider_contract.auth_provider_declaration_violation(
        {"id": "oidc", "name": "OIDC 登录"}
    )

    assert violation is None


def test_contract_rejects_declaration_with_invalid_shape() -> None:
    """既不是 AuthProviderDeclaration 也不是字典的声明必须被拒绝。"""
    violation = auth_provider_contract.auth_provider_declaration_violation("not-a-declaration")

    assert violation is not None


def test_contract_accepts_valid_vuetify_config_form() -> None:
    """形状合法的 config_form 声明合规。"""
    declaration = AuthProviderDeclaration(id="oidc", config_form=_VALID_CONFIG_FORM)

    assert auth_provider_contract.auth_provider_declaration_violation(declaration) is None


def test_contract_rejects_config_form_with_non_list_layout() -> None:
    """config_form 的组件树不是 list 时整条声明被拒。"""
    declaration = AuthProviderDeclaration(
        id="oidc", config_form=("not-a-list", {"client_id": ""})
    )

    violation = auth_provider_contract.auth_provider_declaration_violation(declaration)

    assert violation is not None
    assert "组件树" in violation


def test_contract_rejects_config_form_with_non_dict_defaults() -> None:
    """config_form 的默认数据不是 dict 时整条声明被拒。"""
    declaration = AuthProviderDeclaration(
        id="oidc", config_form=([{"component": "VTextField"}], "not-a-dict")
    )

    violation = auth_provider_contract.auth_provider_declaration_violation(declaration)

    assert violation is not None
    assert "默认数据" in violation


def test_contract_rejects_both_config_form_and_config_component_given() -> None:
    """config_form 与 config_component 同时声明时意图不明，整条声明被拒。"""
    declaration = AuthProviderDeclaration(
        id="oidc", config_form=_VALID_CONFIG_FORM, config_component="OidcConfig"
    )

    violation = auth_provider_contract.auth_provider_declaration_violation(
        declaration, render_mode="vue"
    )

    assert violation is not None
    assert "二选一" in violation


def test_contract_rejects_vuetify_extension_declaring_config_component() -> None:
    """渲染模式为 vuetify 的扩展声明 config_component 属于矛盾声明，被拒。"""
    declaration = AuthProviderDeclaration(id="oidc", config_component="OidcConfig")

    violation = auth_provider_contract.auth_provider_declaration_violation(
        declaration, render_mode="vuetify"
    )

    assert violation is not None
    assert "vue" in violation


def test_contract_accepts_config_component_when_render_mode_is_vue() -> None:
    """渲染模式为 vue 的扩展声明 config_component 时合规。"""
    declaration = AuthProviderDeclaration(id="oidc", config_component="OidcConfig")

    violation = auth_provider_contract.auth_provider_declaration_violation(
        declaration, render_mode="vue"
    )

    assert violation is None


# ---------------------------------------------------------------------------
# PluginProjection.provided_auth_providers() 投影
# ---------------------------------------------------------------------------


class _CapableAuthPlugin:
    """声明认证提供方的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "认证插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False, render_mode=None):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error
        self._render_mode = render_mode

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_auth_providers(self):
        """返回声明的认证提供方，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明认证提供方时出错")
        return self._declarations

    def get_render_mode(self):
        """返回声明方渲染模式，缺省视为 vuetify。"""
        return self._render_mode or "vuetify", "dist/assets"


def test_projection_accepts_valid_declaration() -> None:
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableAuthPlugin(
        declarations=[AuthProviderDeclaration(id="oidc", name="OIDC 登录")]
    )
    projection = PluginProjection({"DemoAuth": plugin})

    declared = projection.provided_auth_providers()

    assert len(declared["DemoAuth"]) == 1
    assert declared["DemoAuth"][0].id == "oidc"


def test_projection_accepts_bare_dict_without_wrapper() -> None:
    """插件直接交出字段字典而不包 AuthProviderDeclaration 的兼容写法应被接受。"""
    plugin = _CapableAuthPlugin(declarations=[{"id": "oidc", "name": "OIDC 登录"}])
    projection = PluginProjection({"DemoAuth": plugin})

    declared = projection.provided_auth_providers()

    assert declared["DemoAuth"] == [{"id": "oidc", "name": "OIDC 登录"}]


def test_projection_rejects_declaration_with_conflicting_config_surface() -> None:
    """config_form 与 config_component 同时声明的条目在投影层同样被拒绝。"""
    plugin = _CapableAuthPlugin(
        declarations=[
            AuthProviderDeclaration(
                id="oidc", config_form=_VALID_CONFIG_FORM, config_component="OidcConfig"
            )
        ]
    )
    projection = PluginProjection({"DemoAuth": plugin})

    declared = projection.provided_auth_providers()

    assert declared["DemoAuth"] == []


def test_projection_rejects_config_component_when_render_mode_is_vuetify() -> None:
    """渲染模式为 vuetify 时声明 config_component 的条目在投影层被拒绝。"""
    plugin = _CapableAuthPlugin(
        declarations=[AuthProviderDeclaration(id="oidc", config_component="OidcConfig")],
        render_mode="vuetify",
    )
    projection = PluginProjection({"DemoAuth": plugin})

    declared = projection.provided_auth_providers()

    assert declared["DemoAuth"] == []


def test_projection_partial_rejection_keeps_valid_siblings() -> None:
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableAuthPlugin(
        declarations=[
            AuthProviderDeclaration(id="ok_provider"),
            AuthProviderDeclaration(
                id="bad_provider",
                config_form=_VALID_CONFIG_FORM,
                config_component="X",
            ),
        ]
    )
    projection = PluginProjection({"DemoAuth": plugin})

    declared = projection.provided_auth_providers()

    assert len(declared["DemoAuth"]) == 1
    assert declared["DemoAuth"][0].id == "ok_provider"


def test_projection_swallows_plugin_exception_without_blocking_others() -> None:
    """单个插件声明认证提供方抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableAuthPlugin(raise_error=True)
    healthy = _CapableAuthPlugin(declarations=[AuthProviderDeclaration(id="ok_provider")])
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_auth_providers()

    assert "Broken" not in declared
    assert declared["Ok"][0].id == "ok_provider"


# ---------------------------------------------------------------------------
# auth_providers() 组装：默认值、vue 组件+远程描述、旧钩子合并与废弃门禁
# ---------------------------------------------------------------------------


class _VueAuthPlugin:
    """vue 渲染模式下声明认证提供方专属配置组件的插件桩。"""

    plugin_name = "Vue认证插件"
    plugin_version = "2.0.0"

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def get_render_mode(self):
        """声明本插件按 vue 模式渲染，编译产物位于 dist/assets。"""
        return "vue", "dist/assets"

    def provides_auth_providers(self):
        """声明本插件提供的认证提供方，附带专属 vue 配置组件名。"""
        return [
            AuthProviderDeclaration(
                id="oidc", name="OIDC 登录", config_component="OidcProviderConfig"
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_auth_providers_vue_mode_declaration_carries_component_name_and_remote(
    monkeypatch, plugin_manager: PluginManager
):
    """vue 模式声明登记后，组装结果须带登录入口组件、专属配置组件名与联邦远程入口。"""
    plugin_id = _VueAuthPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_VueAuthPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    providers = plugin_manager.get_plugin_auth_providers()

    assert len(providers) == 1
    provider = providers[0]
    assert provider["id"] == "oidc"
    assert provider["name"] == "OIDC 登录"
    # 登录入口本身在 vue 模式下固定渲染为 AuthPage，随 component+remote 机制注入
    assert provider["component"] == "AuthPage"
    assert provider["remote"]["id"] == plugin_id
    assert provider["remote"]["version"] == _VueAuthPlugin.plugin_version
    # 专属配置界面另携带组件名与联邦远程入口，二者归属声明本身
    assert provider["config_component"]["component"] == "OidcProviderConfig"
    assert provider["config_component"]["remote"]["id"] == plugin_id

    plugin_manager.stop(plugin_id)


class _VuetifyFormAuthPlugin:
    """vuetify 渲染模式下声明认证提供方专属配置表单的插件桩。"""

    plugin_name = "Vuetify认证插件"
    plugin_version = "1.0.0"

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_auth_providers(self):
        """声明本插件提供的认证提供方，附带专属 vuetify 配置表单。"""
        return [AuthProviderDeclaration(id="basic", config_form=_VALID_CONFIG_FORM)]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_auth_providers_vuetify_mode_declaration_carries_config_form(
    monkeypatch, plugin_manager: PluginManager
):
    """vuetify 模式声明登记后，组装结果须原样带出该提供方的专属配置表单。"""
    plugin_id = _VuetifyFormAuthPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_VuetifyFormAuthPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    providers = plugin_manager.get_plugin_auth_providers()

    assert len(providers) == 1
    assert providers[0]["config_form"] == _VALID_CONFIG_FORM
    assert "config_component" not in providers[0]

    plugin_manager.stop(plugin_id)


def test_auth_providers_defaults_id_and_name_when_declaration_omits_them() -> None:
    """声明未给出 id/name 时应回落为 plugin:<实例键> 与插件展示名。"""
    plugin = _CapableAuthPlugin(declarations=[AuthProviderDeclaration()])
    projection = PluginProjection({"DemoAuth": plugin})

    providers = projection.auth_providers()

    assert len(providers) == 1
    assert providers[0]["id"] == "plugin:DemoAuth"
    assert providers[0]["name"] == "认证插件"
    assert providers[0]["enabled"] is True
    assert providers[0]["instance_key"] == "DemoAuth"


# ---------------------------------------------------------------------------
# 与旧钩子 get_auth_providers() 的合并、废弃告警与门禁
# ---------------------------------------------------------------------------


class _DualSourceAuthPlugin:
    """同时实现新旧两种认证提供方钩子的插件桩。"""

    plugin_name = "双来源认证插件"

    def __init__(self, declared=None, legacy=None, enabled=True):
        self._declared = declared or []
        self._legacy = legacy
        self._enabled = enabled

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_auth_providers(self):
        """返回声明式认证提供方。"""
        return self._declared

    def get_auth_providers(self):
        """返回旧式认证提供方；未配置时返回 None 表示未提供。"""
        return self._legacy


def test_auth_providers_merges_declared_and_legacy_sources() -> None:
    """同一实例的声明式与旧式认证提供方应合并到同一份组装结果中。"""
    plugin = _DualSourceAuthPlugin(
        declared=[AuthProviderDeclaration(id="declared_provider")],
        legacy=[{"id": "legacy_provider", "name": "旧式登录"}],
    )
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    ids = {item["id"] for item in providers}
    assert ids == {"declared_provider", "legacy_provider"}


def test_auth_providers_legacy_only_still_works() -> None:
    """只实现旧钩子的插件照常经投影收敛，不因新增声明式钩子而失效。"""
    plugin = _DualSourceAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers[0]["id"] == "legacy_only_provider"


def test_auth_providers_legacy_hook_emits_deprecation_warning_once(monkeypatch) -> None:
    """触达旧式 get_auth_providers() 必须触发一次废弃告警，重复投影不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin = _DualSourceAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    projection.auth_providers()
    projection.auth_providers()

    hits = [m for m in emitted if "get_auth_providers" in m]
    assert len(hits) == 1


def test_auth_providers_legacy_hook_stops_when_stage_disabled(monkeypatch) -> None:
    """废弃阶段推进到 DISABLED 后，旧钩子不再对组装结果生效。"""
    key = "plugin.get_auth_providers"
    disabled_notice = dataclasses.replace(
        notices_module.NOTICES[key], stage=notices_module.DeprecationStage.DISABLED
    )
    monkeypatch.setitem(notices_module.NOTICES, key, disabled_notice)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    plugin = _DualSourceAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers == []


def test_auth_providers_legacy_hook_restored_via_deprecation_enabled(monkeypatch) -> None:
    """DISABLED 阶段下标识被列入 DEPRECATION_ENABLED 后，旧钩子应恢复生效。"""
    key = "plugin.get_auth_providers"
    disabled_notice = dataclasses.replace(
        notices_module.NOTICES[key], stage=notices_module.DeprecationStage.DISABLED
    )
    monkeypatch.setitem(notices_module.NOTICES, key, disabled_notice)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", lambda: frozenset({key}))
    plugin = _DualSourceAuthPlugin(legacy=[{"id": "legacy_only_provider"}])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers[0]["id"] == "legacy_only_provider"


def test_auth_providers_swallows_legacy_exception_without_blocking_declared() -> None:
    """旧钩子抛异常时不应阻断同一实例声明式来源的结果。"""

    class _BrokenLegacyPlugin(_DualSourceAuthPlugin):
        """旧钩子抛异常的插件桩。"""

        def get_auth_providers(self):
            """模拟旧钩子实现出错。"""
            raise RuntimeError("获取认证提供方时出错")

    plugin = _BrokenLegacyPlugin(declared=[AuthProviderDeclaration(id="declared_provider")])
    projection = PluginProjection({"Demo": plugin})

    providers = projection.auth_providers()

    assert providers == [
        provider for provider in providers if provider["id"] == "declared_provider"
    ]
    assert len(providers) == 1
