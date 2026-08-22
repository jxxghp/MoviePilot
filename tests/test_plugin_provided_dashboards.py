"""插件声明仪表盘链路测试：契约校验、vue 模式组件描述、停用回收与旧钩子并存。"""

from typing import Iterator, List, Optional

import pytest

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.deprecation.notices import DeprecationNotice, DeprecationStage
from app.runtime.extensions.contract.declaration import DashboardDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager

# 一份合法的配置界面：组件树加默认数据二元组，形状与 get_form() 相同
_VALID_CONFIG_FORM = (
    [{"component": "VCard", "props": {"title": "用量"}}],
    {"used": 0},
)


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _set_notice_stage(monkeypatch, key: str, stage: DeprecationStage) -> None:
    """把指定废弃标识的登记替换为指定阶段的副本，其余登记原样保留。"""
    original = notices_module.NOTICES[key]
    updated = dict(notices_module.NOTICES)
    updated[key] = DeprecationNotice(
        key=original.key,
        subject=original.subject,
        stage=stage,
        since=original.since,
        remove_in=original.remove_in,
        replacement=original.replacement,
        reason=original.reason,
    )
    monkeypatch.setattr(notices_module, "NOTICES", updated)
    monkeypatch.setattr(deprecation_policy, "NOTICES", updated)


class _CapableDashboardPlugin:
    """声明仪表盘的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "仪表盘插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False, render_mode="vuetify"):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error
        self._render_mode = render_mode

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_render_mode(self):
        """返回预设渲染模式。"""
        return self._render_mode, ("dist/assets" if self._render_mode == "vue" else None)

    def provides_dashboards(self):
        """返回声明的仪表盘，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明仪表盘时出错")
        return self._declarations


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableDashboardPlugin(
        declarations=[
            DashboardDeclaration(key="usage", name="用量", config_form=_VALID_CONFIG_FORM)
        ]
    )
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert len(declared["DemoDashboard"]) == 1
    accepted = declared["DemoDashboard"][0]
    assert accepted.key == "usage"
    assert accepted.name == "用量"


def test_projection_accepts_bare_dict_without_wrapper():
    """插件直接交出描述字典而不包 DashboardDeclaration 的兼容写法应被接受。"""
    raw = {"key": "usage", "name": "用量"}
    plugin = _CapableDashboardPlugin(declarations=[raw])
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert declared["DemoDashboard"] == [raw]


def test_projection_accepts_empty_key_as_default_dashboard():
    """key 留空代表插件的默认仪表盘，不因此被拒绝。"""
    plugin = _CapableDashboardPlugin(
        declarations=[DashboardDeclaration(key="", name="默认仪表盘")]
    )
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert len(declared["DemoDashboard"]) == 1
    assert declared["DemoDashboard"][0].key == ""


def test_declaration_rejected_when_name_is_empty():
    """未声明非空展示名称的声明必须被拒绝。"""
    plugin = _CapableDashboardPlugin(declarations=[DashboardDeclaration(key="usage", name="")])
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert declared["DemoDashboard"] == []


def test_declaration_rejected_when_component_tree_is_not_list():
    """config_form 的组件树不是 list 时，整条仪表盘声明被拒。"""
    plugin = _CapableDashboardPlugin(
        declarations=[
            DashboardDeclaration(
                key="usage", name="用量", config_form=("not-a-list", {"used": 0})
            )
        ]
    )
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert declared["DemoDashboard"] == []


def test_declaration_rejected_when_both_config_form_and_config_component_given():
    """config_form 与 config_component 同时声明时意图不明，整条声明被拒。"""
    plugin = _CapableDashboardPlugin(
        render_mode="vue",
        declarations=[
            DashboardDeclaration(
                key="usage",
                name="用量",
                config_form=_VALID_CONFIG_FORM,
                config_component="UsageDashboard",
            )
        ],
    )
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert declared["DemoDashboard"] == []


def test_declaration_rejected_when_vuetify_extension_declares_config_component():
    """渲染模式为 vuetify 的扩展声明 config_component 属于矛盾声明，被拒。"""
    plugin = _CapableDashboardPlugin(
        render_mode="vuetify",
        declarations=[
            DashboardDeclaration(key="usage", name="用量", config_component="UsageDashboard")
        ],
    )
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert declared["DemoDashboard"] == []


def test_projection_partial_rejection_keeps_valid_siblings():
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableDashboardPlugin(
        declarations=[
            DashboardDeclaration(key="ok", name="OK"),
            DashboardDeclaration(key="bad", name=""),
        ]
    )
    projection = PluginProjection({"DemoDashboard": plugin})

    declared = projection.provided_dashboards()

    assert len(declared["DemoDashboard"]) == 1
    assert declared["DemoDashboard"][0].key == "ok"


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明仪表盘抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableDashboardPlugin(raise_error=True)
    healthy = _CapableDashboardPlugin(declarations=[DashboardDeclaration(key="ok", name="OK")])
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_dashboards()

    assert "Broken" not in declared
    assert declared["Ok"][0].key == "ok"


