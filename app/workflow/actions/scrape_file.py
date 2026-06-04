from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.core.config import global_vars
from app.log import logger
from app.schemas import ActionParams, ActionContext
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

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._scraped_files = []
        self._has_error = False

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "刮削文件"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "刮削媒体信息和图片"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return ScrapeFileParams().model_dump()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        刮削fileitems中的所有文件
        """
        # 失败次数
        _failed_count = 0
        for fileitem in context.fileitems:
            if global_vars.is_workflow_stopped(workflow_id):
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
            mediachain.scrape_metadata(
                fileitem=fileitem,
                meta=media_context.meta_info,
                mediainfo=media_context.media_info
            )
            self._scraped_files.append(fileitem)
            # 保存缓存
            self.save_cache(workflow_id, cache_key)

        if not self._scraped_files and _failed_count:
            self._has_error = True

        self.job_done(f"成功刮削 {len(self._scraped_files)} 个文件，失败 {_failed_count} 个")
        return context
