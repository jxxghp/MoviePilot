"""
扫描指定目录并将文件条目加入工作流上下文。
"""

from pathlib import Path
from typing import Optional

from pydantic import Field

from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.storage import StorageChain
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.file import FileItem
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class ScanFileParams(ActionParams):
    """
    整理文件参数
    """
    # 存储
    storage: Optional[str] = Field(default="local", description="存储")
    directory: Optional[str] = Field(default=None, description="目录")


class ScanFileAction(BaseAction):
    """
    整理文件
    """

    contract = {
        "outputs": [{"name": "fileitems", "label": "文件", "kind": "list"}],
    }

    def __init__(self, action_id: str) -> None:
        """
        初始化当前动作的结果集合与失败状态。
        """
        super().__init__(action_id)
        self._fileitems: list[FileItem] = []
        self._has_error = False

    name = "扫描目录"
    description = "扫描目录文件到队列"
    data = ScanFileParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回动作是否未遇到终止性的处理失败。
        """
        return not self._has_error

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        扫描目录中的所有文件，记录到fileitems
        """
        action_params = ScanFileParams.model_validate(params)
        if not action_params.storage or not action_params.directory:
            return context
        storagechain = StorageChain()
        fileitem = storagechain.get_file_item(action_params.storage, Path(action_params.directory))
        if not fileitem:
            logger.error(f"目录不存在: 【{action_params.storage}】{action_params.directory}")
            self._has_error = True
            return context
        files = storagechain.list_files(fileitem, recursion=True)
        if files is None:
            self._has_error = True
            self.job_done("目录读取失败")
            return context
        runtime_config = get_chain_runtime_config_snapshot()
        media_exts = (
            runtime_config.media_extensions
            + runtime_config.subtitle_extensions
            + runtime_config.audio_extensions
        )
        for file in files:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            if not file.extension or f".{file.extension.lower()}" not in media_exts:
                continue
            # 添加文件到队列，而不是目录
            self._fileitems.append(file)

        if self._fileitems:
            if context.fileitems is None:
                context.fileitems = []
            context.fileitems.extend(self._fileitems)

        self.job_done(f"扫描到 {len(self._fileitems)} 个文件")
        return context
