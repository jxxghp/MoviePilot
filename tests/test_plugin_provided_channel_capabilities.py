"""插件声明渠道能力链路测试：契约校验、登记归属、停用回收与旧钩子废弃门禁。"""

import dataclasses
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions.admission import channel as channel_capability_contract
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.notification import (
    ChannelCapabilities,
    ChannelCapability,
    ChannelCapabilityManager,
)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.fixture(autouse=True)
def _reset_extension_capability_registry() -> Iterator[None]:
    """快照并复原扩展渠道能力登记表，避免测试间相互污染。"""
    original_registrations = {
        identity: list(stack)
        for identity, stack in ChannelCapabilityManager._extension_registrations.items()
    }
    original_capabilities = dict(ChannelCapabilityManager._extension_capabilities)
    original_owners = dict(ChannelCapabilityManager._extension_owners)
    try:
        yield
    finally:
        ChannelCapabilityManager._extension_registrations.clear()
        ChannelCapabilityManager._extension_registrations.update(original_registrations)
        ChannelCapabilityManager._extension_capabilities.clear()
        ChannelCapabilityManager._extension_capabilities.update(original_capabilities)
        ChannelCapabilityManager._extension_owners.clear()
        ChannelCapabilityManager._extension_owners.update(original_owners)


@pytest.fixture(autouse=True)
def _clean_deprecation_warned() -> Iterator[None]:
    """每个用例前后都清空废弃告警去重记录，避免用例间互相掩盖。"""
    deprecation_policy.reset_warned()
    yield
    deprecation_policy.reset_warned()


# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


def test_contract_accepts_valid_declaration() -> None:
    """渠道标识非空、能力集合形状合法时声明合规。"""
    declaration = ChannelCapabilities(
        channel="demo_channel", capabilities={ChannelCapability.MARKDOWN}
    )

    assert channel_capability_contract.channel_capability_declaration_violation(
        declaration
    ) is None


def test_contract_rejects_non_channel_capabilities_instance() -> None:
    """声明不是 ChannelCapabilities 实例时必须被拒绝。"""
    violation = channel_capability_contract.channel_capability_declaration_violation(
        {"channel": "demo_channel", "capabilities": {"inline_buttons"}}
    )

    assert violation is not None
    assert "不是 ChannelCapabilities 声明" in violation


def test_contract_rejects_blank_channel_identity() -> None:
    """渠道标识缺失或为空白的声明必须被拒绝。"""
    declaration = ChannelCapabilities(channel="  ", capabilities=set())

    violation = channel_capability_contract.channel_capability_declaration_violation(
        declaration
    )

    assert violation is not None
    assert "渠道标识" in violation


def test_contract_rejects_non_iterable_capabilities_shape() -> None:
    """capabilities 不是集合形状时必须被拒绝，而不是被当作字符串悄悄迭代。"""
    declaration = ChannelCapabilities(channel="demo_channel", capabilities="markdown")

    violation = channel_capability_contract.channel_capability_declaration_violation(
        declaration
    )

    assert violation is not None
    assert "capabilities" in violation


def test_contract_rejects_capabilities_with_non_enum_member() -> None:
    """capabilities 含非 ChannelCapability 成员的声明必须被拒绝。"""
    declaration = ChannelCapabilities(channel="demo_channel", capabilities={"markdown"})

    violation = channel_capability_contract.channel_capability_declaration_violation(
        declaration
    )

    assert violation is not None
    assert "非 ChannelCapability" in violation


# ---------------------------------------------------------------------------
# PluginProjection.provided_channel_capabilities() 投影
# ---------------------------------------------------------------------------


class _CapableChannelPlugin:
    """声明渠道能力的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "渠道插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_channel_capabilities(self):
        """返回声明的渠道能力，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明渠道能力时出错")
        return self._declarations


def test_projection_accepts_valid_declaration() -> None:
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableChannelPlugin(
        declarations=[
            ChannelCapabilities(
                channel="demo_channel", capabilities={ChannelCapability.MARKDOWN}
            )
        ]
    )
    projection = PluginProjection({"DemoChannel": plugin})

    declared = projection.provided_channel_capabilities()

    assert len(declared["DemoChannel"]) == 1
    assert declared["DemoChannel"][0].channel == "demo_channel"


def test_projection_partial_rejection_keeps_valid_siblings() -> None:
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableChannelPlugin(
        declarations=[
            ChannelCapabilities(channel="ok_channel", capabilities=set()),
            ChannelCapabilities(channel="", capabilities=set()),
            ChannelCapabilities(channel="bad_channel", capabilities={"not-an-enum"}),
        ]
    )
    projection = PluginProjection({"DemoChannel": plugin})

    declared = projection.provided_channel_capabilities()

    assert len(declared["DemoChannel"]) == 1
    assert declared["DemoChannel"][0].channel == "ok_channel"


