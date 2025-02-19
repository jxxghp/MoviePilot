from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class AddDownloadParams(ActionParams):
    """
    添加下载资源参数
    """
    pass


class AddDownloadAction(BaseAction):
    """
    添加下载资源
    """

    @property
    def name(self) -> str:
        return "添加下载资源"

    @property
    def description(self) -> str:
        return "根据资源列表添加下载任务"

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: AddDownloadParams, context: ActionContext) -> ActionContext:
        pass
