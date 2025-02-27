from app.actions import BaseAction, ActionChain
from app.schemas import ActionParams, ActionContext
from app.log import logger


class FetchDownloadsParams(ActionParams):
    """
    获取下载任务参数
    """
    pass


class FetchDownloadsAction(BaseAction):
    """
    获取下载任务
    """

    _downloads = []

    def __init__(self):
        super().__init__()
        self.chain = ActionChain()

    @property
    def name(self) -> str:
        return "获取下载任务"

    @property
    def description(self) -> str:
        return "获取下载任务，更新任务状态"

    @property
    def data(self) -> dict:
        return FetchDownloadsParams().dict()

    @property
    def success(self) -> bool:
        if not self._downloads:
            return True
        return True if all([d.completed for d in self._downloads]) else False

    async def execute(self, params: FetchDownloadsParams, context: ActionContext) -> ActionContext:
        """
        更新downloads中的下载任务状态
        """
        self._downloads = context.downloads
        for download in self._downloads:
            logger.info(f"获取下载任务 {download.download_id} 状态 ...")
            torrents = self.chain.list_torrents(hashs=[download.download_id])
            if not torrents:
                download.completed = True
                continue
            for t in torrents:
                download.path = t.path
                if t.progress >= 100:
                    logger.info(f"下载任务 {download.download_id} 已完成")
                    download.completed = True

        self.job_done()
        return context
