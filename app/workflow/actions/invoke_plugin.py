from pydantic import Field

from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class InvokePluginParams(ActionParams):
    """
    调用插件动作参数
    """
    plugin_id: str = Field(default=None, description="插件ID")
    action_id: str = Field(default=None, description="动作ID")
    action_params: dict = Field(default={}, description="动作参数")


class InvokePluginAction(BaseAction):
    """
    调用插件
    """

    contract = {}

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._success = False

    name = "调用插件"
    description = "调用插件提供的动作"
    data = InvokePluginParams().model_dump()

    @property
    def success(self) -> bool:
        return self._success

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        执行插件定义的动作
        """
        params = InvokePluginParams(**params)
        if not params.plugin_id or not params.action_id:
            return context
        try:
            plugin_manager = get_plugin_manager()
            # 未指定实例的插件 ID 存在分身时须裁决默认调用目标，避免历史工作流在
            # 源插件本体停用、仅分身启用后静默失效
            resolved_plugin_id = plugin_manager.resolve_plugin_call_target(params.plugin_id)
            plugin_actions = plugin_manager.get_plugin_actions(resolved_plugin_id)
            if not plugin_actions:
                logger.error(f"插件不存在: {params.plugin_id}")
                return context
            actions = plugin_actions[0].get("actions", [])
            # 插件公开动作契约使用 ``id``；读取旧插件声明时保留 action_id 回退，避免已保存工作流失效。
            action = next(
                (
                    action
                    for action in actions
                    if (action.get("id") or action.get("action_id"))
                    == params.action_id
                ),
                None,
            )
            if not action or not action.get("func"):
                logger.error(f"插件动作不存在: {params.plugin_id} - {params.action_id}")
                return context
            # 执行插件动作
            self._success, context = action["func"](context, **params.action_params)
        except Exception as e:
            self._success = False
            logger.error(f"调用插件动作失败: {e}")
            return context
        self.job_done()
        return context
