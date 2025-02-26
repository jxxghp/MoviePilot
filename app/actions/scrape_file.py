from app.actions import BaseAction
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

    __scraped_files = []

    def __init__(self):
        super().__init__()
        self.storagechain = StorageChain()
        self.mediachain = MediaChain()

    @property
    def name(self) -> str:
        return "刮削文件"

    @property
    def description(self) -> str:
        return "刮削媒体信息和图片"

    @property
    def success(self) -> bool:
        return True if self.__scraped_files else False

    async def execute(self, params: ScrapeFileParams, context: ActionContext) -> ActionContext:
        """
        刮削fileitems中的所有文件
        """
        for fileitem in context.fileitems:
            if fileitem in self.__scraped_files:
                continue
            if not self.storagechain.exists(fileitem):
                continue
            meta = MetaInfoPath(fileitem.path)
            mediainfo = self.chain.recognize_media(meta)
            if not mediainfo:
                logger.info(f"{fileitem.path} 未识别到媒体信息，无法刮削")
                continue
            self.mediachain.scrape_metadata(fileitem=fileitem, meta=meta, mediainfo=mediainfo)
            self.__scraped_files.append(fileitem)

        self.job_done()
        return context
