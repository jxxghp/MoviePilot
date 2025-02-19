from app.actions import BaseAction
from app.chain.download import DownloadChain
from app.schemas import ActionParams, ActionContext


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
        self.downloadchain = DownloadChain()

    @property
    def name(self) -> str:
        return "获取下载任务"

    @property
    def description(self) -> str:
        return "获取下载任务，更新任务状态"

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
            torrents = self.downloadchain.list_torrents(hashs=[download.download_id])
            if not torrents:
                download.completed = True
                continue
            for t in torrents:
                if t.progress >= 100:
                    download.completed = True

        self.job_done()
        return context
