"""下载器任务查询和控制 owner。"""

from typing import Any, List, Optional, Union

from app.application.download.tasks import DownloadTaskService
from app.chain.download.contract import _DownloadOwnerBase
from app.foundation import size as size_tools
from app.runtime.events import Event
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.transfer import DownloaderTorrent
from app.schemas.transfer import DownloaderTorrent as _SchemaDownloaderTorrent
from app.schemas.types import (
    EventType,
    MessageType,
    NotificationChannel,
    TorrentStatus,
)


class DownloadTaskOwner(_DownloadOwnerBase):
    """下载器任务查询和控制 owner。"""


    def remote_downloading(
        self,
        channel: NotificationChannel,
        userid: Union[str, int, None] = None,
        source: Optional[str] = None,
    ) -> None:
        """
        查询正在下载的任务，并发送消息
        """
        torrents = self.list_torrents(status=TorrentStatus.DOWNLOADING)
        if not torrents:
            self.post_message(Message(
                channel=channel,
                source=source,
                mtype=MessageType.Download,
                title="没有正在下载的任务！",
                userid=userid,
                link=self.runtime_config.downloading_url,
                save_history=False,
            ))
            return
        # 发送消息
        title = f"共 {len(torrents)} 个任务正在下载："
        messages = []
        index = 1
        for torrent in torrents:
            messages.append(f"{index}. {torrent.title} "
                            f"{size_tools.format_compact_size(torrent.size or 0)} "
                            f"{round(torrent.progress or 0, 1)}%")
            index += 1
        self.post_message(Message(
            channel=channel,
            source=source,
            mtype=MessageType.Download,
            title=title,
            text="\n".join(messages),
            userid=userid,
            link=self.runtime_config.downloading_url,
            save_history=False,
        ))

    def downloading(self, name: Optional[str] = None) -> List[DownloaderTorrent]:
        """
        查询正在下载的任务
        """
        return self._download_task_service().downloading(name)

    def set_downloading(self, hash_str: str, oper: str, name: Optional[str] = None) -> bool:
        """
        控制下载任务 start/stop
        """
        return self._download_task_service().set_downloading(
            hash_str,
            oper,
            name,
        )

    def remove_downloading(self, hash_str: str, name: Optional[str] = None) -> bool:
        """
        删除下载任务
        """
        return self._download_task_service().remove_downloading(hash_str, name)

    def _download_task_service(self) -> DownloadTaskService:
        """构造绑定当前下载器能力与历史仓储的任务服务。"""
        def list_torrents(**kwargs: Any) -> List[DownloaderTorrent]:
            """把下载器允许的空结果归一为应用服务要求的列表。"""
            return self.list_torrents(**kwargs) or []

        return DownloadTaskService(
            list_torrents=list_torrents,
            get_history_by_hashes=self.download_history_repository.get_by_hashes,
            start_torrents=self.start_torrents,
            stop_torrents=self.stop_torrents,
            remove_torrents=self.remove_torrents,
        )

    def _download_file_deleted(self, event: Event) -> None:
        """
        下载文件删除时，同步删除下载任务
        """
        if not event:
            return
        hash_str = event.event_data.get("hash")
        if not hash_str:
            return
        logger.warn(f"检测到下载源文件被删除，删除下载任务（不含文件）：{hash_str}")
        # 先查询种子
        torrents: List[_SchemaDownloaderTorrent] = self.list_torrents(hashs=[hash_str]) or []
        if torrents:
            self.remove_torrents(hashs=[hash_str], delete_file=False)
            # 发出下载任务删除事件，如需处理辅种，可监听该事件
            self.eventmanager.send_event(EventType.DownloadDeleted, {
                "hash": hash_str,
                    "torrents": [torrent.model_dump() for torrent in torrents]
            })
        else:
            logger.info(f"没有在下载器中查询到 {hash_str} 对应的下载任务")
