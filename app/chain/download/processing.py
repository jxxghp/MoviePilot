"""历史提交后的通知与附加处理 owner。"""

from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from app.application.messaging.message import MessageTemplateHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.runtime.thread import ThreadHelper
from app.schemas.message import Message
from app.schemas.types import (
    ContentType,
    MessageType,
    NotificationChannel,
)


class DownloadProcessingOwner(_DownloadOwnerBase):
    """历史提交后的通知与附加处理 owner。"""

    def _build_download_notification(
        self,
        *,
        media: MediaInfo | MusicInfo,
        meta: MetaBase,
        torrent: TorrentInfo,
        channel: NotificationChannel | None,
        source: str | None,
        userid: str | int | None,
        username: str | None,
        download_episodes: str | None,
    ) -> Optional[Message]:
        """提交前冻结下载通知，供即时路径与 durable 恢复共用。"""
        return MessageTemplateHelper.render(
            message=Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Download,
                ctype=ContentType.DownloadAdded,
                image=media.get_message_image(),
                link=self.runtime_config.downloading_url,
                userid=userid,
                username=username,
            ),
            meta=meta,
            mediainfo=media,
            torrentinfo=torrent,
            download_episodes=download_episodes,
            username=username,
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _submit_download_added_task(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes],
            download_hash: Optional[str] = None,
            downloader: Optional[str] = None,
    ) -> None:
        """
        后台执行下载成功后的附加处理，并传递下载器身份以解析实际内容路径。

        TempPath 下载器会在完成前把内容放在不同于 save_path 的目录中；
        字幕处理需要该身份才能把字幕写入当前内容目录，随下载器迁移一起移动。
        """

        def _run_download_added() -> None:
            try:
                self.download_added(
                    context=context,
                    download_dir=download_dir,
                    torrent_content=torrent_content,
                )
                self.download_site_subtitles(
                    context=context,
                    download_dir=download_dir,
                    torrent_content=torrent_content,
                    download_hash=download_hash,
                    downloader=downloader,
                )
            except Exception as e:
                logger.error(f"执行下载成功后处理失败：{str(e)}")

        try:
            ThreadHelper().submit(_run_download_added)
        except Exception as err:
            logger.error(f"提交下载成功后处理后台任务失败：{str(err)}")
