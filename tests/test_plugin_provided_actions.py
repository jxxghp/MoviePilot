"""插件声明工作流动作链路测试：契约校验、聚合归属、停用回收与旧钩子并存。"""

from typing import Iterator, List, Optional

import pytest

from app.foundation.singleton import Singleton
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.deprecation.notices import DeprecationNotice, DeprecationStage
from app.runtime.extensions.contract.declaration import ActionDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager


def _handler(context, **_kwargs):
    """契约合规的动作实现：接收 context 首个位置参数，回显执行结果。"""
    return True, context


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


class _CapableActionPlugin:
    """声明工作流动作的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "动作插件"

    def __init__(self, enabled=True, declarations=None, raise_error=False):
        self._enabled = enabled
        self._declarations = declarations
        self._raise_error = raise_error

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def provides_actions(self):
        """返回声明的工作流动作，或按需抛出异常模拟插件实现出错。"""
        if self._raise_error:
            raise RuntimeError("声明工作流动作时出错")
        return self._declarations


def test_projection_accepts_valid_declaration():
    """契约合规的声明应被接受，字段原样保留。"""
    plugin = _CapableActionPlugin(
        declarations=[
            ActionDeclaration(action_id="my_action", name="我的动作", impl=_handler)
        ]
    )
    projection = PluginProjection({"DemoAction": plugin})

    declared = projection.provided_actions()

    assert len(declared["DemoAction"]) == 1
    accepted = declared["DemoAction"][0]
    assert accepted.action_id == "my_action"
    assert accepted.impl is _handler


def test_projection_accepts_bare_dict_without_wrapper():
    """插件直接交出描述字典而不包 ActionDeclaration 的兼容写法应被接受。"""
    raw = {"action_id": "my_action", "name": "我的动作", "func": _handler}
    plugin = _CapableActionPlugin(declarations=[raw])
    projection = PluginProjection({"DemoAction": plugin})

    declared = projection.provided_actions()

    assert declared["DemoAction"] == [raw]


def _kwargs_only(**_kwargs):
    """无法接收位置参数的可调用对象，用于验证签名契约拦截。"""
    return True, None


@pytest.mark.parametrize(
    "declaration",
    [
        ActionDeclaration(action_id="a", name="A", impl=None),
        ActionDeclaration(action_id="a", name="A", impl="not-callable"),
        ActionDeclaration(action_id="a", name="A", impl=_kwargs_only),
        ActionDeclaration(action_id="", name="A", impl=_handler),
        ActionDeclaration(action_id="a", name="", impl=_handler),
        ActionDeclaration(action_id="a", name="A", impl=_handler, kwargs="not-a-mapping"),
    ],
    ids=[
        "impl_missing",
        "impl_not_callable",
        "impl_rejects_positional",
        "action_id_empty",
        "name_empty",
        "kwargs_not_mapping",
    ],
)
def test_projection_rejects_declaration_violations(declaration):
    """不合契约的声明必须被拒绝：实现缺失/不可调用/签名不合、标识或名称缺失、kwargs 非映射。"""
    plugin = _CapableActionPlugin(declarations=[declaration])
    projection = PluginProjection({"DemoAction": plugin})

    declared = projection.provided_actions()

    assert declared["DemoAction"] == []


def test_projection_partial_rejection_keeps_valid_siblings():
    """同一实例声明多条时，不合契约的条目被跳过，合规的条目照常保留。"""
    plugin = _CapableActionPlugin(
        declarations=[
            ActionDeclaration(action_id="ok_action", name="OK", impl=_handler),
            ActionDeclaration(action_id="", name="Bad", impl=_handler),
        ]
    )
    projection = PluginProjection({"DemoAction": plugin})

    declared = projection.provided_actions()

    assert len(declared["DemoAction"]) == 1
    assert declared["DemoAction"][0].action_id == "ok_action"


def test_projection_swallows_plugin_exception_without_blocking_others():
    """单个插件声明工作流动作抛异常时不应影响其它插件的投影结果。"""
    broken = _CapableActionPlugin(raise_error=True)
    healthy = _CapableActionPlugin(
        declarations=[ActionDeclaration(action_id="ok_action", name="OK", impl=_handler)]
    )
    projection = PluginProjection({"Broken": broken, "Ok": healthy})

    declared = projection.provided_actions()

    assert "Broken" not in declared
    assert declared["Ok"][0].action_id == "ok_action"


class _FakeActionPlugin:
    """既声明新式动作又实现旧式钩子的插件桩，用于驱动聚合器完整链路。"""

    plugin_name = "假想动作插件"

    def __init__(
        self,
        declared: Optional[List[ActionDeclaration]] = None,
        legacy: Optional[list] = None,
        state: bool = True,
    ):
        self._declared = declared or []
        self._legacy = legacy
        self._state = state
        self.legacy_calls: List[int] = []

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._state

    def provides_actions(self):
        """返回声明的工作流动作。"""
        return self._declared

    def get_actions(self):
        """返回旧式裸描述字典列表并记录调用次数；未配置时返回 None 表示未提供。"""
        if self._legacy is None:
            return None
        self.legacy_calls.append(1)
        return self._legacy


def test_get_plugin_actions_merges_declared_and_legacy_sources(
    plugin_manager: PluginManager,
) -> None:
    """同一实例的声明式动作与旧式裸字典动作应合并到同一份聚合结果中。"""
    plugin = _FakeActionPlugin(
        declared=[ActionDeclaration(action_id="new_action", name="新式动作", impl=_handler)],
        legacy=[{"action_id": "legacy_action", "name": "旧式动作", "func": _handler}],
    )
    plugin_manager.running_plugins["Demo"] = plugin

    result = plugin_manager.get_plugin_actions("Demo")

    assert len(result) == 1
    action_ids = {action["action_id"] for action in result[0]["actions"]}
    assert action_ids == {"new_action", "legacy_action"}


def test_get_plugin_actions_emits_deprecation_warning_for_legacy_hook(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """触达旧式 get_actions() 时必须触发一次废弃告警，重复触达不重复告警。"""
    emitted = []
    monkeypatch.setattr(deprecation_policy.logger, "warning", emitted.append)
    plugin_manager.running_plugins["Demo"] = _FakeActionPlugin(
        legacy=[{"action_id": "legacy_action", "name": "旧式动作", "func": _handler}]
    )

    plugin_manager.get_plugin_actions("Demo")
    plugin_manager.get_plugin_actions("Demo")

    assert len(emitted) == 1
    assert "get_actions" in emitted[0]


def test_legacy_hook_stops_at_disabled_stage_and_resumes_via_override(
    plugin_manager: PluginManager, monkeypatch
) -> None:
    """阶段推进到 DISABLED 时旧钩子真的停用；标识列入 DEPRECATION_ENABLED 能恢复。"""
    plugin_manager.running_plugins["Demo"] = _FakeActionPlugin(
        legacy=[{"action_id": "legacy_action", "name": "旧式动作", "func": _handler}]
    )

    # 阶段一（默认登记）：旧钩子照常生效
    result = plugin_manager.get_plugin_actions("Demo")
    assert result and result[0]["actions"]

    # 阶段二：默认停用，旧钩子不再产出条目
    _set_notice_stage(monkeypatch, "plugin.get_actions", DeprecationStage.DISABLED)
    monkeypatch.setattr(deprecation_policy, "_enabled_keys", frozenset)
    result = plugin_manager.get_plugin_actions("Demo")
    assert result == []

    # 阶段二 + 标识列入 DEPRECATION_ENABLED：临时恢复
    monkeypatch.setattr(
        deprecation_policy, "_enabled_keys", lambda: frozenset({"plugin.get_actions"})
    )
    result = plugin_manager.get_plugin_actions("Demo")
    assert result and result[0]["actions"]
