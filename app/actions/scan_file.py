import copy
from pathlib import Path
from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.core.config import global_vars, settings
from app.schemas import ActionParams, ActionContext
from app.chain.storage import StorageChain
from app.log import logger


class ScanFileParams(ActionParams):
    """
    整理文件参数
    """
    # 存储
    storage: Optional[str] = Field("local", description="存储")
    directory: Optional[str] = Field(None, description="目录")


class ScanFileAction(BaseAction):
    """
    整理文件
    """

    _fileitems = []
    _has_error = False

    def __init__(self):
        super().__init__()
        self.storagechain = StorageChain()

    @classmethod
    @property
    def name(cls) -> str:
        return "扫描目录"

    @classmethod
    @property
    def description(cls) -> str:
        return "扫描目录文件到队列"

    @classmethod
    @property
    def data(cls) -> dict:
        return ScanFileParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        扫描目录中的所有文件，记录到fileitems
        """
        params = ScanFileParams(**params)
        if not params.storage or not params.directory:
            return context
        fileitem = self.storagechain.get_file_item(params.storage, Path(params.directory))
        if not fileitem:
            logger.error(f"目录不存在: 【{params.storage}】{params.directory}")
            self._has_error = True
            return context
        files = self.storagechain.list_files(fileitem, recursion=True)
        for file in files:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if not file.extension or f".{file.extension.lower()}" not in settings.RMT_MEDIAEXT:
                continue
            self._fileitems.append(fileitem)

        if self._fileitems:
            context.fileitems.extend(self._fileitems)

        self.job_done()
        return context
