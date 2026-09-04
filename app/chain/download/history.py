"""下载历史原子写入与持久化结算 owner。"""

import time
from pathlib import Path
from typing import Any, Optional, Set, Union, cast

from app.application.chain.events import (
    snapshot_download_notification,
    snapshot_download_processing,
)
from app.application.classification.reference import effective_classification_snapshot
from app.application.history import DownloadFileWrite, DownloadHistoryWrite
from app.chain.download.contract import _DownloadOwnerBase
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.schemas.media import resolve_media_identity
from app.schemas.types import (
    EventType,
    MediaType,
    NotificationChannel,
)


class DownloadHistoryOwner(_DownloadOwnerBase):
    """下载历史原子写入与持久化结算 owner。"""


    @staticmethod
    def _build_download_note(
            source: Optional[str],
            media: MediaInfo | MusicInfo,
            meta: MetaBase,
    ) -> dict[str, Any]:
        """构造下载历史备注，并为音乐保存可恢复的版本化上下文。"""
        note: dict[str, Any] = {"source": source}
        if getattr(media, "type", None) != MediaType.MUSIC:
            return note
        media_payload = media.to_dict()
        media_payload.pop("raw_data", None)
        note["music"] = {
            "version": 1,
            "meta": cast(Any, meta).to_dict(),
            "media": media_payload,
        }
        return note

    def _settle_download_success(
            self,
            *,
            context: Context,
            media: MediaInfo | MusicInfo,
            meta: MetaBase,
            torrent: TorrentInfo,
            folder_name: str,
            file_list: list[str],
            download_dir: Path,
            layout: Optional[str],
            downloader: Optional[str],
            download_hash: str,
            download_episodes: Optional[str],
            episodes: Optional[Set[int]],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            userid: Union[str, int, None],
            username: Optional[str],
            torrent_content: Union[str, bytes],
            custom_words: Optional[str],
    ) -> None:
        """提交下载历史、文件明细和 durable 下载事件。"""
        if layout == "NoSubfolder" or not folder_name:
            download_path = download_dir / file_list[0] if file_list else download_dir
        elif folder_name:
            download_path = download_dir / folder_name
        else:
            download_path = download_dir / Path(file_list[0]).stem if file_list else download_dir
        save_path = download_dir if layout == "NoSubfolder" or not folder_name else download_path
        media_source, media_id = resolve_media_identity(media=media)
        classification = effective_classification_snapshot(media)
        history = DownloadHistoryWrite(
            path=download_path.as_posix(),
            type=media.type.value,
            title=media.title or "",
            year=str(media.year) if media.year is not None else None,
            media_source=media_source,
            media_id=media_id,
            music_type=getattr(media, "music_type", None),
            seasons=meta.season,
            episodes=download_episodes or meta.episode,
            image=media.get_backdrop_image(),
            poster=media.get_poster_image(),
            downloader=downloader,
            download_hash=download_hash,
            torrent_name=torrent.title,
            torrent_description=torrent.description,
            torrent_site=torrent.site_name,
            userid=userid,
            username=username,
            channel=channel.value if channel else None,
            date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            media_category_id=classification.category_id,
            media_category=classification.path,
            classification_rule_id=classification.rule_id,
            classification_policy_revision=classification.policy_revision,
            classification_source=classification.source,
            episode_group=media.episode_group,
            note=self._build_download_note(source, media, meta),
            custom_words=custom_words,
        )
        files_to_add: list[DownloadFileWrite] = []
        for file in file_list:
            if episodes:
                file_meta = MetaInfo(Path(file).stem)
                if not file_meta.begin_episode or file_meta.begin_episode not in episodes:
                    continue
            if not Path(file).suffix or Path(file).suffix.lower() not in self.runtime_config.media_extensions:
                continue
            files_to_add.append(
                DownloadFileWrite(
                    download_hash=download_hash,
                    downloader=downloader,
                    fullpath=(save_path / file).as_posix(),
                    savepath=save_path.as_posix(),
                    filepath=file,
                    torrentname=meta.org_string,
                )
            )
        frozen_files = tuple(files_to_add)
        event_payload = {
            "hash": download_hash, "context": context, "username": username,
            "downloader": downloader, "episodes": episodes or meta.episode_list, "source": source,
        }
        notification = self._build_download_notification(
            media=media,
            meta=meta,
            torrent=torrent,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            download_episodes=download_episodes,
        )
        notification_payload = snapshot_download_notification(notification)
        processing_payload = snapshot_download_processing(
            context=context,
            download_dir=download_dir,
            torrent_content=torrent_content,
            download_hash=download_hash,
            downloader=downloader,
        )

        durable_event_writer = getattr(self, "durable_event_writer", None)
        if durable_event_writer:
            durable_event_writer.download_added(
                history=history,
                files=frozen_files,
                event_payload=event_payload,
                notification_payload=notification_payload,
                processing_payload=processing_payload,
                publish=lambda payload: self.eventmanager.send_event(EventType.DownloadAdded, payload),
            )
            return
        self.download_history_repository.add(history, frozen_files)
        if notification is not None:
            self.post_message(notification)
        self._submit_download_added_task(
            context=context,
            download_dir=download_dir,
            torrent_content=torrent_content,
            download_hash=download_hash,
            downloader=downloader,
        )
        self.eventmanager.send_event(EventType.DownloadAdded, event_payload)
