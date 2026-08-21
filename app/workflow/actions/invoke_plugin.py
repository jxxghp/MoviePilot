from pydantic import Field

from app.workflow.actions import BaseAction
from app.application.plugin.runtime import get_plugin_manager as PluginManager
from app.runtime.extensions.service_instance_requirement import (
    SERVICE_INSTANCE_PARAM,
    resolve_required_service_instance,
)
from app.runtime.log import logger
from app.schemas.workflow import ActionParams
from app.schemas.workflow import ActionContext


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
        return InvokePluginParams().model_dump()

    @property
    def success(self) -> bool:
        return self._success

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        执行插件定义的动作

        插件按配置扇出多个分身时，未指定实例的调用按该插件的默认调用目标裁决；
        插件不存在、动作不存在，或裁决不出目标，均以异常呈现给工作流引擎，由其
        统一转换为用户可见的失败原因，本层不再吞掉后转成静默失败。

        动作声明了作用于哪一族服务实例时，用户在本节点选中的实例名从动作参数里
        取，解析成立后按同一个键交回给实现；未选中则按该族的默认调用目标裁决，
        裁决不出即报错并列出候选。未声明作用对象的动作，参数原样展开，调用形状
        与该字段存在之前逐字相同。
        """
        params = InvokePluginParams(**params)
        if not params.plugin_id or not params.action_id:
            return context
        logger.info(f"调用插件动作: {params.plugin_id} - {params.action_id}")
        action = PluginManager().get_plugin_action(params.plugin_id, params.action_id)
        action_params = dict(params.action_params)
        requirement = action.get("requires_service_instance")
        if requirement:
            action_params[SERVICE_INSTANCE_PARAM] = resolve_required_service_instance(
                requirement, action_params.get(SERVICE_INSTANCE_PARAM)
            )
        # 执行插件动作
        self._success, context = action["func"](context, **action_params)
        self.job_done()
        return context