def test_projection_swallows_plugin_exception_without_blocking_others() -> None:
    """单个插件声明渠道能力抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableChannelPlugin(raise_error=True)
    healthy = _CapableChannelPlugin(
        declarations=[ChannelCapabilities(channel="ok_channel", capabilities=set())]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_channel_capabilities()

    assert "Broken" not in declared
    assert declared["Ok"][0].channel == "ok_channel"


# ---------------------------------------------------------------------------
# channel_capabilities() 双来源合并
# ---------------------------------------------------------------------------


class _DualSourceChannelPlugin:
    """同时实现新旧两种渠道能力钩子的插件桩。"""

    plugin_name = "双来源渠道插件"

    def __init__(self, declared=None, legacy=None, enabled=True):
        self._declared = declared or []
        self._legacy = legacy
        self._enabled = enabled

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_channel_capabilities(self):
        """返回声明式渠道能力。"""
        return self._declared

    def get_channel_capabilities(self):
        """返回旧式渠道能力；未配置时返回 None 表示未提供。"""
        return self._legacy


def test_channel_capabilities_merges_declared_and_legacy_sources() -> None:
    """同一实例的声明式与旧式渠道能力应合并到同一份投影结果中。"""
    plugin = _DualSourceChannelPlugin(
        declared=[ChannelCapabilities(channel="declared_channel", capabilities=set())],
        legacy=[ChannelCapabilities(channel="legacy_channel", capabilities=set())],
    )
    projection = PluginProjection({"Demo": plugin})

    declared = projection.channel_capabilities()

    identities = {item.channel for item in declared["Demo"]}
    assert identities == {"declared_channel", "legacy_channel"}


def test_channel_capabilities_declared_source_wins_on_identity_overlap() -> None:
    """两条来源声明同一渠道标识时，注册到能力管理器后应以声明式登记为准。"""
    plugin = _DualSourceChannelPlugin(
        declared=[
            ChannelCapabilities(
                channel="shared_channel",
                capabilities={ChannelCapability.INLINE_BUTTONS},
                max_buttons_per_row=9,
            )
        ],
        legacy=[
            ChannelCapabilities(
                channel="shared_channel",
                capabilities=set(),
                max_buttons_per_row=2,
            )
        ],
    )
    projection = PluginProjection({"Demo": plugin})

    declared = projection.channel_capabilities()
    ChannelCapabilityManager.register_extension_capabilities("Demo", declared["Demo"])

    assert ChannelCapabilityManager.get_max_buttons_per_row("shared_channel") == 9
    assert ChannelCapabilityManager.supports_buttons("shared_channel") is True


def test_channel_capabilities_legacy_only_still_works() -> None:
    """只实现旧钩子的插件照常经投影收敛，不因新增声明式钩子而失效。"""
    plugin = _DualSourceChannelPlugin(
        legacy=[ChannelCapabilities(channel="legacy_only_channel", capabilities=set())]
    )
    projection = PluginProjection({"Demo": plugin})

    declared = projection.channel_capabilities()

    assert declared["Demo"][0].channel == "legacy_only_channel"


def test_channel_capabilities_legacy_hook_emits_deprecation_warning_once(
    monkeypatch,
) -> None:
    """触达旧式 get_channel_capabilities() 必须触发一次废弃告警，重复投影不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin = _DualSourceChannelPlugin(
        legacy=[ChannelCapabilities(channel="legacy_only_channel", capabilities=set())]
    )
    projection = PluginProjection({"Demo": plugin})

    projection.channel_capabilities()
    projection.channel_capabilities()

    overlap_messages = [m for m in emitted if "get_channel_capabilities" in m]
    assert len(overlap_messages) == 1


def test_channel_capabilities_legacy_hook_stops_when_stage_disabled(monkeypatch) -> None:
    """废弃阶段推进到 DISABLED 后，旧钩子不再对投影结果生效。"""
    key = "plugin.get_channel_capabilities"
    disabled_notice = dataclasses.replace(
        notices_module.NOTICES[key], stage=notices_module.DeprecationStage.DISABLED
    )
    monkeypatch.setitem(notices_module.NOTICES, key, disabled_notice)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    plugin = _DualSourceChannelPlugin(
        legacy=[ChannelCapabilities(channel="legacy_only_channel", capabilities=set())]
    )
    projection = PluginProjection({"Demo": plugin})

    declared = projection.channel_capabilities()

    assert "Demo" not in declared


