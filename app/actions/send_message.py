from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class SendMessageParams(ActionParams):
    """
    发送消息参数
    """
    pass


class SendMessageAction(BaseAction):
    """
    发送消息
    """

    @property
    def name(self) -> str:
        return "发送消息"

    @property
    def description(self) -> str:
        return "发送特定消息"

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: SendMessageParams, context: ActionContext) -> ActionContext:
        pass
