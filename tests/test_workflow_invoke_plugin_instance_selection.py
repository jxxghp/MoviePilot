"""工作流调用插件动作：多分身默认调用目标裁决与失败可见性。

插件按配置扇出多个正在运行的分身后，`get_plugin_actions()` 按分身分组返回，
不能像早前实现那样取分组列表的第一项——那个顺序只取决于运行实例登记的先后，
与用户的选择无关，是 docs/plugin-extension-architecture.md §7.2 明令禁止的
「取第一个」。本文件验证 `PluginManager.get_plugin_action()` 与其调用方
`InvokePluginAction` 按该节的默认调用目标裁决规则选中正确的分身：有已启用
默认目标则用，没有则报错且消息列出候选，单分身时照常工作；报错必须以异常
方式冒出工作流引擎，不能被本层 `except Exception` 吞掉变成静默失败。
"""
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.contract.declaration import ActionDeclaration
from app.runtime.extensions.admission.instance_selection import PluginInstanceTarget
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.workflow import Action, ActionContext
from app.workflow import WorkFlowManager
from app.workflow.actions import invoke_plugin as invoke_plugin_module
from app.workflow.actions.invoke_plugin import InvokePluginAction


class _ActionInstancePlugin:
    """声明单个工作流动作的插件分身桩，回显自身实例键以便断言命中哪一个分身。"""

    plugin_name = "分身动作插件"

    def __init__(self, instance_key: str, state: bool = True):
        self._instance_key = instance_key
        self._state = state

    def get_state(self) -> bool:
        """返回该分身的启用状态。"""
        return self._state

    def provides_actions(self):
        """声明唯一的 ping 动作，实现绑定在本分身上。"""
        return [ActionDeclaration(action_id="ping", name="Ping", impl=self._ping)]

    def _ping(self, context: ActionContext, **_kwargs):
        """回显命中的实例键，用于断言调用落到了哪一个分身。"""
        context.runtime_state = context.runtime_state or {}
        context.runtime_state["hit_instance"] = self._instance_key
        return True, context


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.fixture
def instance_targets(monkeypatch):
    """把实例状态读取钩子换成用例自备的内存实现。

    :return: ``install(plugin_id, targets)``，登记某插件的实例状态
    """
    import app.runtime.extensions.admission.instance_selection as module

    registry: dict[str, list[PluginInstanceTarget]] = {}
    monkeypatch.setattr(
        module, "_instance_target_lister", lambda plugin_id: registry.get(plugin_id, [])
    )

    def install(plugin_id: str, targets: list[PluginInstanceTarget]) -> None:
        registry[plugin_id] = targets

    return install


def _target(instance_id: str, *, enabled: bool = True, default: bool = False):
    """构造一条实例状态。"""
    return PluginInstanceTarget(
        instance_id=instance_id, is_enabled=enabled, is_default_target=default
    )


# --------------------------------------------------------------------------- #
# PluginManager.get_plugin_action：分身裁决
# --------------------------------------------------------------------------- #

def test_get_plugin_action_uses_default_target_among_multiple_instances(
    plugin_manager, instance_targets
):
    """多分身且有已启用默认目标时，动作解析命中默认分身，不取登记顺序中的第一个。

    两个分身按 "zzz" 先于 "alt" 的顺序登记，字典序也是 zzz 更靠后；如果实现
    退化成取列表第一项，命中的会是 zzz 而不是被指定为默认目标的 alt。
    """
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz"), _target("alt", default=True)])

    action = plugin_manager.get_plugin_action("Demo", "ping")

    context = ActionContext()
    success, result_context = action["func"](context)

    assert success is True
    assert result_context.runtime_state["hit_instance"] == "Demo@alt"


def test_get_plugin_action_raises_with_candidates_when_no_default(
    plugin_manager, instance_targets
):
    """多分身且没有默认目标时报错，消息含插件标识与候选分身及其启用态。"""
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz"), _target("alt")])

    with pytest.raises(LookupError) as excinfo:
        plugin_manager.get_plugin_action("Demo", "ping")

    message = str(excinfo.value)
    assert "Demo" in message
    assert "zzz（已启用）" in message
    assert "alt（已启用）" in message


def test_get_plugin_action_raises_when_default_target_is_disabled(
    plugin_manager, instance_targets
):
    """默认目标已停用时报错，不改走另一个在跑的分身。"""
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz", enabled=False, default=True), _target("alt")])

    with pytest.raises(LookupError) as excinfo:
        plugin_manager.get_plugin_action("Demo", "ping")

    assert "已停用" in str(excinfo.value)


def test_get_plugin_action_works_for_single_instance(plugin_manager):
    """单分身插件调用动作照常工作，不需要设置默认调用目标。"""
    plugin_manager.running_plugins["Demo"] = _ActionInstancePlugin("Demo")

    action = plugin_manager.get_plugin_action("Demo", "ping")
    success, result_context = action["func"](ActionContext())

    assert success is True
    assert result_context.runtime_state["hit_instance"] == "Demo"


