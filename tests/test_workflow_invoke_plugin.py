"""插件工作流动作标识的公开契约与执行回归。"""

from unittest.mock import Mock, patch

from app.schemas.workflow import ActionContext
from app.workflow.actions.invoke_plugin import InvokePluginAction


def _execute_with_action(action: dict) -> tuple[InvokePluginAction, ActionContext]:
    """在最小运行时替身中执行一个插件动作。"""
    context = ActionContext(content="before")
    action_fn = Mock(return_value=(True, context))
    action["func"] = action_fn
    plugin_manager = Mock()
    plugin_manager.get_plugin_actions.return_value = [
        {"plugin_id": "plugin-a", "actions": [action]}
    ]

    with patch(
        "app.workflow.actions.get_configured_system_config",
        return_value=Mock(),
    ), patch(
        "app.workflow.actions.invoke_plugin.get_plugin_manager",
        return_value=plugin_manager,
    ):
        action_runner = InvokePluginAction("invoke")
        result = action_runner.execute(
            workflow_id=1,
            params={
                "plugin_id": "plugin-a",
                "action_id": "cleanup",
                "action_params": {"force": True},
            },
            context=context,
        )

    action_fn.assert_called_once_with(context, force=True)
    assert action_runner.success is True
    assert action_runner.done is True
    return action_runner, result


def test_invoke_plugin_uses_public_action_id() -> None:
    """插件公开的 id 字段必须可被工作流执行器解析。"""
    _, result = _execute_with_action({"id": "cleanup"})

    assert result.content == "before"


def test_invoke_plugin_keeps_legacy_action_id_fallback() -> None:
    """历史插件声明仍可通过 action_id 回退执行。"""
    _, result = _execute_with_action({"action_id": "cleanup"})

    assert result.content == "before"


def test_invoke_plugin_dispatches_to_resolved_default_target() -> None:
    """插件 ID 有分身时，动作按裁决出的默认调用目标查询动作，而不是原样使用插件 ID。

    这是历史工作流在源插件本体停用、仅分身启用后仍能继续工作的关键：存量工作流
    保存的还是物理插件 ID，必须经过默认调用目标裁决才能落到实际在跑的分身上。
    """
    context = ActionContext(content="before")
    action = {"id": "cleanup"}
    action_fn = Mock(return_value=(True, context))
    action["func"] = action_fn
    plugin_manager = Mock()
    plugin_manager.resolve_plugin_call_target.return_value = "plugin-a-clone"
    plugin_manager.get_plugin_actions.return_value = [
        {"plugin_id": "plugin-a-clone", "actions": [action]}
    ]

    with patch(
        "app.workflow.actions.get_configured_system_config",
        return_value=Mock(),
    ), patch(
        "app.workflow.actions.invoke_plugin.get_plugin_manager",
        return_value=plugin_manager,
    ):
        action_runner = InvokePluginAction("invoke")
        action_runner.execute(
            workflow_id=1,
            params={
                "plugin_id": "plugin-a",
                "action_id": "cleanup",
                "action_params": {},
            },
            context=context,
        )

    plugin_manager.resolve_plugin_call_target.assert_called_once_with("plugin-a")
    plugin_manager.get_plugin_actions.assert_called_once_with("plugin-a-clone")
    assert action_runner.success is True


def test_invoke_plugin_fails_gracefully_when_default_target_undecidable() -> None:
    """裁决报错（未设默认目标／默认目标已停用）时动作失败但不抛出，不误执行任何实例。"""
    context = ActionContext(content="before")
    plugin_manager = Mock()
    plugin_manager.resolve_plugin_call_target.side_effect = LookupError(
        "插件 plugin-a 未设置默认实例，调用必须显式指定实例；可选实例：a（已启用）、b（已启用）"
    )

    with patch(
        "app.workflow.actions.get_configured_system_config",
        return_value=Mock(),
    ), patch(
        "app.workflow.actions.invoke_plugin.get_plugin_manager",
        return_value=plugin_manager,
    ):
        action_runner = InvokePluginAction("invoke")
        result = action_runner.execute(
            workflow_id=1,
            params={
                "plugin_id": "plugin-a",
                "action_id": "cleanup",
                "action_params": {},
            },
            context=context,
        )

    plugin_manager.get_plugin_actions.assert_not_called()
    assert action_runner.success is False
    assert result.content == "before"
