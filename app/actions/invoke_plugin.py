from pydantic import Field

from app.actions import BaseAction
from app.core.plugin import PluginManager
from app.log import logger
from app.schemas import ActionParams, ActionContext


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

    _success = False

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._success = False

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "调用插件"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "调用插件提供的动作"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return InvokePluginParams().dict()

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
            plugin_actions = PluginManager().get_plugin_actions(params.plugin_id)
            if not plugin_actions:
                logger.error(f"插件不存在: {params.plugin_id}")
                return context
            actions = plugin_actions[0].get("actions", [])
            action = next((action for action in actions if action.action_id == params.action_id), None)
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
