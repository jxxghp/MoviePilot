from pathlib import Path

from app.actions import BaseAction
from app.core.config import global_vars
from app.schemas import ActionParams, ActionContext
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.log import logger


class TransferFileParams(ActionParams):
    """
    整理文件参数
    """
    pass


class TransferFileAction(BaseAction):
    """
    整理文件
    """

    _fileitems = []
    _has_error = False

    def __init__(self):
        super().__init__()
        self.transferchain = TransferChain()
        self.storagechain = StorageChain()

    @classmethod
    @property
    def name(cls) -> str:
        return "整理文件"

    @classmethod
    @property
    def description(cls) -> str:
        return "整理下载队列中的文件"

    @classmethod
    @property
    def data(cls) -> dict:
        return TransferFileParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        从downloads中整理文件，记录到fileitems
        """
        for download in context.downloads:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if not download.completed:
                logger.info(f"下载任务 {download.download_id} 未完成")
                continue
            fileitem = self.storagechain.get_file_item(storage="local", path=Path(download.path))
            if not fileitem:
                logger.info(f"文件 {download.path} 不存在")
                continue
            logger.info(f"开始整理文件 {download.path} ...")
            state, errmsg = self.transferchain.do_transfer(fileitem, background=False)
            if not state:
                self._has_error = True
                logger.error(f"整理文件 {download.path} 失败: {errmsg}")
                continue
            logger.info(f"整理文件 {download.path} 完成")
            self._fileitems.append(fileitem)

        if self._fileitems:
            context.fileitems.extend(self._fileitems)

        self.job_done()
        return context
