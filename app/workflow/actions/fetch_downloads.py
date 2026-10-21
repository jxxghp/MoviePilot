"""
读取下载任务并写入工作流上下文。
"""

from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.download import DownloadTask
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import ActionChain, BaseAction


class FetchDownloadsParams(ActionParams):
    """
    获取下载任务参数
    """
    pass


class FetchDownloadsAction(BaseAction):
    """
    获取下载任务
    """

    contract = {
        "inputs": [{"name": "downloads", "label": "下载任务", "kind": "list"}],
        "outputs": [{"name": "downloads", "label": "下载任务", "kind": "list", "merge": "replace"}],
        "concurrency_key": "download",
    }

    def __init__(self, action_id: str) -> None:
        """
        初始化当前动作独立的执行状态。
        """
        super().__init__(action_id)
        self._downloads: list[DownloadTask] = []

    name = "获取下载任务"
    description = "获取下载队列中的任务状态"
    data = FetchDownloadsParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回当前动作是否已达到完成条件。
        """
        return self.done

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        更新downloads中的下载任务状态
        """
        self._downloads = context.downloads or []
        if not self._downloads:
            self.job_done("无下载任务")
            return context

        for download in self._downloads:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            logger.info(f"获取下载任务 {download.download_id} 状态 ...")
            if not download.download_id:
                continue
            torrents = ActionChain().list_torrents(
                hashs=[download.download_id],
                downloader=download.downloader,
            )
            if not torrents:
                download.completed = True
                continue
            for t in torrents:
                torrent_path = t.path
                download.path = str(torrent_path) if torrent_path is not None else None
                if t.progress is not None and t.progress >= 100:
                    logger.info(f"下载任务 {download.download_id} 已完成")
                    download.completed = True
                else:
                    logger.info(f"下载任务 {download.download_id} 未完成")
                    download.completed = False
        if all([d.completed for d in self._downloads]):
            self.job_done("下载任务已全部完成")
        return context
