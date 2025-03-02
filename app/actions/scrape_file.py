from pathlib import Path

from app.actions import BaseAction
from app.core.config import global_vars
from app.schemas import ActionParams, ActionContext
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.core.metainfo import MetaInfoPath
from app.log import logger


class ScrapeFileParams(ActionParams):
    """
    刮削文件参数
    """
    pass


class ScrapeFileAction(BaseAction):
    """
    刮削文件
    """

    _scraped_files = []
    _has_error = False

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self.storagechain = StorageChain()
        self.mediachain = MediaChain()

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
        return ScrapeFileParams().dict()

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
            if not self.storagechain.exists(fileitem):
                continue
            # 检查缓存
            cache_key = f"{fileitem.path}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{fileitem.path} 已刮削过，跳过")
                continue
            meta = MetaInfoPath(Path(fileitem.path))
            mediainfo = self.mediachain.recognize_media(meta)
            if not mediainfo:
                _failed_count += 1
                logger.info(f"{fileitem.path} 未识别到媒体信息，无法刮削")
                continue
            self.mediachain.scrape_metadata(fileitem=fileitem, meta=meta, mediainfo=mediainfo)
            self._scraped_files.append(fileitem)
            # 保存缓存
            self.save_cache(workflow_id, cache_key)

        if not self._scraped_files and _failed_count:
            self._has_error = True

        self.job_done(f"成功刮削 {len(self._scraped_files)} 个文件，失败 {_failed_count} 个")
        return context
