import copy
from pathlib import Path
from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.core.config import global_vars
from app.db.transferhistory_oper import TransferHistoryOper
from app.schemas import ActionParams, ActionContext
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.log import logger


class TransferFileParams(ActionParams):
    """
    整理文件参数
    """
    # 来源
    source: Optional[str] = Field("downloads", description="来源")


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
        self.transferhis = TransferHistoryOper()

    @classmethod
    @property
    def name(cls) -> str:
        return "整理文件"

    @classmethod
    @property
    def description(cls) -> str:
        return "整理队列中的文件"

    @classmethod
    @property
    def data(cls) -> dict:
        return TransferFileParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        从 downloads / fileitems 中整理文件，记录到fileitems
        """

        def check_continue():
            """
            检查是否继续整理文件
            """
            if global_vars.is_workflow_stopped(workflow_id):
                return False
            return True

        params = TransferFileParams(**params)
        if params.source == "downloads":
            # 从下载任务中整理文件
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
                transferd = self.transferhis.get_by_src(fileitem.path, storage=fileitem.storage)
                if transferd:
                    # 已经整理过的文件不再整理
                    continue
                logger.info(f"开始整理文件 {download.path} ...")
                state, errmsg = self.transferchain.do_transfer(fileitem, background=False)
                if not state:
                    self._has_error = True
                    logger.error(f"整理文件 {download.path} 失败: {errmsg}")
                    continue
                logger.info(f"整理文件 {download.path} 完成")
                self._fileitems.append(fileitem)
        else:
            # 从 fileitems 中整理文件
            for fileitem in copy.deepcopy(context.fileitems):
                if not check_continue():
                    break
                transferd = self.transferhis.get_by_src(fileitem.path, storage=fileitem.storage)
                if transferd:
                    # 已经整理过的文件不再整理
                    continue
                logger.info(f"开始整理文件 {fileitem.path} ...")
                state, errmsg = self.transferchain.do_transfer(fileitem, background=False,
                                                               continue_callback=check_continue)
                if not state:
                    self._has_error = True
                    logger.error(f"整理文件 {fileitem.path} 失败: {errmsg}")
                    continue
                logger.info(f"整理文件 {fileitem.path} 完成")
                # 从 fileitems 中移除已整理的文件
                context.fileitems.remove(fileitem)
                self._fileitems.append(fileitem)

        if self._fileitems:
            context.fileitems.extend(self._fileitems)

        self.job_done()
        return context
