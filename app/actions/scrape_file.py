from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class ScrapeFileParams(ActionParams):
    """
    刮削文件参数
    """
    pass


class ScrapeFileAction(BaseAction):
    """
    刮削文件
    """

    @property
    def name(self) -> str:
        return "刮削文件"

    @property
    def description(self) -> str:
        return "刮削媒体信息和图片"

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: ScrapeFileParams, context: ActionContext) -> ActionContext:
        pass
