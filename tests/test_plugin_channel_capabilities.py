"""插件声明渠道能力链路测试：投影收敛、能力管理器登记与插件生命周期同步。"""

from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.notification import (
    ChannelCapabilities,
    ChannelCapability,
    ChannelCapabilityManager,
)
from app.schemas.types import NotificationChannel


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.fixture(autouse=True)
def _reset_extension_capability_registry():
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


class _CapableChannelPlugin:
    """声明渠道能力的最小插件桩。"""

    plugin_name = "能力插件"

    def __init__(self, enabled=True, capabilities=None, raise_error=False):
        self._enabled = enabled
        self._capabilities = capabilities
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_channel_capabilities(self):
        """返回声明的渠道能力，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明渠道能力时出错")
        return self._capabilities


class _FakeChannelPlugin:
    """声明渠道能力的插件桩，用于驱动插件管理器完整生命周期。"""

    plugin_name = "假想渠道插件"
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

    def get_channel_capabilities(self):
        """返回声明的固定渠道能力。"""
        return [
            ChannelCapabilities(
                channel="fake_lifecycle_channel",
                capabilities={ChannelCapability.INLINE_BUTTONS},
                max_buttons_per_row=7,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_projection_declared_capabilities_register_and_support_buttons():
    """插件声明渠道能力登记后，能力管理器应能查到按钮支持与自定义上限。"""
    plugin = _CapableChannelPlugin(
        capabilities=[
            ChannelCapabilities(
                channel="demo_bridge",
                capabilities={ChannelCapability.INLINE_BUTTONS},
                max_buttons_per_row=6,
            )
        ]
    )
    projection = PluginProjection({"DemoBridge": plugin})

    declared = projection.channel_capabilities()
    ChannelCapabilityManager.register_extension_capabilities(
        "DemoBridge", declared.get("DemoBridge", [])
    )

    assert ChannelCapabilityManager.supports_buttons("demo_bridge") is True
    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 6


def test_projection_disabled_plugin_revokes_registration():
    """插件停用后重新同步，登记应被撤销并回落到默认值。"""
    plugin = _CapableChannelPlugin(
        capabilities=[
            ChannelCapabilities(
                channel="demo_bridge",
                capabilities={ChannelCapability.INLINE_BUTTONS},
                max_buttons_per_row=6,
            )
        ]
    )
    projection = PluginProjection({"DemoBridge": plugin})
    ChannelCapabilityManager.register_extension_capabilities(
        "DemoBridge", projection.channel_capabilities().get("DemoBridge", [])
    )
    assert ChannelCapabilityManager.supports_buttons("demo_bridge") is True

    plugin._enabled = False
    ChannelCapabilityManager.register_extension_capabilities(
        "DemoBridge", projection.channel_capabilities().get("DemoBridge", [])
    )

    assert ChannelCapabilityManager.supports_buttons("demo_bridge") is False
    # 未登记渠道的按钮行上限回落到管理器缺省值
    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 2


def test_projection_skips_non_capabilities_and_empty_channel_elements():
    """非 ChannelCapabilities 元素与空渠道标识都应被跳过而非导致异常。"""
    plugin = _CapableChannelPlugin(
        capabilities=[
            {"channel": "demo_bridge", "capabilities": {"inline_buttons"}},
            ChannelCapabilities(channel="", capabilities=set()),
            ChannelCapabilities(
                channel="demo_bridge_valid",
                capabilities={ChannelCapability.MARKDOWN},
            ),
        ]
    )
    projection = PluginProjection({"DemoBridge": plugin})

    declared = projection.channel_capabilities()

    assert len(declared["DemoBridge"]) == 1
    assert declared["DemoBridge"][0].channel == "demo_bridge_valid"


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明渠道能力抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableChannelPlugin(raise_error=True)
    healthy = _CapableChannelPlugin(
        capabilities=[
            ChannelCapabilities(channel="ok_channel", capabilities=set())
        ]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.channel_capabilities()

    assert "Broken" not in declared
    assert declared["Ok"][0].channel == "ok_channel"


def test_plugin_manager_lifecycle_syncs_channel_capabilities(
    monkeypatch, plugin_manager: PluginManager
):
    """插件启动、配置生效、停止都必须同步或撤销渠道能力登记表。"""
    plugin_id = _FakeChannelPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_FakeChannelPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    # 启动加载后应登记插件声明的渠道能力
    plugin_manager.start(pid=plugin_id)
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is True
    assert ChannelCapabilityManager.get_max_buttons_per_row(
        "fake_lifecycle_channel"
    ) == 7

    # 配置变更导致插件停用时应重新同步并撤销登记
    plugin_obj = plugin_manager._running_plugins[plugin_id]
    plugin_obj.enabled = False
    plugin_manager.init_plugin(plugin_id, {})
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is False

    # 重新启用插件并生效配置后登记应恢复
    plugin_obj.enabled = True
    plugin_manager.init_plugin(plugin_id, {})
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is True

    # 插件停止后必须撤销登记，不留残留
    plugin_manager.stop(plugin_id)
    assert ChannelCapabilityManager.supports_buttons("fake_lifecycle_channel") is False


def test_plugin_manager_start_skips_channel_capabilities_when_plugin_raises(
    monkeypatch, plugin_manager: PluginManager
):
    """插件的 get_channel_capabilities 抛异常时不应阻断插件加载。"""

    class _BrokenChannelPlugin(_FakeChannelPlugin):
        """声明渠道能力时抛异常的插件桩。"""

        def get_channel_capabilities(self):
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


def _bridge_capabilities(max_buttons_per_row: int) -> ChannelCapabilities:
    """构造同一渠道标识、按钮行上限不同的一条渠道能力声明。

    :param max_buttons_per_row: 每行按钮数上限，用于分辨生效的是哪一份登记
    :return: 渠道能力声明
    """
    return ChannelCapabilities(
        channel="demo_bridge",
        capabilities={ChannelCapability.INLINE_BUTTONS},
        max_buttons_per_row=max_buttons_per_row,
    )


def test_later_registration_overrides_the_earlier_one_on_the_same_channel():
    """渠道标识指称同一个外部渠道，不同登记方声明同一标识按后登记生效处置。"""
    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )

    handovers = ChannelCapabilityManager.register_extension_capabilities(
        "Second", [_bridge_capabilities(6)]
    )

    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 6
    assert [
        (item.identity, item.previous_owner, item.current_owner) for item in handovers
    ] == [("demo_bridge", "First", "Second")]


def test_revoking_the_override_restores_the_channel_to_the_displaced_owner():
    """覆盖方撤销登记后，被它压住的那一份重新接手，而不是留下空位。"""
    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )
    ChannelCapabilityManager.register_extension_capabilities(
        "Second", [_bridge_capabilities(6)]
    )

    handovers = ChannelCapabilityManager.register_extension_capabilities("Second", [])

    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 3
    assert ChannelCapabilityManager._extension_owners["demo_bridge"] == "First"
    assert [
        (item.identity, item.previous_owner, item.current_owner) for item in handovers
    ] == [("demo_bridge", "Second", "First")]


def test_revoking_the_displaced_owner_leaves_the_effective_registration_alone():
    """被压住的一方自己停用只是从队列里退出，不影响当前生效的覆盖方。"""
    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )
    ChannelCapabilityManager.register_extension_capabilities(
        "Second", [_bridge_capabilities(6)]
    )

    handovers = ChannelCapabilityManager.register_extension_capabilities("First", [])

    assert handovers == ()
    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 6

    # 队列里已无 First，覆盖方撤销后该渠道回到无人登记
    ChannelCapabilityManager.register_extension_capabilities("Second", [])
    assert ChannelCapabilityManager.supports_buttons("demo_bridge") is False


def test_all_owners_revoked_clears_the_channel_entirely():
    """登记方全部撤销后渠道标识不再残留任何登记。"""
    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )
    ChannelCapabilityManager.register_extension_capabilities(
        "Second", [_bridge_capabilities(6)]
    )

    ChannelCapabilityManager.register_extension_capabilities("Second", [])
    ChannelCapabilityManager.register_extension_capabilities("First", [])

    assert ChannelCapabilityManager.supports_buttons("demo_bridge") is False
    assert "demo_bridge" not in ChannelCapabilityManager._extension_registrations
    assert "demo_bridge" not in ChannelCapabilityManager._extension_owners


def test_same_owner_re_registration_is_not_reported_as_a_handover():
    """同一登记方刷新自己的登记不构成归属变更，不打覆盖告警。"""
    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )

    handovers = ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(9)]
    )

    assert handovers == ()
    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 9


def test_re_registering_after_being_displaced_takes_the_channel_back():
    """被压住的一方重新登记即排到末位，按后登记生效重新拿回渠道。"""
    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )
    ChannelCapabilityManager.register_extension_capabilities(
        "Second", [_bridge_capabilities(6)]
    )

    handovers = ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(4)]
    )

    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 4
    assert [
        (item.identity, item.previous_owner, item.current_owner) for item in handovers
    ] == [("demo_bridge", "Second", "First")]

    # Second 仍在队列里，First 撤销后由它接手
    ChannelCapabilityManager.register_extension_capabilities("First", [])
    assert ChannelCapabilityManager.get_max_buttons_per_row("demo_bridge") == 6


def test_builtin_channel_still_wins_over_extension_registrations():
    """内建渠道优先未被裁决改动：扩展登记只在内建静态表未命中时才被查到。"""
    builtin_identity = NotificationChannel.Telegram.value
    builtin = ChannelCapabilityManager.get_capabilities(NotificationChannel.Telegram)

    ChannelCapabilityManager.register_extension_capabilities(
        "First",
        [
            ChannelCapabilities(
                channel=builtin_identity,
                capabilities=set(),
                max_buttons_per_row=1,
            )
        ],
    )
    ChannelCapabilityManager.register_extension_capabilities(
        "Second",
        [
            ChannelCapabilities(
                channel=builtin_identity,
                capabilities=set(),
                max_buttons_per_row=2,
            )
        ],
    )

    assert ChannelCapabilityManager.get_capabilities(NotificationChannel.Telegram) is builtin
    assert ChannelCapabilityManager.supports_buttons(NotificationChannel.Telegram) is True


def test_manager_warns_on_override_and_reports_restore(monkeypatch):
    """管理器把裁决结果落成日志：覆盖打告警，撤销后交回被覆盖方打提示。"""
    warnings: list = []
    infos: list = []
    monkeypatch.setattr(plugin_manager_module.logger, "warning", warnings.append)
    monkeypatch.setattr(plugin_manager_module.logger, "info", infos.append)

    ChannelCapabilityManager.register_extension_capabilities(
        "First", [_bridge_capabilities(3)]
    )
    PluginManager._log_channel_capability_handovers(
        "Second",
        ChannelCapabilityManager.register_extension_capabilities(
            "Second", [_bridge_capabilities(6)]
        ),
    )

    assert len(warnings) == 1
    assert "demo_bridge" in warnings[0] and "First" in warnings[0]

    PluginManager._log_channel_capability_handovers(
        "Second", ChannelCapabilityManager.register_extension_capabilities("Second", [])
    )

    assert len(warnings) == 1
    assert len(infos) == 1
    assert "demo_bridge" in infos[0] and "First" in infos[0]
