"""历史提交后的通知与附加处理 owner。"""

from pathlib import Path
from typing import Union

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


    def _after_download_history_commit(
        self,
        *,
        context: Context,
        media: MediaInfo | MusicInfo,
        meta: MetaBase,
        torrent: TorrentInfo,
        channel: NotificationChannel | None,
        source: str | None,
        userid: str | None,
        username: str | None,
        download_episodes: list[int] | None,
        download_dir: Path,
        torrent_content: bytes,
    ) -> None:
        """保持下载历史提交后的通知和后处理顺序。"""
        self.post_message(
            Message(
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
        )
        self._submit_download_added_task(
            context=context,
            download_dir=download_dir,
            torrent_content=torrent_content,
        )

    def _submit_download_added_task(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes],
    ) -> None:
        """
        后台执行下载成功后的附加处理，避免站点字幕下载阻塞添加下载响应。
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
                )
            except Exception as e:
                logger.error(f"执行下载成功后处理失败：{str(e)}")

        try:
            ThreadHelper().submit(_run_download_added)
        except Exception as err:
            logger.error(f"提交下载成功后处理后台任务失败：{str(err)}")
