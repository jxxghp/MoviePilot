"""
调用插件公开的工作流动作。
"""

from typing import Optional

from pydantic import Field

from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class InvokePluginParams(ActionParams):
    """
    调用插件动作参数
    """
    plugin_id: Optional[str] = Field(default=None, description="插件ID")
    action_id: Optional[str] = Field(default=None, description="动作ID")
    action_params: dict[str, JsonData] = Field(default_factory=dict, description="动作参数")


class InvokePluginAction(BaseAction):
    """
    调用插件
    """

    contract = {}

    def __init__(self, action_id: str):
        """
        绑定工作流动作标识，并初始化插件执行结果状态。
        """
        super().__init__(action_id)
        self._success = False

    name = "调用插件"
    description = "调用插件提供的动作"
    data = InvokePluginParams().model_dump()

    @property
    def success(self) -> bool:
        """
返回插件动作是否报告执行成功。

        :return: 插件动作完成且成功时为 True
        """
        return self._success

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        执行插件定义的动作
        """
        action_params = InvokePluginParams.model_validate(params)
        if not action_params.plugin_id or not action_params.action_id:
            return context
        try:
            plugin_manager = get_plugin_manager()
            # 只给了插件 ID 而该插件已有分身时，必须先裁决默认调用目标：按登记顺序
            # 取第一个会让同一条工作流在不同时刻落到不同实例，执行结果不可复现
            resolved_plugin_id = plugin_manager.resolve_plugin_call_target(action_params.plugin_id)
            plugin_actions = plugin_manager.get_plugin_actions(resolved_plugin_id)
            if not plugin_actions:
                logger.error(f"插件不存在: {action_params.plugin_id}")
                return context
            actions = plugin_actions[0].get("actions", [])
            # 插件公开动作契约使用 ``id``；读取旧插件声明时保留 action_id 回退，避免已保存工作流失效。
            action = next(
                (
                    action
                    for action in actions
                    if (action.get("id") or action.get("action_id"))
                    == action_params.action_id
                ),
                None,
            )
            if not action or not action.get("func"):
                logger.error(f"插件动作不存在: {action_params.plugin_id} - {action_params.action_id}")
                return context
            # 执行插件动作
            self._success, context = action["func"](context, **action_params.action_params)
        except Exception as e:
            self._success = False
            logger.error(f"调用插件动作失败: {e}")
            return context
        self.job_done()
        return context
