from typing import List, Optional, Union

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.schemas import ActionParams, ActionContext, Notification


class SendMessageParams(ActionParams):
    """
    发送消息参数
    """
    client: Optional[List[str]] = Field(default=[], description="消息渠道")
    userid: Optional[Union[str, int]] = Field(default=None, description="用户ID")


class SendMessageAction(BaseAction):
    """
    发送消息
    """

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self.chain = ActionChain()

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "发送消息"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "发送任务执行消息"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return SendMessageParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        发送messages中的消息
        """
        params = SendMessageParams(**params)
        msg_text = f"当前进度：{context.progress}%"
        index = 1
        if context.execute_history:
            for history in context.execute_history:
                if not history.message:
                    continue
                msg_text += f"\n{index}. {history.action}：{history.message}"
                index += 1
            # 发送消息
            if not params.client:
                params.client = [""]
            for client in params.client:
                self.chain.post_message(
                    Notification(
                        source=client,
                        userid=params.userid,
                        title="【工作流执行结果】",
                        text=msg_text,
                        link="#/workflow"
                    )
                )

        self.job_done()
        return context
