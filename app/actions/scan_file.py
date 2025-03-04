from pathlib import Path
from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.chain.storage import StorageChain
from app.core.config import global_vars, settings
from app.log import logger
from app.schemas import ActionParams, ActionContext


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

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self.storagechain = StorageChain()
        self._fileitems = []
        self._has_error = False

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "扫描目录"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "扫描目录文件到队列"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
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
            # 检查缓存
            cache_key = f"{file.path}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{file.path} 已处理过，跳过")
                continue
            self._fileitems.append(fileitem)
            # 保存缓存
            self.save_cache(workflow_id, cache_key)

        if self._fileitems:
            context.fileitems.extend(self._fileitems)

        self.job_done(f"扫描到 {len(self._fileitems)} 个文件")
        return context
