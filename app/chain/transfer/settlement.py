"""整理终态结算、历史事件与失败通知。"""

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from app.application.chain.events import TransferResultSettlement
from app.application.configuration import get_configured_system_config
from app.application.history import (
    TransferHistorySnapshot,
    TransferHistoryStagingPort,
    add_transfer_fail,
    add_transfer_success,
    clear_transfer_failures,
    record_transfer_failure,
)
from app.application.outbox import (
    AUDIO_TRANSFER_COMPLETED_TOPIC,
    AUDIO_TRANSFER_FAILED_TOPIC,
    SUBTITLE_TRANSFER_COMPLETED_TOPIC,
    SUBTITLE_TRANSFER_FAILED_TOPIC,
    TRANSFER_COMPLETED_TOPIC,
    TRANSFER_FAILED_TOPIC,
)
from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferSettlementResult,
)
from app.application.transfer.workflow import (
    TransferFailureNotification,
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferTask,
    build_transfer_failure_group_key,
    job_lock,
)
from app.chain.storage import StorageChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain import episode as episode_rules
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.errors import public_error_message
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.schemas.message import Message
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    EventType,
    MediaType,
    MessageType,
    SystemConfigKey,
)


class TransferSettlementOwner(_TransferOwnerBase):
    """唯一持有整理终态结算、历史事件和失败通知。"""

    @staticmethod
    def _TransferChain__transfer_plan_fingerprint(checkpoint: TransferPlanCheckpoint) -> str:
        """由完整冻结计划生成执行步骤使用的稳定 SHA-256 身份。"""
        return checkpoint.fingerprint

    @staticmethod
    def _transfer_result_payload(
        task: TransferTask,
        transferinfo: TransferInfo,
        history_id: int | None = None,
    ) -> dict[str, Any]:
        """构造保持插件旧对象字段不变的整理结果事件 payload。"""
        return {
            "fileitem": task.fileitem,
            "meta": task.meta,
            "mediainfo": task.mediainfo,
            "transferinfo": transferinfo,
            "downloader": task.downloader,
            "download_hash": task.download_hash,
            "transfer_history_id": history_id,
        }

    def _durable_transfer_event(
        self,
        task: TransferTask,
        *,
        success: bool,
    ) -> Optional[tuple[str, EventType]]:
        """返回当前整理结果应持久化的 topic 与兼容事件类型。"""
        if success:
            if self._is_primary_media_file(task.fileitem, task.mediainfo):
                return TRANSFER_COMPLETED_TOPIC, EventType.TransferComplete
            if self._is_subtitle_file(task.fileitem):
                return (
                    SUBTITLE_TRANSFER_COMPLETED_TOPIC,
                    EventType.SubtitleTransferComplete,
                )
            if self._is_audio_file(task.fileitem):
                return AUDIO_TRANSFER_COMPLETED_TOPIC, EventType.AudioTransferComplete
            return None
        if self._is_media_file(task.fileitem):
            return TRANSFER_FAILED_TOPIC, EventType.TransferFailed
        if self._is_subtitle_file(task.fileitem):
            return SUBTITLE_TRANSFER_FAILED_TOPIC, EventType.SubtitleTransferFailed
        if self._is_audio_file(task.fileitem):
            return AUDIO_TRANSFER_FAILED_TOPIC, EventType.AudioTransferFailed
        return None

    @staticmethod
    def _TransferChain__build_transfer_result_settlement(
            task: TransferTask,
            transferinfo: TransferInfo,
            *,
            overwrite_declined: bool = False,
    ) -> Optional[TransferResultSettlement]:
        """由执行结果与已核实的覆盖裁决构造受 lease fencing 保护的终态命令。"""
        checkpoint = task.execution_checkpoint
        if checkpoint is None:
            if task.preview:
                return None
            raise RuntimeError("非预览整理终态缺少持久执行检查点")
        if not task.admission_task_id or not task.lease_token:
            raise TransferLeaseLostError("整理终态缺少持久任务身份或租约")
        successful_outcome = bool(transferinfo.success or overwrite_declined)
        settlement_outcome = "succeeded" if successful_outcome else "failed"
        checkpoint.validate_settlement_outcome(settlement_outcome)
        frozen_transferinfo = checkpoint.payload.get("transferinfo")
        if (
                isinstance(frozen_transferinfo, dict)
                and frozen_transferinfo != transferinfo.model_dump(mode="json")
        ):
            raise TransferExecutionConflictError(
                "整理终态与冻结 TransferInfo 不一致"
            )
        return TransferResultSettlement(
            task_id=task.admission_task_id,
            lease_token=task.lease_token,
            execution_fingerprint=checkpoint.fingerprint,
            outcome=settlement_outcome,
            error=(
                None
                if successful_outcome
                else (transferinfo.message or "整理失败")
            ),
        )

    def _TransferChain__settle_legacy_transfer_result(
            self,
            task: TransferTask,
            transferinfo: TransferInfo,
    ) -> None:
        """无公开事件地原子提交旧同步调用的历史回执与任务终态。"""
        transferhis = self.transfer_history_repository
        overwrite_declined = self._is_overwrite_declined(
            task,
            transferinfo,
            transferhis,
        )
        settlement = self._TransferChain__build_transfer_result_settlement(
            task,
            transferinfo,
            overwrite_declined=overwrite_declined,
        )
        if settlement is None:
            raise RuntimeError("旧整理兼容命令缺少可验证的执行检查点")
        writer = getattr(self, "durable_event_writer", None)
        if writer is None:
            raise RuntimeError("旧整理兼容命令缺少 durable 原子写入端口")

        def stage_history(
                staging: TransferHistoryStagingPort,
        ) -> Optional[TransferHistorySnapshot]:
            """按兼容结果暂存成功、失败或覆盖跳过的唯一历史投影。"""
            if not task.fileitem or not task.fileitem.path:
                raise ValueError("整理终态缺少源文件路径")
            if overwrite_declined:
                return staging.get_success_by_src(
                    task.fileitem.path,
                    task.fileitem.storage,
                )
            if transferinfo.success:
                return add_transfer_success(
                    fileitem=task.fileitem,
                    mode=transferinfo.transfer_type or "",
                    downloader=task.downloader,
                    download_hash=task.download_hash,
                    meta=cast(MetaBase, task.meta),
                    mediainfo=cast(Union[MediaInfo, MusicInfo], task.mediainfo),
                    transferinfo=transferinfo,
                    transfer_history_oper=staging,
                )
            return add_transfer_fail(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type or "",
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=cast(MetaBase, task.meta),
                mediainfo=task.mediainfo,
                transferinfo=transferinfo,
                transfer_history_oper=staging,
            )
        def write_result() -> Any:
            """以相同 task_id 和执行指纹提交或回读同一终态。"""
            return writer.transfer_result(
                topic=None,
                stage_history=stage_history,
                event_payload=self._transfer_result_payload(task, transferinfo),
                publish=None,
                settlement=settlement,
            )

        try:
            result = write_result()
        except Exception as first_error:
            logger.warning(
                "旧整理兼容命令首次结算响应不确定，按同一 task_id 回读 durable 回执：%s",
                first_error,
            )
            result = write_result()
        if not isinstance(result, TransferSettlementResult):
            raise RuntimeError("旧整理兼容命令没有返回 durable 结算结果")
        task.mark_terminal_settled()
        assert task.admission_task_id is not None
        assert task.lease_token is not None
        self._TransferChain__forget_owned_lease(task.admission_task_id, task.lease_token)

    @staticmethod
    def _TransferChain__transfer_history_id(
            history: Optional[
                Union[TransferHistorySnapshot, TransferSettlementResult]
            ],
    ) -> Optional[int]:
        """统一读取旧历史投影和 task-aware 结算结果的历史标识。"""
        if isinstance(history, TransferSettlementResult):
            return history.history_id
        return getattr(history, "id", None) if history is not None else None

    def _publish_transfer_result(
        self,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> None:
        """显式分派整理结果，使运行契约可追踪每种事件的生产者。"""
        if event_type is EventType.TransferComplete:
            self.eventmanager.send_event(EventType.TransferComplete, payload)
        elif event_type is EventType.TransferFailed:
            self.eventmanager.send_event(EventType.TransferFailed, payload)
        elif event_type is EventType.SubtitleTransferComplete:
            self.eventmanager.send_event(EventType.SubtitleTransferComplete, payload)
        elif event_type is EventType.SubtitleTransferFailed:
            self.eventmanager.send_event(EventType.SubtitleTransferFailed, payload)
        elif event_type is EventType.AudioTransferComplete:
            self.eventmanager.send_event(EventType.AudioTransferComplete, payload)
        elif event_type is EventType.AudioTransferFailed:
            self.eventmanager.send_event(EventType.AudioTransferFailed, payload)
        else:
            raise ValueError(f"不支持的整理结果事件：{event_type}")

    def _TransferChain__default_callback(
            self, task: TransferTask, transferinfo: TransferInfo, /
    ) -> Tuple[bool, str]:
        """
        整理完成后处理
        """
        # 状态
        ret_status = True
        # 错误信息
        ret_message = ""

        def __notify():
            """
            完成时发送消息、移除任务等
            """
            # 更新文件数量
            transferinfo.file_count = (
                    self.jobview.count(task.mediainfo, task.meta.begin_season) or 1
            )
            # 更新文件大小
            transferinfo.total_size = (
                    self.jobview.size(task.mediainfo, task.meta.begin_season)
                    or task.fileitem.size
            )
            # 发送通知，实时手动整理时不发
            if transferinfo.need_notify and (task.background or not task.manual):
                se_str = None
                if task.mediainfo.type == MediaType.TV:
                    season_episodes = self.jobview.season_episodes(
                        task.mediainfo, task.meta.begin_season
                    )
                    if season_episodes:
                        se_str = f"{task.meta.season} {episode_rules.format_ranges(season_episodes)}"
                    else:
                        se_str = f"{task.meta.season}"
                # 发送入库成功消息
                self.send_transfer_message(
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    transferinfo=transferinfo,
                    season_episode=se_str,
                    episodes_info=task.episodes_info,
                    username=task.username,
                )

        transferhis = self.transfer_history_repository
        target_dir_path = self._TransferChain__get_transfer_target_dir_path(transferinfo)
        job_id = self.jobview.get_job_id(task)
        overwrite_declined = False
        if not transferinfo.success:
            overwrite_declined = self._is_overwrite_declined(
                task, transferinfo, transferhis
            )
        settlement = self._TransferChain__build_transfer_result_settlement(
            task,
            transferinfo,
            overwrite_declined=overwrite_declined,
        )
        if settlement is None:
            raise RuntimeError("非预览整理终态无法建立 durable 结算命令")
        durable_writer = getattr(self, "durable_event_writer", None)
        if durable_writer is None:
            raise RuntimeError("非预览整理终态缺少 durable 原子写入端口")

        # 转移失败
        if not transferinfo.success:
            # 查重闸放行同路径新版本后由 overwrite_mode 判定不覆盖，是一次正常裁决而非故障：
            # 媒体库里原有版本仍然在位，写失败记录会按同源 replace 替换原成功记录，此后该路径
            # 永远处于失败态，每个新事件都会重试并重推失败通知。此时保留原记录、不写历史、
            # 不发事件与通知、不触发重试，仅把任务置为未入库
            history = None
            if overwrite_declined:
                logger.info(
                    f"{task.fileitem.name} 未入库并保留原整理记录：{transferinfo.message}"
                )
                history = durable_writer.transfer_result(
                    topic=None,
                    stage_history=lambda staging: staging.get_success_by_src(
                        task.fileitem.path,
                        task.fileitem.storage,
                    ),
                    event_payload=self._transfer_result_payload(task, transferinfo),
                    publish=None,
                    settlement=settlement,
                )
                if not isinstance(history, TransferSettlementResult):
                    raise RuntimeError("覆盖跳过的 durable 终态没有返回结算结果")
                task.mark_terminal_settled()
            else:
                logger.warn(f"{task.fileitem.name} 入库失败：{transferinfo.message}")

                durable_event = self._durable_transfer_event(task, success=False)
                topic = durable_event[0] if durable_event else None
                event_type = durable_event[1] if durable_event else None
                event_payload = self._transfer_result_payload(task, transferinfo)
                history = durable_writer.transfer_result(
                    topic=topic,
                    stage_history=lambda writer: add_transfer_fail(
                        fileitem=task.fileitem,
                        mode=transferinfo.transfer_type if transferinfo else "",
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        meta=task.meta,
                        mediainfo=task.mediainfo,
                        transferinfo=transferinfo,
                        transfer_history_oper=writer,
                    ),
                    event_payload=event_payload,
                    publish=(
                        lambda payload: self._publish_transfer_result(
                            event_type, payload
                        )
                        if event_type is not None
                        else None
                    ),
                    settlement=settlement,
                )
                if not isinstance(history, TransferSettlementResult):
                    raise RuntimeError("整理失败 durable 终态没有返回结算结果")
                task.mark_terminal_settled()

                # 失败计数不是终态真相，只能在原子历史与 pending 结算成功后更新。
                record_transfer_failure(
                    task.fileitem.path if task.fileitem else None,
                    task.fileitem.storage if task.fileitem else None,
                    file_size=task.fileitem.size if task.fileitem else None,
                    file_modify_time=task.fileitem.modify_time if task.fileitem else None,
                    fileid=task.fileitem.fileid if task.fileitem else None,
                )

                self.queue_failed_transfer_notification(
                    task=task,
                    transferinfo=transferinfo,
                    history_id=self._TransferChain__transfer_history_id(history),
                )

            # 设置任务失败
            self.jobview.fail_task(task)

            # 返回失败
            ret_status = False
            ret_message = transferinfo.message

        else:
            # 转移成功
            logger.info(f"{task.fileitem.name} 入库成功：{target_dir_path or ''}")

            durable_event = self._durable_transfer_event(task, success=True)
            topic = durable_event[0] if durable_event else None
            event_type = durable_event[1] if durable_event else None
            event_payload = self._transfer_result_payload(task, transferinfo)
            history = durable_writer.transfer_result(
                topic=topic,
                stage_history=lambda writer: add_transfer_success(
                    fileitem=task.fileitem,
                    mode=transferinfo.transfer_type if transferinfo else "",
                    downloader=task.downloader,
                    download_hash=task.download_hash,
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    transferinfo=transferinfo,
                    transfer_history_oper=writer,
                ),
                event_payload=event_payload,
                publish=(
                    lambda payload: self._publish_transfer_result(
                        event_type, payload
                    )
                    if event_type is not None
                    else None
                ),
                settlement=settlement,
            )
            if not isinstance(history, TransferSettlementResult):
                raise RuntimeError("整理成功 durable 终态没有返回结算结果")
            task.mark_terminal_settled()

            # 失败预算同样只能在 durable 成功终态提交后重置。
            clear_transfer_failures(
                task.fileitem.path if task.fileitem else None,
                task.fileitem.storage if task.fileitem else None,
            )

            # task登记转移成功文件清单
            target_files = transferinfo.file_list_new
            if target_files:
                with job_lock:
                    if self._success_target_files.get(job_id):
                        self._success_target_files[job_id].extend(target_files)
                    else:
                        self._success_target_files[job_id] = list(target_files)

            # 设置任务成功
            self.jobview.finish_task(task)

            # 登记批次级刮削目标
            self._record_scrape_target(task, transferinfo)

        # 全部整理完成且有成功的任务时，发送消息和事件
        if self.jobview.is_finished(task):
            # 更新文件清单
            with job_lock:
                transferinfo.file_list_new = list(dict.fromkeys(
                    self._success_target_files.pop(job_id, [])
                    or transferinfo.file_list_new
                    or []
                ))
            __notify()
            if not task.transfer_batch_id:
                self._send_metadata_scrape_event(task, transferinfo)

        # 只要该种子的所有任务都已整理完成，则设置种子状态为已整理
        self._TransferChain__mark_torrent_completed_if_done(task.download_hash, task.downloader)

        # 移动模式，全部成功时删除空目录和种子文件
        if transferinfo.transfer_type in ["move"]:
            # 全部整理成功时
            if self.jobview.is_success(task):
                # 所有成功的业务
                tasks = self.jobview.success_tasks(
                    task.mediainfo, task.meta.begin_season
                )
                system_config_oper = get_configured_system_config()
                # 获取整理屏蔽词
                transfer_exclude_words = system_config_oper.get(
                    SystemConfigKey.TransferExcludeWords
                )
                # 挂载盘空目录清理默认开启
                delete_mounted_local_disk_empty_dirs = system_config_oper.get(
                    SystemConfigKey.MountedLocalDiskDeleteEmptyDirs
                ) is not False
                mounted_filesystem_cache: Dict[Path, bool] = {}
                processed_hashes = set()
                for t in tasks:
                    if t.download_hash and t.download_hash not in processed_hashes:
                        # 检查该种子的所有任务（跨作业）是否都已成功
                        if self.jobview.is_torrent_success(t.download_hash):
                            processed_hashes.add(t.download_hash)
                            if self._can_delete_torrent(
                                    t.download_hash, t.downloader, transfer_exclude_words
                            ):
                                # 移除种子及文件
                                if self.remove_torrents(
                                        t.download_hash, downloader=t.downloader
                                ):
                                    logger.info(
                                        f"移动模式删除种子成功：{t.download_hash}"
                                    )
                    if (
                            not t.download_hash
                            and t.fileitem
                            and self._should_delete_empty_source_directories(
                        t,
                        delete_mounted_local_disk_empty_dirs,
                        mounted_filesystem_cache,
                    )
                    ):
                        # 删除剩余空目录
                        StorageChain().delete_media_file(t.fileitem, delete_self=False)

        return ret_status, ret_message

    def queue_failed_transfer_notification(
            self,
            *,
            task: TransferTask,
            transferinfo: TransferInfo,
            history_id: Optional[int],
            manual_identity: bool = False,
    ) -> None:
        """按配置逐条发送或按媒体聚合整理失败通知，供第三方整理补丁复用。"""
        notification = TransferFailureNotification(
            media_title=(
                task.mediainfo.title_year
                if task.mediainfo
                else task.fileitem.name if task.fileitem else "未知媒体"
            ),
            season_episode=getattr(task.meta, "season_episode", "") or "",
            reason=public_error_message(transferinfo.message, context="transfer") or "整理失败",
            history_id=history_id,
            image=(
                task.mediainfo.get_message_image()
                if task.mediainfo and hasattr(task.mediainfo, "get_message_image")
                else None
            ),
            username=task.username,
            manual_identity=manual_identity,
        )
        if not self.runtime_config.transfer_failure_notification_aggregation:
            self._send_transfer_failure_notifications([notification])
            return
        try:
            self.failure_notification_aggregator.schedule(
                group_key=build_transfer_failure_group_key(task),
                notification=notification,
                callback=self._send_transfer_failure_notifications,
                loop=main_loop_registry.require(),
            )
        except Exception as err:
            logger.error(f"加入整理失败通知聚合缓冲失败，将立即发送：{err}")
            self._send_transfer_failure_notifications([notification])

    def _send_transfer_failure_notifications(
            self,
            notifications: List[TransferFailureNotification],
    ) -> None:
        """把一个媒体分组的失败快照渲染为单条消息。"""
        if not notifications:
            return
        first = notifications[0]
        history_ids = [item.history_id for item in notifications if item.history_id]
        if len(notifications) == 1:
            history_hint = (
                (
                    "如果按钮不可用，可回复：\n"
                    f"```\n/redo {history_ids[0]}\n"
                    f"/redo {history_ids[0]} [media_source]|[media_id]|[类型]\n```\n"
                    "自动重试或手动识别整理。"
                    if first.manual_identity
                    else f"如果按钮不可用，可回复：\n```\n/redo {history_ids[0]}\n```"
                )
                if history_ids
                else ""
            )
            text = "\n".join([f"原因：{first.reason}", history_hint]).strip()
            buttons = self.build_failed_transfer_buttons(
                history_ids[0] if history_ids else None
            )
            title = (
                f"{first.media_title} 未识别到媒体信息，无法入库！"
                if first.manual_identity
                else f"{first.media_title} {first.season_episode} 入库失败！"
            )
        else:
            reason_counts = Counter(item.reason for item in notifications)
            reason_lines = [
                f"- {reason} × {count}"
                for reason, count in reason_counts.most_common()
            ]
            history_text = "、".join(f"#{history_id}" for history_id in history_ids)
            text_parts = [
                f"失败文件：{len(notifications)} 个",
                "原因统计：",
                *reason_lines,
            ]
            if history_text:
                text_parts.extend([f"整理记录：{history_text}", "可在整理历史中批量处理。"])
            text = "\n".join(text_parts)
            buttons = [[{
                "text": "批量处理",
                "url": self.runtime_config.history_url,
            }]]
            title = f"{first.media_title} 入库失败（{len(notifications)} 个文件）"
        self.post_message(
            Message(
                mtype=MessageType.Manual,
                title=title,
                text=text,
                image=first.image,
                username=first.username,
                link=self.runtime_config.history_url,
                buttons=buttons,
            )
        )

    def _TransferChain__mark_torrent_completed_if_done(
            self,
            download_hash: Optional[str],
            downloader: Optional[str],
            history_exists: bool = True,
    ):
        """
        当同一种子的任务都已结束且种子已完成下载时，回写下载器已整理标签。
        """
        if (
                not history_exists
                or not download_hash
                or not self.jobview.is_torrent_done(download_hash)
        ):
            return
        # 作业视图只包含已登记的整理任务；多集种子部分文件先下载完成时，
        # 剩余文件尚未产生任务，此时打已整理标签会使下载器轮询永久跳过
        # 剩余文件（#6009），因此必须确认种子已整体下载完成。
        if not self._TransferChain__is_torrent_download_completed(download_hash, downloader):
            logger.debug(
                f"种子 {download_hash} 尚未下载完成或状态未知，暂不设置已整理标签"
            )
            return
        if not self.jobview.is_torrent_done(download_hash):
            logger.debug(
                f"种子 {download_hash} 存在新登记的整理任务，暂不设置已整理标签"
            )
            return
        self.transfer_completed(hashs=download_hash, downloader=downloader)

    def _TransferChain__is_torrent_download_completed(
            self, download_hash: str, downloader: Optional[str]
    ) -> bool:
        """
        检查种子在下载器中是否已完成下载；查询不到或查询失败时视为未完成，
        留待下载器定时轮询兜底，避免误打已整理标签。
        """
        try:
            torrents = self.list_torrents(hashs=download_hash, downloader=downloader)
            if not torrents:
                return False
            return all((torrent.progress or 0) >= 100 for torrent in torrents)
        except Exception as e:
            logger.error(f"检查种子 {download_hash} 下载进度失败：{e}")
            return False

    def _TransferChain__finish_job_execution(
            self,
            task: TransferTask,
            *,
            terminal: bool = True,
            terminal_settlement: Optional[bool] = None,
    ) -> bool:
        """结束内存执行，只接受原子终态回执并保留未结算任务供恢复。"""
        marker = getattr(self.jobview, "finish_execution", None)
        if marker:
            marker(task)
        if task.preview:
            return True
        if terminal:
            if terminal_settlement:
                if task.admission_task_id and task.lease_token:
                    self._TransferChain__forget_owned_lease(
                        task.admission_task_id,
                        task.lease_token,
                    )
                return True
            self._TransferChain__release_task_claim(
                task,
                error="整理终态未完成 durable 原子结算",
            )
            return False
        self._TransferChain__release_task_claim(task)
        return True

    def _TransferChain__fail_transfer_task(self, task: TransferTask):
        """
        标记异常整理任务失败并清理作业视图
        """
        self.jobview.fail_unfinished_task(task)
        self.jobview.try_remove_job(task)
        self._finish_scrape_batch_task(task)
