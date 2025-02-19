from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class TransferFileParams(ActionParams):
    """
    整理文件参数
    """
    pass


class TransferFileAction(BaseAction):
    """
    整理文件
    """

    @property
    def name(self) -> str:
        return "整理文件"

    @property
    def description(self) -> str:
        return "整理和转移文件"

    @property
    def success(self) -> bool:
        return True

    async def execute(self, params: TransferFileParams, context: ActionContext) -> ActionContext:
        pass
