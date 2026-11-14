"""
为工作流中的文件刮削媒体资料与图片。
"""

from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain
from app.chain.storage import StorageChain
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.file import FileItem
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class ScrapeFileParams(ActionParams):
    """
    刮削文件参数
    """
    pass


class ScrapeFileAction(BaseAction):
    """
    刮削文件
    """

    contract = {
        "inputs": [{"name": "fileitems", "label": "文件", "kind": "list"}],
        "outputs": [{"name": "fileitems", "label": "文件", "kind": "list"}],
    }

    def __init__(self, action_id: str) -> None:
        """
        建立当前动作的已刮削文件集合与错误状态。

        :param action_id: 当前工作流动作的标识
        """
        super().__init__(action_id)
        self._scraped_files: list[FileItem] = []
        self._has_error = False

    name = "刮削文件"
    description = "刮削媒体信息和图片"
    data = ScrapeFileParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回文件刮削过程是否未记录错误。

        :return: 未遇到刮削错误时为 True
        """
        return not self._has_error

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        刮削上下文中的文件并记录成功项。

        :param workflow_id: 当前工作流标识，用于检查停止状态与缓存
        :param params: 动作参数；此动作沿用上下文中的文件列表
        :param context: 包含待刮削文件的工作流上下文
        :return: 原工作流上下文，动作状态由成功数与失败数更新
        """
        # 失败次数
        _failed_count = 0
        for fileitem in context.fileitems or []:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            if fileitem in self._scraped_files:
                continue
            if not StorageChain().exists(fileitem):
                continue
            # 检查缓存
            cache_key = f"{fileitem.path}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{fileitem.path} 已刮削过，跳过")
                continue
            mediachain = MediaChain()
            media_context = mediachain.recognize_by_path(
                fileitem.path,
                obtain_images=True,
            )
            if not media_context or not media_context.media_info:
                _failed_count += 1
                logger.info(f"{fileitem.path} 未识别到媒体信息，无法刮削")
                continue
            scrape_result = ScrapingChain().scrape_metadata(
                fileitem=fileitem,
                meta=media_context.meta_info,
                mediainfo=media_context.media_info
            )
            if isinstance(scrape_result, tuple) and not scrape_result[0]:
                _failed_count += 1
                logger.info(
                    f"{fileitem.path} 刮削失败：{scrape_result[1] or '未知错误'}"
                )
                continue
            self._scraped_files.append(fileitem)
            # 保存缓存
            self.save_cache(workflow_id, cache_key)

        if not self._scraped_files and _failed_count:
            self._has_error = True

        self.job_done(f"成功刮削 {len(self._scraped_files)} 个文件，失败 {_failed_count} 个")
        return context