def test_channel_capabilities_legacy_hook_restored_via_deprecation_enabled(
    monkeypatch,
) -> None:
    """DISABLED 阶段下标识被列入 DEPRECATION_ENABLED 后，旧钩子应恢复生效。"""
    key = "plugin.get_channel_capabilities"
    disabled_notice = dataclasses.replace(
        notices_module.NOTICES[key], stage=notices_module.DeprecationStage.DISABLED
    )
    monkeypatch.setitem(notices_module.NOTICES, key, disabled_notice)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", lambda: frozenset({key}))
    plugin = _DualSourceChannelPlugin(
        legacy=[ChannelCapabilities(channel="legacy_only_channel", capabilities=set())]
    )
    projection = PluginProjection({"Demo": plugin})

    declared = projection.channel_capabilities()

    assert declared["Demo"][0].channel == "legacy_only_channel"


# ---------------------------------------------------------------------------
# 插件管理器完整生命周期
# ---------------------------------------------------------------------------


class _FakeChannelPlugin:
    """声明渠道能力的插件桩，用于驱动插件管理器完整生命周期。"""

    plugin_name = "假想渠道插件"
    plugin_version = "1.0.0"
    channel_identity_value = "fake_lifecycle_channel"

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

    def provides_channel_capabilities(self):
        """返回声明的固定渠道能力。"""
        return [
            ChannelCapabilities(
                channel=self.channel_identity_value,
                capabilities={ChannelCapability.INLINE_BUTTONS},
                max_buttons_per_row=7,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_plugin_manager_lifecycle_registers_channel_capability_with_instance_key_owner(
    monkeypatch, plugin_manager: PluginManager
):
    """插件启动后应以实例键为登记方登记声明的渠道能力；停止后必须撤销，不留残留。"""
    plugin_id = _FakeChannelPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeChannelPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    assert ChannelCapabilityManager._extension_owners.get(
        "fake_lifecycle_channel"
    ) == plugin_id
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is True
    assert ChannelCapabilityManager.get_max_buttons_per_row("fake_lifecycle_channel") == 7

    plugin_manager.stop(plugin_id)

    assert "fake_lifecycle_channel" not in ChannelCapabilityManager._extension_owners
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is False


def test_plugin_manager_config_update_resyncs_channel_capability_registration(
    monkeypatch, plugin_manager: PluginManager
):
    """配置生效后停用实例应撤销登记，重新启用后登记应恢复。"""
    plugin_id = _FakeChannelPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeChannelPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is True

    plugin_obj = plugin_manager._running_plugins[plugin_id]
    plugin_obj.enabled = False
    plugin_manager.init_plugin(plugin_id, {})
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is False

    plugin_obj.enabled = True
    plugin_manager.init_plugin(plugin_id, {})
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is True


def test_plugin_manager_start_skips_channel_capability_registration_when_plugin_raises(
    monkeypatch, plugin_manager: PluginManager
):
    """插件的 provides_channel_capabilities 抛异常时不应阻断插件加载。"""

    class _BrokenChannelPlugin(_FakeChannelPlugin):
        """声明渠道能力时抛异常的插件桩。"""

        def provides_channel_capabilities(self):
            """模拟插件实现出错。"""
            raise RuntimeError("声明渠道能力时出错")

    plugin_id = _BrokenChannelPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_BrokenChannelPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    assert plugin_id in plugin_manager._running_plugins
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is False


class _ChannelFanOutPlugin:
    """按实例配置声明渠道能力或按需抛异常的插件桩，用于驱动多实例扇出。"""

    plugin_name = "渠道扇出插件"
    plugin_version = "1.0.0"

    def __init__(self):
        self.config: dict = {}

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息。"""
        self.config = config or {}

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return bool(self.config.get("enable", True))

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def provides_channel_capabilities(self):
        """按配置声明渠道能力，或按配置模拟实现出错。"""
        if self.config.get("raise_error"):
            raise RuntimeError("声明渠道能力时出错")
        channel = self.config.get("channel")
        if not channel:
            return []
        return [ChannelCapabilities(channel=channel, capabilities=set())]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_sibling_instance_exception_does_not_block_healthy_instance(
    monkeypatch, plugin_manager: PluginManager
):
    """一个实例的渠道能力声明抛异常时，兄弟实例的登记与运行态都不受影响。"""
    plugin_id = _ChannelFanOutPlugin.__name__
    second_key = f"{plugin_id}@second"
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_ChannelFanOutPlugin],
    )
    monkeypatch.setattr(
        plugin_manager, "_plugin_instance_ids", lambda pid: ["default", "second"]
    )
    monkeypatch.setattr(
        plugin_manager,
        "get_plugin_config",
        lambda pid: {
            plugin_id: {"enable": True, "channel": "fanout_default_channel"},
            second_key: {"enable": True, "raise_error": True},
        }.get(pid, {}),
    )

    plugin_manager.start(pid=plugin_id)

    assert plugin_id in plugin_manager._running_plugins
    assert second_key in plugin_manager._running_plugins
    assert ChannelCapabilityManager.supports_buttons("fanout_default_channel") is False
    assert ChannelCapabilityManager._extension_owners.get(
        "fanout_default_channel"
    ) == plugin_id
