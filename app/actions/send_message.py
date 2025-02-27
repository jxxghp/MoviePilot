import copy
from typing import List, Optional, Union

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.schemas import ActionParams, ActionContext


class SendMessageParams(ActionParams):
    """
    发送消息参数
    """
    client: Optional[List[str]] = Field([], description="消息渠道")
    userid: Optional[Union[str, int]] = Field(None, description="用户ID")


class SendMessageAction(BaseAction):
    """
    发送消息
    """

    def __init__(self):
        super().__init__()
        self.chain = ActionChain()

    @property
    def name(self) -> str:
        return "发送消息"

    @property
    def description(self) -> str:
        return "发送队列中的所有消息"

    @property
    def data(self) -> dict:
        return SendMessageParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    async def execute(self, params: SendMessageParams, context: ActionContext) -> ActionContext:
        """
        发送messages中的消息
        """
        for message in copy.deepcopy(context.messages):
            if params.client:
                message.source = params.client
            if params.userid:
                message.userid = params.userid
            self.chain.post_message(message)
            context.messages.remove(message)

        context.messages = []

        self.job_done()
        return context
