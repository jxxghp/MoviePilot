from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class FetchMediasParams(ActionParams):
    """
    获取媒体数据参数
    """
    pass


class FetchMediasAction(BaseAction):
    """
    获取媒体数据
    """

    @property
    def name(self) -> str:
        return "获取媒体数据"

    @property
    def description(self) -> str:
        return "获取媒体数据"

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: FetchMediasParams, context: ActionContext) -> ActionContext:
        pass
