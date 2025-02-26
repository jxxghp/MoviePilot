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

    __success = False

    @property
    def name(self) -> str:
        return "发送事件"

    @property
    def description(self) -> str:
        return "发送特定事件"

    @property
    def success(self) -> bool:
        return self.__success

    async def execute(self, params: SendEventParams, context: ActionContext) -> ActionContext:
        """
        发送events中的事件
        """
        if context.events:
            # 按优先级排序，优先级高的先发送
            context.events.sort(key=lambda x: x.priority, reverse=True)
            for event in copy.deepcopy(context.events):
                eventmanager.send_event(etype=event.event_type, data=event.event_data)
                context.events.remove(event)
                self.__success = True

        self.job_done()
        return context
