"""
下载历史原子写入与持久化结算 owner。
"""

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
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.types import (
    EventType,
    MediaType,
    NotificationChannel,
)


class DownloadHistoryOwner(_DownloadOwnerBase):
    """
    下载历史原子写入与持久化结算 owner。
    """

    @staticmethod
    def _build_download_note(
            source: Optional[str],
            media: MediaInfo | MusicInfo,
            meta: MetaBase,
    ) -> dict[str, Any]:
        """
        构造下载历史备注，并为音乐保存可恢复的版本化上下文。
        """
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
            already_exists: bool = False,
    ) -> None:
        """
        提交下载历史、文件明细和 durable 下载事件。

        :param already_exists: 下载器中已存在同一任务时为 True，仅补记历史，不再重复通知与文件明细；
                               若整包全部媒体文件已记录过，同时为这个资源登记提交退避
        """
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
            type=(media.type.value if media.type else ""),
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
            note={**self._build_download_note(source, media, meta),
                  **({"subscription_download": context.subscription_download} if context.subscription_download else {})},
            custom_words=custom_words,
        )
        files_to_add: list[DownloadFileWrite] = []
        custom_word_list = custom_words.splitlines() if custom_words else []
        for file in file_list:
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
        if already_exists:
            retained_files = self._filter_unrecorded_download_files(files_to_add, download_hash)
            if files_to_add and not retained_files:
                # 必须核对整包文件，不能因本次只选了已记录的一集就阻止以后补下包内其他集。
                self._record_duplicate_download_backoff(
                    context=context,
                    downloader=downloader,
                    source=source,
                    episodes=episodes,
                )
            files_to_add = retained_files
        if episodes:
            selected_files = []
            for item in files_to_add:
                # 与订阅选集使用相同识别词，避免偏移前的文件集数被目标集数过滤。
                file_meta = MetaInfo(Path(cast(str, item.filepath)).stem, custom_words=custom_word_list)
                if file_meta.begin_episode and file_meta.begin_episode in episodes:
                    selected_files.append(item)
            files_to_add = selected_files
        frozen_files = tuple(files_to_add)
        event_payload = {
            "hash": download_hash, "context": context, "username": username,
            "downloader": downloader, "episodes": episodes or meta.episode_list, "source": source,
        }
        notification = None if already_exists else self._build_download_notification(
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

    def _filter_unrecorded_download_files(
            self,
            files: list[DownloadFileWrite],
            download_hash: str,
    ) -> list[DownloadFileWrite]:
        """
        剔除同一任务 Hash 下已经记录的有效文件明细，避免重复提交写入成对副本。

        :param files: 本次提交解析出的文件明细
        :param download_hash: 下载器中的任务哈希标识
        :return: 尚未记录过的文件明细
        """
        if not files:
            return files
        recorded = {
            item.fullpath
            for item in self.download_history_repository.get_files_by_hash(download_hash, state=1)
            if item.fullpath
        }
        if not recorded:
            return files
        retained = [item for item in files if item.fullpath not in recorded]
        skipped = len(files) - len(retained)
        if skipped:
            logger.info(f"下载任务 {download_hash} 的 {skipped} 条文件明细已记录，不再重复写入")
        return retained
