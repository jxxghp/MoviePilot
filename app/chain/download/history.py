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
from app.domain import torrent as torrent_rules
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.download import DownloadTaskAction
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
            task_action: DownloadTaskAction = DownloadTaskAction.CREATED,
    ) -> None:
        """
        提交下载历史、文件明细和 durable 下载事件。

        选集文件按集号交集保留，确保跨目标边界的多集视频也有文件明细。

        下载历史、文件明细、通知与重复退避是四件不同的事，不能互相替代（#7078/#7107 的教训）：
        - 下载历史：每次受理都写，记录这一次请求；
        - 文件明细：复用已有任务时按完整路径去重，只防止重复写入同一行，不参与任何业务判断；
        - 通知：只在新建任务或真正追加了文件时发送，原样复用、仅恢复或任务被暂缓时不发"开始下载"；
        - 确认记录：note 中累积"MP 在该任务上确认选中过的目标"，用于之后识别被别人取消的文件，
          见 ``_confirmed_task_selection``；
        - 重复退避：见 ``_should_record_duplicate_backoff``。

        :param task_action: 下载器对任务的实际动作，由下载器返回的说明识别；默认按新建任务结算
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
                  **({"subscription_download": context.subscription_download} if context.subscription_download else {}),
                  torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: self._confirmed_task_selection(
                      download_hash, episodes, task_action,
                  ).to_note()},
            custom_words=custom_words,
        )
        files_to_add: list[DownloadFileWrite] = []
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
        media_file_count = len(files_to_add)
        if task_action.is_existing_task:
            retained_files = self._filter_unrecorded_download_files(files_to_add, download_hash)
            if self._should_record_duplicate_backoff(context, task_action, files_to_add, retained_files):
                self._record_duplicate_download_backoff(
                    context=context,
                    downloader=downloader,
                    source=source,
                    episodes=episodes,
                )
            files_to_add = retained_files
        if episodes:
            wanted, _unwanted, _matched = torrent_rules.select_torrent_files(
                ((index, cast(str, item.filepath)) for index, item in enumerate(files_to_add)),
                episodes, self.runtime_config.media_extensions, custom_words,
                torrent_rules.episode_selection_for_context(context),
                allow_single_fallback=media_file_count == 1,
            )
            files_to_add = [files_to_add[index] for index in wanted]
        frozen_files = tuple(files_to_add)
        event_payload = {
            "hash": download_hash, "context": context, "username": username,
            "downloader": downloader, "episodes": episodes or meta.episode_list, "source": source,
            "task_action": task_action.value,
        }
        notify = task_action in (DownloadTaskAction.CREATED, DownloadTaskAction.APPENDED)
        notification = None if not notify else self._build_download_notification(
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

    def _confirmed_task_selection(
            self, download_hash: str, episodes: Optional[Set[int]], task_action: DownloadTaskAction,
    ) -> torrent_rules.ConfirmedSelection:
        """
        计算写入本条下载历史的"MP 在该任务上确认选中过的目标"。

        下载器只有在目标确认选中后才返回 hash，因此本次请求的目标总是已确认的。新建任务从零开始，
        只记本次目标：同一 hash 的旧任务可能已被删除后重新添加，旧记录里的集在新任务上从未选中，
        若沿用会被误判为"被取消"。复用已有任务时与该任务最新一条历史中的记录合并，使记录随多次
        追加累积，下一次只需读取最新一条历史。
        这是"读取—合并—写回"，下载链提交时整段持有该任务的 ``downloader_task_locks``，两次并发追加
        不会各自读到旧记录、后写的覆盖先写的（见 ``_submit_prepared_download``）。

        :param download_hash: 下载器返回的任务 hash
        :param episodes: 本次目标集号；为空表示整个种子
        :param task_action: 下载器对任务的实际动作
        :return: 合并后的确认记录
        """
        current = torrent_rules.ConfirmedSelection.from_request(episodes)
        if not task_action.is_existing_task:
            return current
        previous = self.download_history_repository.get_by_hash(download_hash)
        if previous is None:
            return current
        return torrent_rules.ConfirmedSelection.from_note(previous.note).merge(current)

    @staticmethod
    def _should_record_duplicate_backoff(
            context: Context,
            task_action: DownloadTaskAction,
            files: list[DownloadFileWrite],
            retained_files: list[DownloadFileWrite],
    ) -> bool:
        """
        判断这次复用是否属于 #7107 要压制的"订阅每轮重投同一个已持有整包"。

        #7107 用"整包文件明细都已记录"推断"没有新增交付"，并按不含集数的资源指纹退避 7 天，
        结果同一季包后续的补集请求在选源阶段就被挡住。现在只在以下条件同时成立时才登记：
        - 下载器确认对任务没有任何推进：原样复用，或任务被暂缓（目标已选中、等暂停方恢复）；
          追加了文件或恢复了以“可恢复”意图暂停的任务都算推进，不登记；
        - 本次提交没有可记录的集数（``selected_episodes`` 为空，例如多季包），订阅成功后记不下
          任何集，下一轮必然重投同一资源；带集数的提交成功后会写入订阅进度，不会循环，登记退避
          反而会挡住同一订阅之后扩大范围时对这个包的补集；
        - 整包全部媒体文件都已记录，沿用 #7107 的保守条件。

        :param context: 本次提交的上下文，``selected_episodes`` 在提交前已冻结
        :param task_action: 下载器对任务的实际动作
        :param files: 整包解析出的全部媒体文件明细
        :param retained_files: 去重后尚未记录的文件明细
        :return: 需要登记重复提交退避时返回 True
        """
        if task_action not in (DownloadTaskAction.REUSED, DownloadTaskAction.DEFERRED):
            return False
        if context.selected_episodes:
            return False
        return bool(files) and not retained_files

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
