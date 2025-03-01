from typing import List, Optional, Union

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.schemas import ActionParams, ActionContext, Notification


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
        msg_text = f"当前进度：{context.__progress__}%"
        index = 1
        if context.__execute_history__:
            for history in context.__execute_history__:
                if not history.message:
                    continue
                msg_text += f"\n{index}. {history.action}：{history.message}"
                index += 1
            # 发送消息
            if not params.client:
                params.client = [None]
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