def test_get_plugin_action_raises_for_missing_plugin(plugin_manager):
    """插件不存在或未运行时报错，而不是返回空动作让调用方误当成执行成功。"""
    with pytest.raises(LookupError):
        plugin_manager.get_plugin_action("Missing", "ping")


def test_get_plugin_action_raises_for_missing_action_id(plugin_manager):
    """插件存在但未声明该动作时报错。"""
    plugin_manager.running_plugins["Demo"] = _ActionInstancePlugin("Demo")

    with pytest.raises(LookupError):
        plugin_manager.get_plugin_action("Demo", "not_declared")


def test_get_plugin_action_uses_exact_instance_key_without_default(
    plugin_manager, instance_targets
):
    """调用方已显式传入实例键时直接精确命中，不经过默认调用目标裁决。"""
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz"), _target("alt")])  # 均无默认目标

    action = plugin_manager.get_plugin_action("Demo@zzz", "ping")
    success, result_context = action["func"](ActionContext())

    assert success is True
    assert result_context.runtime_state["hit_instance"] == "Demo@zzz"


# --------------------------------------------------------------------------- #
# InvokePluginAction：工作流动作层
# --------------------------------------------------------------------------- #

def test_invoke_plugin_action_calls_default_target_instance(
    plugin_manager, instance_targets, monkeypatch
):
    """工作流动作按插件ID调用时命中默认分身，验证经由公开端口的完整链路。"""
    monkeypatch.setattr(invoke_plugin_module, "PluginManager", lambda: plugin_manager)
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz"), _target("alt", default=True)])

    action = InvokePluginAction("node-1")
    context = action.execute(
        workflow_id=1,
        params={"plugin_id": "Demo", "action_id": "ping", "action_params": {}},
        context=ActionContext(),
    )

    assert action.success is True
    assert context.runtime_state["hit_instance"] == "Demo@alt"


def test_invoke_plugin_action_raises_lookup_error_instead_of_swallowing_it(
    plugin_manager, instance_targets, monkeypatch
):
    """裁决不出目标时 LookupError 必须从 execute() 冒出来，不能被本层吞掉。

    早前实现在此处有一层 ``except Exception`` 只记服务端日志、对调用方返回
    未变化的 context，调用方与用户都看不出这次调用其实没有执行。
    """
    monkeypatch.setattr(invoke_plugin_module, "PluginManager", lambda: plugin_manager)
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz"), _target("alt")])

    action = InvokePluginAction("node-1")
    with pytest.raises(LookupError) as excinfo:
        action.execute(
            workflow_id=1,
            params={"plugin_id": "Demo", "action_id": "ping", "action_params": {}},
            context=ActionContext(),
        )

    assert "Demo" in str(excinfo.value)


def test_invoke_plugin_action_works_for_single_instance(monkeypatch, plugin_manager):
    """单分身插件按插件ID调用照常工作。"""
    monkeypatch.setattr(invoke_plugin_module, "PluginManager", lambda: plugin_manager)
    plugin_manager.running_plugins["Demo"] = _ActionInstancePlugin("Demo")

    action = InvokePluginAction("node-1")
    context = action.execute(
        workflow_id=1,
        params={"plugin_id": "Demo", "action_id": "ping", "action_params": {}},
        context=ActionContext(),
    )

    assert action.success is True
    assert context.runtime_state["hit_instance"] == "Demo"


# --------------------------------------------------------------------------- #
# 错误传播路径：工作流引擎把异常转换为用户可见的失败原因
# --------------------------------------------------------------------------- #

def test_workflow_manager_surfaces_invoke_plugin_failure_as_visible_message(
    plugin_manager, instance_targets, monkeypatch
):
    """`WorkFlowManager.execute()` 把动作抛出的 LookupError 转换成用户可见的失败消息。

    使用真实的 `InvokePluginAction` 类和真实的 `WorkFlowManager.execute()` 调度
    路径（跳过 `__init__` 的插件扫描，直接注入动作表），证明本次修复对应的
    异常不会在工作流引擎这一层被静默吞掉，而是变成 `ActionResult.success=False`
    且 `message` 携带可读原因。
    """
    monkeypatch.setattr(invoke_plugin_module, "PluginManager", lambda: plugin_manager)
    monkeypatch.setattr(
        "app.workflow.global_vars.is_workflow_stopped", lambda workflow_id: False
    )
    plugin_manager.running_plugins["Demo@zzz"] = _ActionInstancePlugin("Demo@zzz")
    plugin_manager.running_plugins["Demo@alt"] = _ActionInstancePlugin("Demo@alt")
    instance_targets("Demo", [_target("zzz"), _target("alt")])

    manager = object.__new__(WorkFlowManager)
    manager._actions = {"InvokePluginAction": InvokePluginAction}

    result = manager.execute(
        workflow_id=1,
        action=Action(
            id="node-1",
            type="InvokePluginAction",
            name="调用插件",
            data={"plugin_id": "Demo", "action_id": "ping", "action_params": {}},
        ),
        context=ActionContext(),
    )

    assert result.success is False
    assert "Demo" in result.message
