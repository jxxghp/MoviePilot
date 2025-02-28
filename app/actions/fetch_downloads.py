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

    @classmethod
    @property
    def name(cls) -> str:
        return "获取下载任务"

    @classmethod
    @property
    def description(cls) -> str:
        return "获取下载任务，更新任务状态"

    @classmethod
    @property
    def data(cls) -> dict:
        return FetchDownloadsParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, params: dict, context: ActionContext) -> ActionContext:
        """
        更新downloads中的下载任务状态
        """
        __all_complete = False
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
        if all([d.completed for d in self._downloads]):
            self.job_done()
        return context
