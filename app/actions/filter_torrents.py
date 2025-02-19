from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class FilterTorrentsParams(ActionParams):
    """
    过滤资源数据参数
    """
    pass


class FilterTorrentsAction(BaseAction):
    """
    过滤资源数据
    """

    @property
    def name(self) -> str:
        return "过滤资源数据"

    @property
    def description(self) -> str:
        return "过滤资源数据列表"

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: FilterTorrentsParams, context: ActionContext) -> ActionContext:
        pass
