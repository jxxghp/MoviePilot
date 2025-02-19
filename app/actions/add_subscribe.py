from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class AddSubscribeParams(ActionParams):
    """
    添加订阅参数
    """
    pass


class AddSubscribeAction(BaseAction):
    """
    添加订阅
    """

    @property
    def name(self) -> str:
        return "添加订阅"

    @property
    def description(self) -> str:
        return "根据媒体列表添加订阅"

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: AddSubscribeParams, context: ActionContext) -> ActionContext:
        pass
