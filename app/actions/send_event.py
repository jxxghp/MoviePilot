import copy

from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext
from app.core.event import eventmanager


class SendEventParams(ActionParams):
    """
    发送事件参数
    """
    pass


class SendEventAction(BaseAction):
    """
    发送事件
    """

    @property
    def name(self) -> str:
        return "发送事件"

    @property
    def description(self) -> str:
        return "发送队列中的所有事件"

    @property
    def data(self) -> dict:
        return SendEventParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, params: dict, context: ActionContext) -> ActionContext:
        """
        发送events中的事件
        """
        if context.events:
            # 按优先级排序，优先级高的先发送
            context.events.sort(key=lambda x: x.priority, reverse=True)
            for event in copy.deepcopy(context.events):
                eventmanager.send_event(etype=event.event_type, data=event.event_data)
                context.events.remove(event)

        self.job_done()
        return context
