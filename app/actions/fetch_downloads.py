from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class FetchDownloadsParams(ActionParams):
    """
    获取下载任务参数
    """
    pass


class FetchDownloadsAction(BaseAction):
    """
    获取下载任务
    """

    @property
    def name(self) -> str:
        return "获取下载任务"

    @property
    def description(self) -> str:
        return "获取下载任务，更新任务状态"

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: FetchDownloadsParams, context: ActionContext) -> ActionContext:
        pass
