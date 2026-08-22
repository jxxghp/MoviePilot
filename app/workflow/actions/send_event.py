from app.workflow.actions import BaseAction
from app.runtime.events import eventmanager
from app.schemas.workflow import ActionParams
from app.schemas.workflow import ActionContext
from app.schemas.types import ChainEventType


class SendEventParams(ActionParams):
    """
    发送事件参数
    """
    pass


class SendEventAction(BaseAction):
    """
    发送事件
    """

    contract = {}

    name = "发送事件"
    description = "发送任务执行事件"
    data = SendEventParams().model_dump()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        发送工作流事件，以更插件干预工作流执行
        """
        # 触发资源下载事件，更新执行上下文
        event = eventmanager.send_event(ChainEventType.WorkflowExecution, context)
        if event and event.event_data:
            context = event.event_data

        self.job_done()
        return context
