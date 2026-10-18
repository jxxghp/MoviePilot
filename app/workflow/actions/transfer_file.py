"""
按整理策略处理工作流中的文件。
"""

import copy
from pathlib import Path
from typing import Optional

from pydantic import Field

from app.chain.storage import StorageChain
from app.chain.transfer.facade import TransferChain
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.file import FileItem
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


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

    contract = {
        "inputs": [
            {"name": "downloads", "label": "下载任务", "kind": "list"},
            {"name": "fileitems", "label": "文件", "kind": "list"},
        ],
        "outputs": [{"name": "fileitems", "label": "文件", "kind": "list"}],
        "concurrency_key": "transfer",
    }

    def __init__(self, action_id: str) -> None:
        """
        初始化当前动作的结果集合与失败状态。
        """
        super().__init__(action_id)
        self._fileitems: list[FileItem] = []
        self._has_error = False

    name = "整理文件"
    description = "整理队列中的文件"
    data = TransferFileParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回动作是否未遇到终止性的处理失败。
        """
        return not self._has_error

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        从 downloads / fileitems 中整理文件，记录到fileitems
        """

        def check_continue() -> bool:
            """
            检查是否继续整理文件
            """
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                return False
            return True

        action_params = TransferFileParams.model_validate(params)
        # 失败次数
        _failed_count = 0
        storagechain = StorageChain()
        transferchain = TransferChain()
        transferhis = transferchain.transfer_history_repository
        if action_params.source == "downloads":
            # 从下载任务中整理文件
            for download in context.downloads or []:
                if runtime_stop_state.is_workflow_stopped(workflow_id):
                    break
                if not download.completed:
                    logger.info(f"下载任务 {download.download_id} 未完成")
                    continue
                # 检查缓存
                cache_key = f"{download.download_id}"
                if self.check_cache(workflow_id, cache_key):
                    logger.info(f"{download.path} 已整理过，跳过")
                    continue
                if not download.path:
                    _failed_count += 1
                    logger.warning("已完成下载缺少文件路径，跳过整理")
                    continue
                fileitem = storagechain.get_file_item(storage="local", path=Path(download.path))
                if not fileitem:
                    logger.info(f"文件 {download.path} 不存在")
                    continue
                if not fileitem.path:
                    logger.warn("工作流文件缺少可查询的源路径，跳过")
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
            source_files = context.fileitems if context.fileitems is not None else []
            for fileitem in copy.deepcopy(source_files):
                if not check_continue():
                    break
                if not fileitem.path:
                    logger.warn("工作流文件缺少可查询的源路径，跳过")
                    continue
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
                source_files.remove(fileitem)
                self._fileitems.append(fileitem)
                # 记录已整理的文件
                self.save_cache(workflow_id, cache_key)

        return self._complete_transfer(context, _failed_count)

    def _complete_transfer(self, context: ActionContext, failed_count: int) -> ActionContext:
        """
        写回成功文件并结算动作，延续部分成功时允许后续节点执行的规则。
        """
        if self._fileitems:
            if context.fileitems is None:
                context.fileitems = []
            context.fileitems.extend(self._fileitems)
        elif failed_count:
            self._has_error = True

        self.job_done(f"整理成功 {len(self._fileitems)} 个文件，失败 {failed_count} 个")
        return context
