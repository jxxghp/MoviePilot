from pathlib import Path

from app.actions import BaseAction
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

    __fileitems = []

    def __init__(self):
        super().__init__()
        self.transferchain = TransferChain()
        self.storagechain = StorageChain()

    @property
    def name(self) -> str:
        return "整理文件"

    @property
    def description(self) -> str:
        return "转移和重命名文件"

    @property
    def data(self) -> dict:
        return TransferFileParams().dict()

    @property
    def success(self) -> bool:
        return True if self.__fileitems else False

    async def execute(self, params: TransferFileParams, context: ActionContext) -> ActionContext:
        """
        从downloads中整理文件，记录到fileitems
        """
        for download in context.downloads:
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
                logger.error(f"整理文件 {download.path} 失败: {errmsg}")
                continue
            logger.info(f"整理文件 {download.path} 完成")
            self.__fileitems.append(fileitem)

        if self.__fileitems:
            context.fileitems.extend(self.__fileitems)

        self.job_done()
        return context
