from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


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
        return "发送特定事件"

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: SendEventParams, context: ActionContext) -> ActionContext:
        pass
