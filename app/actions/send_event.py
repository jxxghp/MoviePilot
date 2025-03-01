from app.actions import BaseAction
from app.core.event import eventmanager
from app.schemas import ActionParams, ActionContext
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

    @classmethod
    @property
    def name(cls) -> str:
        return "发送事件"

    @classmethod
    @property
    def description(cls) -> str:
        return "发送任务执行事件"

    @classmethod
    @property
    def data(cls) -> dict:
        return SendEventParams().dict()

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