class _FakeDashboardPlugin:
    """既声明新式仪表盘又实现旧式钩子的插件桩，用于驱动聚合器完整链路。"""

    plugin_name = "假想仪表盘插件"
    plugin_version = "1.0.0"

    def __init__(
        self,
        declared: Optional[List[DashboardDeclaration]] = None,
        legacy: Optional[list] = None,
        state: bool = True,
        render_mode: str = "vuetify",
    ):
        self._declared = declared or []
        self._legacy = legacy
        self._state = state
        self._render_mode = render_mode
        self.legacy_calls: List[int] = []

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._state

    def get_render_mode(self):
        """返回预设渲染模式。"""
        return self._render_mode, ("dist/assets" if self._render_mode == "vue" else None)

    def get_dashboard(self, key: str = None, user_agent: str = None):
        """仪表盘数据获取钩子，测试只需其存在以通过投影的前置门槛。"""
        return {}, {}, []

    def provides_dashboards(self):
        """返回声明的仪表盘。"""
        return self._declared

    def get_dashboard_meta(self):
        """返回旧式裸元信息列表并记录调用次数；未配置时返回 None 表示未提供。"""
        if self._legacy is None:
            return None
        self.legacy_calls.append(1)
        return self._legacy


def test_dashboard_metadata_prefers_declared_over_legacy_when_both_present(
    plugin_manager: PluginManager,
) -> None:
    """同一实例两条来源皆有声明时，声明式登记优先生效，旧钩子不再被取用。"""
    plugin = _FakeDashboardPlugin(
        declared=[DashboardDeclaration(key="new", name="新式仪表盘")],
        legacy=[{"key": "legacy", "name": "旧式仪表盘"}],
    )
    plugin_manager.running_plugins["Demo"] = plugin

    metadata = plugin_manager.get_plugin_dashboard_meta()

    keys = {item["key"] for item in metadata}
    assert keys == {"new"}
    assert plugin.legacy_calls == []


def test_dashboard_metadata_falls_back_to_single_default_dashboard(
    plugin_manager: PluginManager,
) -> None:
    """插件两条来源都未实现时退化为单一默认仪表盘，与既有行为一致。"""
    plugin_manager.running_plugins["Demo"] = _FakeDashboardPlugin()

    metadata = plugin_manager.get_plugin_dashboard_meta()

    assert metadata == [{
        "id": "Demo",
        "name": "假想仪表盘插件",
        "key": "",
        "instance_id": "default",
        "instance_key": "Demo",
    }]


def test_dashboard_metadata_returns_component_and_remote_for_vue_mode_declaration(
    plugin_manager: PluginManager,
) -> None:
    """vue 模式声明登记后，元信息附带组件名与联邦远程入口描述。"""
    plugin = _FakeDashboardPlugin(
        declared=[
            DashboardDeclaration(key="usage", name="用量", config_component="UsageDashboard")
        ],
        render_mode="vue",
    )
    plugin_manager.running_plugins["Demo"] = plugin

    metadata = plugin_manager.get_plugin_dashboard_meta()

    assert len(metadata) == 1
    entry = metadata[0]
    assert entry["component"] == "UsageDashboard"
    assert entry["remote"] is not None
    assert entry["remote"]["id"] == "Demo"
    assert entry["remote"]["name"] == "假想仪表盘插件"


def test_get_plugin_dashboard_meta_emits_deprecation_warning_for_legacy_hook(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """触达旧式 get_dashboard_meta() 时必须触发一次废弃告警，重复触达不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin_manager.running_plugins["Demo"] = _FakeDashboardPlugin(
        legacy=[{"key": "legacy", "name": "旧式仪表盘"}]
    )

    plugin_manager.get_plugin_dashboard_meta()
    plugin_manager.get_plugin_dashboard_meta()

    assert len(emitted) == 1
    assert "get_dashboard_meta" in emitted[0]


def test_legacy_hook_stops_at_disabled_stage_and_resumes_via_override(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """阶段推进到 DISABLED 时旧钩子真的停用（退化为默认仪表盘）；标识列入
    DEPRECATION_ENABLED 能恢复旧钩子声明的多仪表盘列表。
    """
    plugin_manager.running_plugins["Demo"] = _FakeDashboardPlugin(
        legacy=[{"key": "legacy", "name": "旧式仪表盘"}]
    )

    # 阶段一（默认登记）：旧钩子照常生效
    metadata = plugin_manager.get_plugin_dashboard_meta()
    assert {item["key"] for item in metadata} == {"legacy"}

    # 阶段二：默认停用，退化为单一默认仪表盘
    _set_notice_stage(monkeypatch, "plugin.get_dashboard_meta", DeprecationStage.DISABLED)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    metadata = plugin_manager.get_plugin_dashboard_meta()
    assert metadata == [{
        "id": "Demo",
        "name": "假想仪表盘插件",
        "key": "",
        "instance_id": "default",
        "instance_key": "Demo",
    }]

    # 阶段二 + 标识列入 DEPRECATION_ENABLED：临时恢复
    monkeypatch.setattr(
        deprecation_policy, "_enabled_keys", lambda: frozenset({"plugin.get_dashboard_meta"})
    )
    metadata = plugin_manager.get_plugin_dashboard_meta()
    assert {item["key"] for item in metadata} == {"legacy"}
