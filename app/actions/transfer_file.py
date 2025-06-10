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
    source: Optional[str] = Field(default="downloads", description="来源")


class TransferFileAction(BaseAction):
    """
    整理文件
    """

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._fileitems = []
        self._has_error = False

    @classmethod
    @property
    def name(cls) -> str:  # noqa
        return "整理文件"

    @classmethod
    @property
    def description(cls) -> str:  # noqa
        return "整理队列中的文件"

    @classmethod
    @property
    def data(cls) -> dict:  # noqa
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
        # 失败次数
        _failed_count = 0
        storagechain = StorageChain()
        transferchain = TransferChain()
        transferhis = TransferHistoryOper()
        if params.source == "downloads":
            # 从下载任务中整理文件
            for download in context.downloads:
                if global_vars.is_workflow_stopped(workflow_id):
                    break
                if not download.completed:
                    logger.info(f"下载任务 {download.download_id} 未完成")
                    continue
                # 检查缓存
                cache_key = f"{download.download_id}"
                if self.check_cache(workflow_id, cache_key):
                    logger.info(f"{download.path} 已整理过，跳过")
                    continue
                fileitem = storagechain.get_file_item(storage="local", path=Path(download.path))
                if not fileitem:
                    logger.info(f"文件 {download.path} 不存在")
                    continue
                transferd = transferhis.get_by_src(fileitem.path, storage=fileitem.storage)
                if transferd:
                    # 已经整理过的文件不再整理
                    continue
                logger.info(f"开始整理文件 {download.path} ...")
                state, errmsg = transferchain.do_transfer(fileitem, background=False)
                if not state:
                    _failed_count += 1
                    logger.error(f"整理文件 {download.path} 失败: {errmsg}")
                    continue
                logger.info(f"整理文件 {download.path} 完成")
                self._fileitems.append(fileitem)
                self.save_cache(workflow_id, cache_key)
        else:
            # 从 fileitems 中整理文件
            for fileitem in copy.deepcopy(context.fileitems):
                if not check_continue():
                    break
                # 检查缓存
                cache_key = f"{fileitem.path}"
                if self.check_cache(workflow_id, cache_key):
                    logger.info(f"{fileitem.path} 已整理过，跳过")
                    continue
                transferd = transferhis.get_by_src(fileitem.path, storage=fileitem.storage)
                if transferd:
                    # 已经整理过的文件不再整理
                    continue
                logger.info(f"开始整理文件 {fileitem.path} ...")
                state, errmsg = transferchain.do_transfer(fileitem, background=False,
                                                          continue_callback=check_continue)
                if not state:
                    _failed_count += 1
                    logger.error(f"整理文件 {fileitem.path} 失败: {errmsg}")
                    continue
                logger.info(f"整理文件 {fileitem.path} 完成")
                # 从 fileitems 中移除已整理的文件
                context.fileitems.remove(fileitem)
                self._fileitems.append(fileitem)
                # 记录已整理的文件
                self.save_cache(workflow_id, cache_key)

        if self._fileitems:
            context.fileitems.extend(self._fileitems)
        elif _failed_count:
            self._has_error = True

        self.job_done(f"整理成功 {len(self._fileitems)} 个文件，失败 {_failed_count} 个")
        return context
