from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class FilterMediasParams(ActionParams):
    """
    过滤媒体数据参数
    """
    pass


class FilterMediasAction(BaseAction):
    """
    过滤媒体数据
    """

    @property
    def name(self) -> str:
        return "过滤媒体数据"

    @property
    def description(self) -> str:
        return "过滤媒体数据列表"

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: FilterMediasParams, context: ActionContext) -> ActionContext:
        pass
