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
