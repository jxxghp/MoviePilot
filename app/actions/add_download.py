from pydantic import Field

from app.actions import BaseAction
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import ActionParams, ActionContext, DownloadTask


class AddDownloadParams(ActionParams):
    """
    添加下载资源参数
    """
    downloader: str = Field(None, description="下载器")
    save_path: str = Field(None, description="保存路径")


class AddDownloadAction(BaseAction):
    """
    添加下载资源
    """

    # 已添加的下载
    _added_downloads = []

    def __init__(self):
        super().__init__()
        self.downloadchain = DownloadChain()
        self.mediachain = MediaChain()

    @property
    def name(self) -> str:
        return "添加下载"

    @property
    def description(self) -> str:
        return "根据资源列表添加下载任务"

    @property
    def data(self) -> dict:
        return AddDownloadParams().dict()

    @property
    def success(self) -> bool:
        return True if self._added_downloads else False

    async def execute(self, params: AddDownloadParams, context: ActionContext) -> ActionContext:
        """
        将上下文中的torrents添加到下载任务中
        """
        for t in context.torrents:
            if not t.meta_info:
                t.meta_info = MetaInfo(title=t.title, subtitle=t.description)
            if not t.media_info:
                t.media_info = self.mediachain.recognize_media(meta=t.meta_info)
            if not t.media_info:
                logger.warning(f"{t.title} 未识别到媒体信息，无法下载")
                continue
            did = self.downloadchain.download_single(context=t,
                                                     downloader=params.downloader,
                                                     save_path=params.save_path)
            if did:
                self._added_downloads.append(did)

        if self._added_downloads:
            logger.info(f"已添加 {len(self._added_downloads)} 个下载任务")
            context.downloads.extend(
                [DownloadTask(download_id=did, downloader=params.downloader) for did in self._added_downloads]
            )

        self.job_done()
        return context
