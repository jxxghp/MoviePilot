import asyncio
import queue
import re
import threading
import time
import traceback
import uuid
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union, cast

from pydantic_core import to_jsonable_python

from app.application.chain.context import ChainRuntimeContext
from app.application.chain.events import TransferResultSettlement
from app.application.configuration import get_configured_system_config
from app.application.directory import DirectoryHelper
from app.application.formatting import FormatParser
from app.application.history import (
    DownloadHistoryQueryPort,
    DownloadHistorySnapshot,
    TransferHistorySnapshot,
    TransferHistoryStagingPort,
    add_transfer_fail,
    add_transfer_success,
    clear_transfer_failures,
    describe_history_gate,
    evaluate_history_gate,
    is_skip_action,
    max_failed_retries,
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
    TransferExecutionCheckpoint,
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionOutcome,
    TransferExecutionRepository,
    TransferExecutionSnapshot,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferSettlementResult,
    TransferStepIntent,
    TransferStepResult,
    TransferStepState,
)
from app.application.transfer.workflow import (
    JobManager,
    TransferAdmission,
    TransferFailureNotification,
    TransferFailureNotificationAggregator,
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
    TransferQueue,
    TransferQueueService,
    TransferTask,
    build_transfer_failure_group_key,
    job_lock,
)
from app.chain._transfer import (
    EpisodeFormatMixin,
    FailedRetryMixin,
    FileFilterMixin,
    FileKeyMixin,
    HistoryMatchMixin,
    ManualHistoryMixin,
    ScrapeBatchMixin,
)
from app.chain.base import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.domain import episode as episode_rules
from app.domain.context import MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metaanime import MetaAnime
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.meta.metavideo import MetaVideo
from app.domain.metainfo import MetaInfoPath
from app.foundation.singleton import Singleton
from app.runtime.config import global_vars
from app.runtime.log import logger
from app.runtime.progress import ProgressHelper
from app.runtime.reload import ConfigReloadMixin
from app.runtime.stop import runtime_stop_state
from app.schemas.event import StorageOperSelectionEventData
from app.schemas.exception import OperationInterrupted
from app.schemas.media import resolve_media_identity
from app.schemas.message import Message
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import EpisodeFormat, TransferInfo, TransferJob
from app.schemas.types import (
    ChainEventType,
    ContentType,
    EventType,
    MediaSource,
    MediaType,
    MessageType,
    NotificationChannel,
    ProgressKey,
    SystemConfigKey,
    TorrentStatus,
)
from app.schemas.workflow import FileItem

# 下载器锁
downloader_lock = threading.Lock()
# 任务锁
task_lock = threading.Lock()


class _TransferRetryDeferred(RuntimeError):
    """表示已把确定失败交回唯一持久重试调度器。"""


class _TransferManualReviewRequired(RuntimeError):
    """表示外部结果无法严格判定，任务已隔离等待人工复核。"""


class _TransferRetryExhausted(RuntimeError):
    """表示步骤重试预算耗尽，任务可进入原子失败结算。"""

    def __init__(self, message: str, snapshot: TransferExecutionSnapshot) -> None:
        """保存失败原因和数据库建立的结算检查点。"""
        super().__init__(message)
        self.snapshot = snapshot


class _DurableTransferStepRunner:
    """以稳定顺序、lease 和 attempt fencing 执行整理外部步骤。"""

    _RETRY_DELAY_SECONDS = 30

    def __init__(
            self,
            *,
            task_id: str,
            lease_token: str,
            checkpoint_fingerprint: str,
            repository: TransferExecutionRepository,
    ) -> None:
        """绑定单个任务的持久执行命令与全局步骤序号。"""
        self._task_id = task_id
        self._lease_token = lease_token
        self._checkpoint_fingerprint = checkpoint_fingerprint
        self._repository = repository
        self._command = TransferExecutionCommand(self._repository)
        snapshot = self._repository.get_snapshot(task_id=task_id)
        existing_steps = list(snapshot.steps) if snapshot else []
        matching_ordinals = [
            step.ordinal
            for step in existing_steps
            if step.checkpoint_fingerprint == checkpoint_fingerprint
        ]
        self._ordinal = (
            min(matching_ordinals)
            if matching_ordinals
            else max((step.ordinal for step in existing_steps), default=-1) + 1
        )
        self._operation_ids = [
            step.operation_id
            for step in existing_steps
            if step.ordinal < self._ordinal
        ]

    @staticmethod
    def _retry_due_at() -> str:
        """生成固定退避后的 UTC 调度时间。"""
        return (
            datetime.now(timezone.utc)
            + timedelta(seconds=_DurableTransferStepRunner._RETRY_DELAY_SECONDS)
        ).strftime("%Y-%m-%d %H:%M:%S.%f")

    def _execute_attempt(
            self,
            *,
            step: Any,
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """在事务外执行副作用，异常后先探测结果再决定重试或人工复核。"""
        try:
            result = execute()
        except Exception as error:
            try:
                observation = observe()
            except Exception as observe_error:
                observation = TransferOperationObservation(
                    state=TransferOperationObservationState.UNKNOWN,
                    evidence=TransferStepResult(payload={
                        "execute_error": str(error),
                        "observe_error": str(observe_error),
                    }),
                )
            if observation.state is TransferOperationObservationState.APPLIED:
                completed = self._command.complete(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    result=observation.evidence,
                )
                if completed.result is None:
                    raise RuntimeError("探测完成的整理步骤缺少结果证据") from error
                return completed.result
            if observation.state is not TransferOperationObservationState.NOT_APPLIED:
                reason = (
                    f"整理步骤 {step.operation_id} 执行异常后外部结果为 "
                    f"{observation.state.value}，禁止自动重放：{error}"
                )
                self._command.manual_review(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    error=reason,
                    evidence=observation.evidence,
                )
                raise _TransferManualReviewRequired(reason) from error
            snapshot = self._repository.get_snapshot(task_id=self._task_id)
            if snapshot is None:
                raise RuntimeError("整理执行快照在步骤失败后丢失") from error
            if snapshot.retry_count >= max_failed_retries():
                exhausted = self._command.exhaust(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    error=str(error),
                    evidence=observation.evidence,
                )
                raise _TransferRetryExhausted(str(error), exhausted) from error
            self._command.defer(
                task_id=self._task_id,
                lease_token=self._lease_token,
                step=step,
                error=str(error),
                retry_due_at=self._retry_due_at(),
                evidence=observation.evidence,
            )
            raise _TransferRetryDeferred(str(error)) from error
        completed = self._command.complete(
            task_id=self._task_id,
            lease_token=self._lease_token,
            step=step,
            result=result,
        )
        if completed.result is None:
            raise RuntimeError("整理步骤完成后缺少持久结果")
        return completed.result

    def run(
            self,
            *,
            phase: str,
            kind: str,
            payload: Mapping[str, Any],
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """幂等消费一个步骤，遗留 STARTED 必须先取得严格观察结论。"""
        intent = TransferStepIntent.create(
            task_id=self._task_id,
            checkpoint_fingerprint=self._checkpoint_fingerprint,
            ordinal=self._ordinal,
            phase=phase,
            kind=kind,
            payload=payload,
        )
        self._ordinal += 1
        self._operation_ids.append(intent.operation_id)
        step = self._command.prepare(
            task_id=self._task_id,
            lease_token=self._lease_token,
            intent=intent,
        )
        if step.state is TransferStepState.SUCCEEDED:
            if step.result is None:
                raise RuntimeError("已成功整理步骤缺少结果证据")
            return step.result
        if step.state is TransferStepState.MANUAL_REVIEW:
            raise _TransferManualReviewRequired(
                step.last_error or "整理步骤正在等待人工复核"
            )
        if step.state is TransferStepState.PREPARED:
            step = self._command.begin(
                task_id=self._task_id,
                lease_token=self._lease_token,
                operation_id=step.operation_id,
            )
        elif step.state is TransferStepState.FAILED:
            step = self._command.resume_failed(
                task_id=self._task_id,
                lease_token=self._lease_token,
                step=step,
            )
        elif step.state is TransferStepState.STARTED:
            observation = observe()
            if observation.state is TransferOperationObservationState.APPLIED:
                completed = self._command.complete(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    result=observation.evidence,
                )
                if completed.result is None:
                    raise RuntimeError("探测完成的整理步骤缺少结果证据")
                return completed.result
            if observation.state is TransferOperationObservationState.NOT_APPLIED:
                step = self._command.restart_after_not_applied(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    evidence=observation.evidence,
                )
            else:
                reason = (
                    f"遗留步骤 {step.operation_id} 外部结果为 "
                    f"{observation.state.value}，禁止自动重放"
                )
                self._command.manual_review(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    error=reason,
                    evidence=observation.evidence,
                )
                raise _TransferManualReviewRequired(reason)
        else:
            raise RuntimeError(f"不支持的整理步骤状态：{step.state}")
        return self._execute_attempt(step=step, execute=execute, observe=observe)

    def checkpoint(self, transferinfo: TransferInfo) -> TransferExecutionCheckpoint:
        """提交足以独立重放终态结算的聚合执行结果。"""
        outcome = (
            "overwrite_skipped"
            if transferinfo.overwrite_skipped
            else "succeeded" if transferinfo.success else "failed"
        )
        checkpoint = TransferExecutionCheckpoint.create(
            payload={
                "outcome": outcome,
                "transferinfo": transferinfo.model_dump(mode="json"),
            },
            operation_ids=tuple(self._operation_ids),
            skip_reason=(
                (transferinfo.message or "zero-side-effect transfer")
                if not self._operation_ids
                else None
            ),
        )
        snapshot = self._command.checkpoint(
            task_id=self._task_id,
            lease_token=self._lease_token,
            checkpoint=checkpoint,
        )
        if snapshot.checkpoint is None:
            raise RuntimeError("整理执行检查点提交后无法回读")
        return snapshot.checkpoint


class TransferChain(FileFilterMixin, ScrapeBatchMixin, EpisodeFormatMixin, HistoryMatchMixin, FileKeyMixin,
                    ManualHistoryMixin, FailedRetryMixin, ChainBase, ConfigReloadMixin, metaclass=Singleton):
    """
    文件整理处理链
    """

    @classmethod
    def _transfer_media_chain(cls):
        """为整理 mixin 提供可替换的媒体识别构造点。"""
        from app.chain import _transfer as _transfer_mixin
        return (_transfer_mixin.MediaChain or MediaChain)()

    @classmethod
    def _transfer_storage_chain(cls) -> StorageChain:
        """为整理 mixin 提供可替换的存储构造点。"""
        from app.chain import _transfer as _transfer_mixin
        return (_transfer_mixin.StorageChain or StorageChain)()

    @classmethod
    def _transfer_subscribe_chain(cls):
        """为整理 mixin 提供可替换的订阅构造点。"""
        from app.chain.subscribe import SubscribeChain as _SubscribeChain
        return _SubscribeChain()

    # worker 在构造期启动；若中途失败，单例仍需先发布给 lifespan 清理入口。
    _retain_failed_singleton = True

    CONFIG_WATCH = {
        "TRANSFER_THREADS",
    }

    _WORKER_RESTART_TIMEOUT_SECONDS = 30.0
    _WORKER_CLOSE_TIMEOUT_SECONDS = 30.0
    _QUEUE_STOP_SENTINEL = object()
    _WORKER_LEASE_SECONDS = 120
    _LEASE_HEARTBEAT_INTERVAL_SECONDS = 30.0
    _RECOVERY_POLL_INTERVAL_SECONDS = 15.0
    _RECOVERY_CLAIM_LIMIT = 100

    @staticmethod
    def __transfer_plan_fingerprint(checkpoint: TransferPlanCheckpoint) -> str:
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
    def __build_transfer_result_settlement(
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

    def __settle_legacy_transfer_result(
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
        settlement = self.__build_transfer_result_settlement(
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
        self.__forget_owned_lease(task.admission_task_id, task.lease_token)

    @staticmethod
    def __transfer_history_id(
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

    def __init__(
        self,
        runtime_context: Optional[ChainRuntimeContext] = None,
    ) -> None:
        """初始化文件整理处理链。"""
        super().__init__(runtime_context=runtime_context)
        # 主要媒体文件后缀
        self._media_exts = self.runtime_config.video_extensions
        # 字幕文件后缀
        self._subtitle_exts = self.runtime_config.subtitle_extensions
        # 音频文件后缀
        self._audio_exts = self.runtime_config.audio_extensions
        # 可处理的文件后缀（视频文件、字幕、音频文件和音乐歌词）
        self._allowed_exts = self._media_exts + self._audio_exts + self._subtitle_exts + (
            ".lrc", ".txt", ".yaml",
        )
        # 待整理任务队列
        self._queue = queue.Queue()
        # 文件整理线程
        self._transfer_threads = []
        # 队列间隔时间（秒）
        self._transfer_interval = 15
        # 事件管理器
        self.jobview = JobManager()
        # Agent重试管理器
        # 整理失败通知聚合器
        self.failure_notification_aggregator = TransferFailureNotificationAggregator()
        # durable admission 仓储先于内存入队保存任务，进程退出后仍可恢复。
        self._transfer_admissions = self.transfer_admission_repository
        self._transfer_executions = self.transfer_execution_repository
        # 转移成功的文件清单
        self._success_target_files: Dict[Tuple, List[str]] = {}
        # 批次级刮削缓冲，避免同一批多文件入库重复触发目录刮削
        self._scrape_batches: Dict[str, Dict[str, Any]] = {}
        # 整理进度进度
        self._progress = ProgressHelper(ProgressKey.FileTransfer)
        # 队列相关状态
        self._threads = []
        self._retiring_threads: List[threading.Thread] = []
        self._queue_active = False
        # 每一代 worker 使用独立停止信号，避免热更新启动新 worker 后旧线程重新取任务
        self._worker_stop_event = threading.Event()
        # 生命周期操作串行化；状态锁只保护短临界区，不覆盖同步文件 I/O 等待
        self._worker_lifecycle_lock = threading.RLock()
        self._worker_state_lock = threading.RLock()
        self._closing = False
        # 恢复调度与租约续期均由整理链持有，关闭时与 worker 一并收敛。
        self._worker_owner_id = uuid.uuid4().hex
        self._owned_leases: Dict[str, Tuple[str, float]] = {}
        self._queued_lease_tokens: set[Tuple[str, str]] = set()
        self._replay_thread: Optional[threading.Thread] = None
        self._replay_stop_event = threading.Event()
        self._recovery_wakeup_event = threading.Event()
        self._lease_heartbeat_thread: Optional[threading.Thread] = None
        self._lease_heartbeat_stop_event = threading.Event()
        self._lease_release_thread: Optional[threading.Thread] = None
        self._active_tasks = 0
        self._processed_num = 0
        self._fail_num = 0
        self._total_num = 0
        # 启动整理任务
        self.__init()

    def __init(self) -> bool:
        """启动一代文件整理线程，并返回是否成功取得 worker 所有权。"""
        with self._worker_lifecycle_lock:
            with self._worker_state_lock:
                if self._closing:
                    logger.warning("文件整理链已进入关闭状态，拒绝重新启动 worker")
                    return False
                self._retiring_threads = [
                    thread for thread in self._retiring_threads if thread.is_alive()
                ]
                alive_threads = [thread for thread in self._threads if thread.is_alive()]
                if alive_threads:
                    logger.error(
                        "上一代文件整理线程尚未收敛，拒绝并行启动新 worker：%s",
                        ", ".join(thread.name for thread in alive_threads),
                    )
                    self._threads = alive_threads
                    return False
                stop_event = threading.Event()
                threads = [
                    threading.Thread(
                        target=self.__start_transfer,
                        args=(stop_event,),
                        name=f"transfer-{index}",
                        daemon=True,
                    )
                    for index in range(self.runtime_config.transfer_threads)
                ]
                self._worker_stop_event = stop_event
                self._threads = threads
                self._queue_active = True
                for index, thread in enumerate(threads):
                    logger.info(f"启动文件整理线程 {index + 1} ...")
                    thread.start()
                return True

    @staticmethod
    def __join_threads(
            threads: List[threading.Thread], deadline: float
    ) -> List[threading.Thread]:
        """在统一截止时间内等待线程，返回仍未收敛且继续由调用方持有的线程。"""
        current_thread = threading.current_thread()
        for thread in threads:
            if thread is current_thread or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return [thread for thread in threads if thread.is_alive()]

    def __acquire_worker_lifecycle_lock(self, deadline: float) -> bool:
        """在统一截止时间内取得 worker 生命周期锁，并兼容同线程 RLock 重入。"""
        return self._worker_lifecycle_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        )

    def __request_worker_stop(self) -> List[threading.Thread]:
        """发布当前 worker 代的停止信号，并用哨兵唤醒空闲线程。"""
        with self._worker_state_lock:
            self._queue_active = False
            self._worker_stop_event.set()
            current_threads = list(self._threads)
            threads = [*self._retiring_threads, *current_threads]
            for _ in current_threads:
                self._queue.put(self._QUEUE_STOP_SENTINEL)
            return threads

    def __stop(self, timeout_seconds: float = _WORKER_RESTART_TIMEOUT_SECONDS) -> bool:
        """在锁等待与线程 join 的共享预算内停止当前 worker 代。"""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        if not self.__acquire_worker_lifecycle_lock(deadline):
            logger.error(
                "未在 %.1f 秒内取得文件整理 worker 生命周期锁",
                max(0.0, timeout_seconds),
            )
            return False
        try:
            threads = self.__request_worker_stop()
            alive_threads = self.__join_threads(threads, deadline)
            with self._worker_state_lock:
                self._threads = []
                self._retiring_threads = alive_threads
            if alive_threads:
                logger.error(
                    "文件整理线程未在 %.1f 秒内收敛，仍由 TransferChain 持有：%s",
                    max(0.0, timeout_seconds),
                    ", ".join(thread.name for thread in alive_threads),
                )
                return False
            logger.info("文件整理线程已停止")
            return True
        finally:
            self._worker_lifecycle_lock.release()

    def close_workers(self, timeout_seconds: float = _WORKER_CLOSE_TIMEOUT_SECONDS) -> bool:
        """
        关闭整理 worker 与 pending 回放，并拒绝后续队列写入。

        这是宿主生命周期使用的同步边界。停止信号只能阻止线程领取下一项工作，不能
        取消已经进入同步文件或数据库 I/O 的调用；超过预算时保留活线程句柄并返回
        False，调用方据此避免过早释放仍被使用的数据库等下游资源。
        :param timeout_seconds: 生命周期锁、worker 与回放线程共享的最大等待秒数
        :return: 全部后台线程均已收敛时返回 True，否则返回 False
        """
        self.__ensure_lease_runtime_state()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        if not self.__acquire_worker_lifecycle_lock(deadline):
            logger.error(
                "未在 %.1f 秒内取得整理后台生命周期锁，关闭未开始",
                max(0.0, timeout_seconds),
            )
            return False
        try:
            with self._worker_state_lock:
                self._closing = True
                worker_threads = self.__request_worker_stop()
                replay_thread = self._replay_thread
                heartbeat_thread = self._lease_heartbeat_thread
                self._replay_stop_event.set()
                self._recovery_wakeup_event.set()

            alive_workers = self.__join_threads(worker_threads, deadline)
            alive_replays = self.__join_threads(
                [replay_thread] if replay_thread else [], deadline
            )
            with self._worker_state_lock:
                self._threads = []
                self._retiring_threads = alive_workers
                if (
                        replay_thread
                        and replay_thread not in alive_replays
                        and self._replay_thread is replay_thread
                ):
                    self._replay_thread = None

            alive_threads = [*alive_workers, *alive_replays]
            if alive_threads:
                logger.error(
                    "整理后台线程未在 %.1f 秒内收敛，仍由 TransferChain 持有：%s",
                    max(0.0, timeout_seconds),
                    ", ".join(thread.name for thread in alive_threads),
                )
                return False
            with self._worker_state_lock:
                release_thread = self.__start_lease_release_owner_locked(
                    error="整理宿主关闭，释放未结算任务租约"
                )
            alive_releases = self.__join_threads(
                [release_thread] if release_thread else [], deadline
            )
            if alive_releases:
                logger.error(
                    "整理租约释放线程未在 %.1f 秒内收敛，heartbeat 保持运行：%s",
                    max(0.0, timeout_seconds),
                    ", ".join(thread.name for thread in alive_releases),
                )
                return False
            with self._worker_state_lock:
                if self._lease_release_thread is release_thread:
                    self._lease_release_thread = None
            self._lease_heartbeat_stop_event.set()
            alive_heartbeats = self.__join_threads(
                [heartbeat_thread] if heartbeat_thread else [], deadline
            )
            if heartbeat_thread and not alive_heartbeats:
                with self._worker_state_lock:
                    if self._lease_heartbeat_thread is heartbeat_thread:
                        self._lease_heartbeat_thread = None
            if alive_heartbeats:
                logger.error(
                    "整理租约续期线程未在 %.1f 秒内收敛：%s",
                    max(0.0, timeout_seconds),
                    ", ".join(thread.name for thread in alive_heartbeats),
                )
                return False
            logger.info("文件整理 worker 与待处理回放线程已关闭")
            return True
        finally:
            self._worker_lifecycle_lock.release()

    async def close(self, timeout_seconds: float = _WORKER_CLOSE_TIMEOUT_SECONDS) -> bool:
        """
        收口整理线程和失败通知，并返回依赖是否可以安全释放。

        同步文件 I/O 在线程内无法被 asyncio 取消，因此先在线程池中执行有界
        ``close_workers``。只有 worker 与 replay 全部退出后才关闭通知和重试；若
        超时则保留通知依赖，供仍在运行的整理回调继续使用。
        :param timeout_seconds: worker 与 replay 共享的最大等待秒数
        :return: 所有整理后台 owner 均已收敛时返回 True
        """
        workers_closed = await asyncio.to_thread(
            self.close_workers,
            timeout_seconds,
        )
        if not workers_closed:
            return False
        self.failure_notification_aggregator.close()
        return True

    def on_config_changed(self) -> None:
        """配置变更时重启文件整理线程。"""
        with self._worker_lifecycle_lock:
            if self._closing:
                logger.info("文件整理链正在关闭，忽略 worker 配置热更新")
                return
            if not self.__stop(
                    timeout_seconds=self._WORKER_RESTART_TIMEOUT_SECONDS
            ):
                logger.warning(
                    "旧文件整理 worker 仍在收尾；其停止信号保持有效，新一代接管后续队列"
                )
            self.__init()

    def __default_callback(
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
        target_dir_path = self.__get_transfer_target_dir_path(transferinfo)
        job_id = self.jobview.get_job_id(task)
        overwrite_declined = False
        if not transferinfo.success:
            overwrite_declined = self._is_overwrite_declined(
                task, transferinfo, transferhis
            )
        settlement = self.__build_transfer_result_settlement(
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
                    history_id=self.__transfer_history_id(history),
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
        self.__mark_torrent_completed_if_done(task.download_hash, task.downloader)

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
            reason=transferinfo.message or "未知",
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
                loop=global_vars.loop,
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

    def __get_transfer_target_dir_path(
            self, transferinfo: Optional[TransferInfo]
    ) -> Optional[str]:
        """
        获取整理目标目录路径，兼容 OpenList 等成功后目录项短时间不可见的存储。
        """
        if not transferinfo:
            return None
        if transferinfo.target_diritem and transferinfo.target_diritem.path:
            return transferinfo.target_diritem.path
        if transferinfo.target_item and transferinfo.target_item.path:
            return Path(transferinfo.target_item.path).parent.as_posix()
        if transferinfo.file_list_new:
            return Path(transferinfo.file_list_new[0]).parent.as_posix()
        return None

    def put_to_queue(self, task: TransferTask) -> bool:
        """
        添加到待整理队列
        :param task: 任务信息
        :return: True表示任务已添加，False表示链已关闭或任务无效/重复
        :raises Exception: 持久准入、批次登记或内存入队失败
        """
        with self._worker_state_lock:
            if self._closing:
                logger.warning("文件整理链已关闭，拒绝新的队列任务")
                return False
            if isinstance(task.lease_token, str) and task.lease_token:
                return self.__enqueue_claimed_task(task)
            return self._transfer_queue_service().put(task, self.__default_callback)

    def _transfer_queue_service(self) -> TransferQueueService:
        """构建保持旧队列对象和私有兼容接缝的应用服务。"""
        return TransferQueueService(
            register_task=self.__put_to_jobview,
            admit_task=self.__admit_transfer,
            enqueue=self._queue.put,
            before_enqueue=self._register_scrape_batch_task,
            enqueue_failed=self.__record_enqueue_failure,
            remove_task=self.jobview.remove_task,
            list_tasks=self.jobview.list_jobs,
            expire_tasks=self.__expire_stale_transfer_tasks,
        )

    def replay_pending(self) -> None:
        """
        启动唯一恢复调度 owner，并唤醒一次即时恢复扫描。

        启动回放、同进程入队补偿和租约过期接管都经由这个入口。调度线程只负责
        claim 和重新入队；实际业务仍由普通整理 worker 执行。
        """
        self.__ensure_recovery_scheduler(immediate=True)

    def __ensure_recovery_scheduler(self, *, immediate: bool) -> None:
        """确保唯一恢复调度 owner 存在，并只为显式请求执行即时扫描。"""
        self.__ensure_lease_runtime_state()
        with self._worker_state_lock:
            if self._closing:
                logger.info("文件整理链正在关闭，跳过待处理文件回放")
                return
            self.__start_lease_heartbeat_owner_locked()
            if self._replay_thread and self._replay_thread.is_alive():
                if immediate:
                    self._recovery_wakeup_event.set()
                return
            thread = threading.Thread(
                target=self.__run_replay_pending,
                args=(self._replay_stop_event, immediate),
                name="MoviePilot-TransferReplay",
                daemon=True,
            )
            self._replay_thread = thread
            if immediate:
                self._recovery_wakeup_event.set()
            # 在状态锁内启动，避免 close_workers 看到尚未 start 的线程后错误 join。
            thread.start()

    def __run_replay_pending(
            self,
            stop_event: threading.Event,
            initial_immediate: bool = True,
    ) -> None:
        """按初次扫描意图持续恢复，失败兜底新建 owner 时先等待固定轮询。"""
        try:
            if not initial_immediate:
                self._recovery_wakeup_event.wait(
                    timeout=self._RECOVERY_POLL_INTERVAL_SECONDS
                )
            while not stop_event.is_set():
                self._recovery_wakeup_event.clear()
                self.__replay_pending(stop_event)
                self._recovery_wakeup_event.wait(
                    timeout=self._RECOVERY_POLL_INTERVAL_SECONDS
                )
        finally:
            with self._worker_state_lock:
                if self._replay_thread is threading.current_thread():
                    self._replay_thread = None

    def __ensure_lease_runtime_state(self) -> None:
        """为绕过构造器的兼容调用补齐进程 owner 与租约线程状态。"""
        if not hasattr(self, "_worker_owner_id"):
            self._worker_owner_id = uuid.uuid4().hex
        if not hasattr(self, "_owned_leases"):
            self._owned_leases = {}
        if not hasattr(self, "_queued_lease_tokens"):
            self._queued_lease_tokens = set()
        if not hasattr(self, "_recovery_wakeup_event"):
            self._recovery_wakeup_event = threading.Event()
        if not hasattr(self, "_lease_heartbeat_thread"):
            self._lease_heartbeat_thread = None
        if not hasattr(self, "_lease_heartbeat_stop_event"):
            self._lease_heartbeat_stop_event = threading.Event()
        if not hasattr(self, "_lease_release_thread"):
            self._lease_release_thread = None
        if not hasattr(self, "_replay_thread"):
            self._replay_thread = None
        if not hasattr(self, "_replay_stop_event"):
            self._replay_stop_event = threading.Event()
        if not hasattr(self, "_worker_state_lock"):
            self._worker_state_lock = threading.RLock()
        if not hasattr(self, "_closing"):
            self._closing = False

    def __start_lease_heartbeat_owner_locked(self) -> None:
        """在状态锁内确保当前进程只有一个租约续期线程。"""
        heartbeat_thread = self._lease_heartbeat_thread
        if heartbeat_thread and heartbeat_thread.is_alive():
            return
        heartbeat_thread = threading.Thread(
            target=self.__run_lease_heartbeat,
            args=(self._lease_heartbeat_stop_event,),
            name="MoviePilot-TransferLeaseHeartbeat",
            daemon=True,
        )
        self._lease_heartbeat_thread = heartbeat_thread
        heartbeat_thread.start()

    def __ensure_lease_heartbeat_owner(self) -> None:
        """按需启动进程级租约续期 owner，供启动前到达的普通任务使用。"""
        self.__ensure_lease_runtime_state()
        with self._worker_state_lock:
            if not self._closing:
                self.__start_lease_heartbeat_owner_locked()

    def __run_lease_heartbeat(self, stop_event: threading.Event) -> None:
        """按固定周期续期本进程已 claim 且尚未结算的任务。"""
        try:
            while not stop_event.wait(self._LEASE_HEARTBEAT_INTERVAL_SECONDS):
                self.__heartbeat_owned_leases()
        finally:
            with self._worker_state_lock:
                if self._lease_heartbeat_thread is threading.current_thread():
                    self._lease_heartbeat_thread = None

    def __heartbeat_owned_leases(self) -> None:
        """经 Application Port 续期所有排队中或执行中的任务租约。"""
        self.__ensure_lease_runtime_state()
        with self._worker_state_lock:
            owned_leases = list(self._owned_leases.items())
        for task_id, (lease_token, deadline) in owned_leases:
            try:
                admission = self._transfer_admissions.heartbeat(
                    task_id=task_id,
                    lease_token=lease_token,
                    lease_seconds=self._WORKER_LEASE_SECONDS,
                )
            except Exception as err:
                if time.monotonic() >= deadline:
                    self.__forget_owned_lease(task_id, lease_token)
                    logger.error(
                        "整理任务租约续期持续失败并已超过本地期限：%s - %s",
                        task_id,
                        err,
                    )
                else:
                    logger.error(f"整理任务租约续期失败：{task_id} - {err}")
                continue
            if (
                    admission is None
                    or admission.lease_owner != self._worker_owner_id
                    or admission.lease_token != lease_token
            ):
                self.__forget_owned_lease(task_id, lease_token)
                logger.error(f"整理任务租约已失效或被接管：{task_id}")
                continue
            with self._worker_state_lock:
                current = self._owned_leases.get(task_id)
                if current and current[0] == lease_token:
                    self._owned_leases[task_id] = (
                        lease_token,
                        time.monotonic() + self._WORKER_LEASE_SECONDS,
                    )

    def __bind_claimed_admission(
            self,
            task: TransferTask,
            admission: TransferAdmission,
    ) -> None:
        """校验仓储 claim 投影并把 token 私有绑定到执行任务。"""
        if (
                admission.task_id != task.admission_task_id
        ):
            raise TransferLeaseLostError(
                f"整理任务 claim 投影无效：{admission.task_id}"
            )
        self.__register_claimed_admission(admission)
        assert admission.lease_owner is not None
        assert admission.lease_token is not None
        task.bind_execution_lease(
            owner_id=admission.lease_owner,
            lease_token=admission.lease_token,
        )

    def __register_claimed_admission(
            self,
            admission: TransferAdmission,
    ) -> None:
        """把仓储 claim 加入续期集合，覆盖恢复构造阶段可能发生的同步 I/O。"""
        if (
                admission.lease_owner != self._worker_owner_id
                or not admission.lease_token
        ):
            raise TransferLeaseLostError(
                f"整理任务 claim 投影无效：{admission.task_id}"
            )
        with self._worker_state_lock:
            self._owned_leases[admission.task_id] = (
                admission.lease_token,
                time.monotonic() + self._WORKER_LEASE_SECONDS,
            )
        self.__ensure_lease_heartbeat_owner()

    def __forget_owned_lease(self, task_id: str, lease_token: str) -> None:
        """仅在 token 仍匹配时移除本进程租约镜像，避免删掉新接管记录。"""
        with self._worker_state_lock:
            current = self._owned_leases.get(task_id)
            if current and current[0] == lease_token:
                self._owned_leases.pop(task_id, None)
            self._queued_lease_tokens.discard((task_id, lease_token))

    def __is_claimed_task_enqueued(self, task_id: str, lease_token: str) -> bool:
        """返回指定 claim 是否已经成功进入普通 worker 队列。"""
        with self._worker_state_lock:
            return (task_id, lease_token) in self._queued_lease_tokens

    def __owns_lease(self, task_id: str, lease_token: Optional[str]) -> bool:
        """返回本地续期镜像是否仍持有指定 token。"""
        if not lease_token:
            return False
        with self._worker_state_lock:
            current = self._owned_leases.get(task_id)
            return bool(current and current[0] == lease_token)

    def __assert_owned_lease(self, task: TransferTask) -> None:
        """拒绝无 token、已过本地期限或已被 heartbeat 判失效的任务推进。"""
        if task.preview:
            return
        task_id = task.admission_task_id
        lease_token = task.lease_token
        if (
                not task_id
                or not lease_token
                or task.lease_owner != self._worker_owner_id
        ):
            raise TransferLeaseLostError("整理任务缺少当前进程的有效执行租约")
        with self._worker_state_lock:
            current = self._owned_leases.get(task_id)
            if (
                    current is None
                    or current[0] != lease_token
                    or current[1] <= time.monotonic()
            ):
                if current and current[0] == lease_token:
                    self._owned_leases.pop(task_id, None)
                raise TransferLeaseLostError(f"整理任务租约已经失效：{task_id}")

    def __claim_task_for_execution(self, task: TransferTask) -> None:
        """让普通队列任务在业务执行前取得唯一租约，恢复任务复用既有 token。"""
        if task.preview:
            return
        self.__ensure_lease_runtime_state()
        if task.lease_token:
            self.__assert_owned_lease(task)
            return
        if not task.admission_task_id:
            admitted = self.__admit_transfer(task)
            task.bind_admission_task_id(admitted.task_id)
        task_id = task.admission_task_id
        if task_id is None:
            raise TransferLeaseLostError("整理任务准入后仍缺少 durable 身份")
        claimed = self._transfer_admissions.claim_task(
            task_id=task_id,
            owner_id=self._worker_owner_id,
            lease_seconds=self._WORKER_LEASE_SECONDS,
        )
        if claimed is None:
            raise TransferLeaseLostError(
                f"整理任务已由其他 worker claim：{task_id}"
            )
        try:
            self.__bind_claimed_admission(task, claimed)
        except Exception as err:
            self.__release_admission_claim(claimed, error=str(err))
            raise

    def __release_task_claim(
            self,
            task: TransferTask,
            *,
            error: Optional[str] = None,
    ) -> bool:
        """按 task token 释放未到终态的 claim，并按固定轮询等待恢复。"""
        task_id = task.admission_task_id if task else None
        lease_token = task.lease_token if task else None
        if not task_id or not lease_token:
            return False
        try:
            return self._transfer_admissions.release_claim(
                task_id=task_id,
                lease_token=lease_token,
                error=error,
            )
        except Exception as err:
            logger.error(f"释放整理任务租约失败：{task_id} - {err}")
            return False
        finally:
            self.__forget_owned_lease(task_id, lease_token)
            self.__ensure_recovery_scheduler(immediate=False)

    def __release_admission_claim(
            self,
            admission: TransferAdmission,
            *,
            error: Optional[str] = None,
    ) -> None:
        """释放尚未绑定或成功入队的恢复 claim。"""
        if not admission.lease_token:
            return
        try:
            self._transfer_admissions.release_claim(
                task_id=admission.task_id,
                lease_token=admission.lease_token,
                error=error,
            )
        except Exception as err:
            logger.error(f"释放恢复任务租约失败：{admission.task_id} - {err}")
        finally:
            self.__forget_owned_lease(admission.task_id, admission.lease_token)

    def __release_all_owned_leases(self, *, error: str) -> None:
        """在执行 owner 全部收敛后释放剩余 claim，避免关停后等待租约自然过期。"""
        with self._worker_state_lock:
            owned_leases = list(self._owned_leases.items())
        for task_id, (lease_token, _deadline) in owned_leases:
            try:
                self._transfer_admissions.release_claim(
                    task_id=task_id,
                    lease_token=lease_token,
                    error=error,
                )
            except Exception as err:
                logger.error(f"关闭时释放整理任务租约失败：{task_id} - {err}")
            finally:
                self.__forget_owned_lease(task_id, lease_token)

    def __start_lease_release_owner_locked(
            self, *, error: str
    ) -> Optional[threading.Thread]:
        """启动并持有唯一租约释放线程，使同步数据库阻塞不突破关闭预算。"""
        release_thread = self._lease_release_thread
        if release_thread is not None:
            return release_thread
        if not self._owned_leases:
            return None
        release_thread = threading.Thread(
            target=self.__release_all_owned_leases,
            kwargs={"error": error},
            name="MoviePilot-TransferLeaseRelease",
            daemon=True,
        )
        self._lease_release_thread = release_thread
        release_thread.start()
        return release_thread

    def __enqueue_claimed_task(self, task: TransferTask) -> bool:
        """把已 claim 的恢复任务送入普通队列，禁止再次准入或二次 claim。"""
        self.__assert_owned_lease(task)
        if not self.__put_to_jobview(task):
            return False
        try:
            self._register_scrape_batch_task(task)
            assert task.admission_task_id is not None
            assert task.lease_token is not None
            with self._worker_state_lock:
                self._queued_lease_tokens.add(
                    (task.admission_task_id, task.lease_token)
                )
            self._queue.put(
                TransferQueue(task=task, callback=self.__default_callback)
            )
        except Exception as err:
            try:
                self.__record_enqueue_failure(task, err)
            finally:
                self.jobview.remove_task(task.fileitem)
            raise
        return True

    def __replay_pending(
            self, stop_event: Optional[threading.Event] = None
    ) -> None:
        """
        把落盘登记的待整理文件重新送回整理入口。

        accepted 记录按原始请求重新规划；planned 记录绑定已提交检查点，重新识别
        领域对象后只执行冻结路径，不再次触发重命名或目标计算。
        :param stop_event: 宿主关闭信号；只阻止处理下一条登记，不取消运行中的同步 I/O
        """
        stop_event = stop_event or threading.Event()
        if stop_event.is_set():
            return
        self.__ensure_lease_runtime_state()
        try:
            pendings = self._transfer_admissions.claim_recoverable(
                owner_id=self._worker_owner_id,
                limit=self._RECOVERY_CLAIM_LIMIT,
                lease_seconds=self._WORKER_LEASE_SECONDS,
            )
        except Exception as err:
            logger.error(f"读取待整理文件登记失败：{err}")
            return
        if not pendings:
            return
        try:
            for admission in pendings:
                self.__register_claimed_admission(admission)
        except Exception as err:
            logger.error(f"登记恢复任务租约失败：{err}")
            for claimed in pendings:
                self.__release_admission_claim(claimed, error=str(err))
            return
        logger.info(f"发现 {len(pendings)} 个上次未整理完的文件，正在重新送入整理链 ...")
        replayed = 0
        for index, admission in enumerate(pendings):
            if stop_event.is_set():
                for unprocessed in pendings[index:]:
                    self.__release_admission_claim(
                        unprocessed,
                        error="整理宿主关闭，恢复任务尚未入队",
                    )
                break
            storage = admission.storage
            src_path = admission.src_path
            try:
                execution_snapshot = self.__execution_replay_snapshot(admission)
                use_frozen_source = bool(
                    admission.checkpoint is not None
                    or execution_snapshot.state is not TransferExecutionState.NOT_STARTED
                    or execution_snapshot.steps
                )
                if use_frozen_source:
                    planning_input = admission.planning_input
                    if planning_input is None:
                        raise TransferPlanningStateError(
                            "执行态回放缺少整理规划输入"
                        )
                    fileitem = FileItem.model_validate(
                        planning_input.source_fileitem
                    )
                    should_discard = False
                else:
                    fileitem, should_discard = self.__build_replay_fileitem(
                        storage,
                        src_path,
                        admission.planning_input,
                    )
                # stat 等同步 I/O 返回后重新检查，关闭期间不得注销尚未完成的登记。
                if stop_event.is_set():
                    self.__release_admission_claim(
                        admission,
                        error="整理宿主关闭，恢复任务尚未入队",
                    )
                    for unprocessed in pendings[index + 1:]:
                        self.__release_admission_claim(
                            unprocessed,
                            error="整理宿主关闭，恢复任务尚未入队",
                        )
                    break
                if not fileitem:
                    if should_discard:
                        # 源文件确认已消失，注销登记避免每次启动重复回放
                        lease_token = admission.lease_token
                        if lease_token is None:
                            raise TransferLeaseLostError(
                                f"恢复任务缺少 lease token：{admission.task_id}"
                            )
                        discarded = self._transfer_admissions.abandon_unstarted(
                            task_id=admission.task_id,
                            lease_token=lease_token,
                        )
                        if not discarded:
                            logger.warning(
                                f"恢复任务终态注销被 CAS 拒绝：{admission.task_id}"
                            )
                            self.__release_admission_claim(
                                admission,
                                error="源已消失但任务状态已变化，保留登记供恢复",
                            )
                        else:
                            self.__forget_owned_lease(
                                admission.task_id,
                                lease_token,
                            )
                    else:
                        self.__release_admission_claim(
                            admission,
                            error="恢复源文件暂时不可读取",
                        )
                    continue
                if admission.checkpoint:
                    if self.__queue_planned_replay(
                            fileitem,
                            admission,
                            execution_checkpoint=(
                                execution_snapshot.checkpoint
                                if execution_snapshot.state
                                is TransferExecutionState.SETTLING
                                else None
                            ),
                    ):
                        replayed += 1
                    else:
                        self.__release_admission_claim(
                            admission,
                            error="恢复任务未进入内存队列",
                        )
                    continue
                planning_input = admission.planning_input
                if planning_input and (
                        planning_input.meta
                        or planning_input.mediainfo
                        or planning_input.episodes_info
                ):
                    if self.__queue_accepted_replay(fileitem, admission):
                        replayed += 1
                    else:
                        self.__release_admission_claim(
                            admission,
                            error="恢复任务未进入内存队列",
                        )
                    continue
                replay_kwargs = self.__build_replay_kwargs(planning_input)
                self._execute_transfer(
                    fileitem=fileitem,
                    recovery_admission=admission,
                    **replay_kwargs,
                )
                assert admission.lease_token is not None
                if self.__is_claimed_task_enqueued(
                        admission.task_id,
                        admission.lease_token,
                ):
                    replayed += 1
                else:
                    self.__release_admission_claim(
                        admission,
                        error="旧恢复入口未产生可执行队列任务",
                    )
            except Exception as err:
                logger.error(f"回放待整理文件失败：{storage}:{src_path} - {err}")
                if self.__owns_lease(admission.task_id, admission.lease_token):
                    self.__release_admission_claim(admission, error=str(err))
        if stop_event.is_set():
            logger.info(
                "待整理文件回放收到关闭请求，已送入 %s 个文件，其余登记保持待处理",
                replayed,
            )
        else:
            logger.info(f"✓ 待整理文件回放完成，{replayed} 个文件已重新送入整理链")

    def __execution_replay_snapshot(
            self,
            admission: TransferAdmission,
    ) -> TransferExecutionSnapshot:
        """读取已 claim 的执行快照，确保任何步骤证据都先于源文件探测。"""
        repository: Optional[TransferExecutionRepository] = getattr(
            self,
            "_transfer_executions",
            None,
        )
        if repository is None:
            raise RuntimeError("整理恢复缺少 execution repository")
        snapshot = repository.get_snapshot(task_id=admission.task_id)
        if snapshot is None:
            raise TransferExecutionConflictError("整理恢复任务缺少执行状态投影")
        if (
                snapshot.state is TransferExecutionState.SETTLING
                and snapshot.checkpoint is None
        ):
            raise TransferExecutionConflictError(
                "settling 整理任务缺少可重放终态检查点"
            )
        return snapshot

    @staticmethod
    def __build_replay_kwargs(
            planning_input: Optional[TransferPlanningInput],
    ) -> dict[str, Any]:
        """从持久请求快照恢复公开整理入口可接受的稳定参数。"""
        if planning_input is None:
            return {}
        options = planning_input.options
        target_directory_payload = planning_input.target_directory
        cleanup_payload = options.get("cleanup_dest_fileitem")
        try:
            media_source = (
                MediaSource(planning_input.media_source)
                if planning_input.media_source
                else None
            )
        except ValueError:
            media_source = None
        try:
            media_type = (
                MediaType(planning_input.media_type)
                if planning_input.media_type
                else None
            )
        except ValueError:
            media_type = None
        return {
            "mtype": media_type,
            "media_source": media_source,
            "media_id": planning_input.media_id,
            "target_directory": (
                TransferDirectoryConf.model_validate(target_directory_payload)
                if target_directory_payload
                else None
            ),
            "target_storage": planning_input.target_storage,
            "target_path": (
                Path(planning_input.target_path)
                if planning_input.target_path
                else None
            ),
            "transfer_type": planning_input.requested_transfer_type,
            "scrape": options.get("scrape"),
            "library_type_folder": options.get("library_type_folder"),
            "library_category_folder": options.get("library_category_folder"),
            "downloader": options.get("downloader"),
            "download_hash": options.get("download_hash"),
            "background": True,
            "manual": bool(options.get("manual")),
            "preview": False,
            "cleanup_dest_fileitem": (
                FileItem.model_validate(cleanup_payload)
                if isinstance(cleanup_payload, dict)
                else None
            ),
        }

    def __queue_accepted_replay(
            self,
            fileitem: FileItem,
            admission: TransferAdmission,
    ) -> bool:
        """从 accepted 输入离线恢复显式领域上下文并送入单任务队列。"""
        planning_input = admission.planning_input
        if planning_input is None:
            raise TransferPlanningStateError("accepted 回放缺少整理规划输入")
        options = planning_input.options
        replay_kwargs = self.__build_replay_kwargs(planning_input)
        task = TransferTask(
            fileitem=fileitem,
            meta=(
                self.__restore_meta_snapshot(
                    planning_input.meta,
                    options.get("_meta_kind"),
                )
                if planning_input.meta
                else None
            ),
            mediainfo=(
                self.__restore_mediainfo_snapshot(
                    planning_input.mediainfo,
                    options.get("_mediainfo_kind"),
                )
                if planning_input.mediainfo
                else None
            ),
            mtype=replay_kwargs["mtype"],
            media_source=replay_kwargs["media_source"],
            media_id=replay_kwargs["media_id"],
            target_directory=replay_kwargs["target_directory"],
            target_storage=replay_kwargs["target_storage"],
            target_path=replay_kwargs["target_path"],
            transfer_type=replay_kwargs["transfer_type"],
            scrape=replay_kwargs["scrape"],
            library_type_folder=replay_kwargs["library_type_folder"],
            library_category_folder=replay_kwargs["library_category_folder"],
            episodes_info=[
                TmdbEpisode.model_validate(item)
                for item in planning_input.episodes_info
            ],
            username=options.get("username"),
            downloader=replay_kwargs["downloader"],
            download_hash=replay_kwargs["download_hash"],
            manual=replay_kwargs["manual"],
            background=True,
            preview=False,
        )
        task.bind_admission_task_id(admission.task_id)
        self.__bind_claimed_admission(task, admission)
        task.bind_planning_input(planning_input)
        if planning_input.mediainfo:
            task.mark_planning_context_restored()
        return self.put_to_queue(task)

    def __queue_planned_replay(
            self,
            fileitem: FileItem,
            admission: TransferAdmission,
            *,
            execution_checkpoint: Optional[TransferExecutionCheckpoint] = None,
    ) -> bool:
        """把已提交检查点直接恢复为单个任务，避免重新扫描、识别和规划。"""
        checkpoint = admission.checkpoint
        if checkpoint is None:
            raise TransferPlanningStateError("planned 回放缺少整理计划检查点")
        options = checkpoint.planning_input.options
        invocation = checkpoint.provider_invocation
        task = TransferTask(
            fileitem=fileitem,
            target_storage=(
                invocation.target_storage if invocation else checkpoint.target_storage
            ),
            target_path=(
                Path(invocation.target_path)
                if invocation and invocation.target_path
                else Path(checkpoint.root_target_path)
                if checkpoint.root_target_path
                else None
            ),
            transfer_type=(
                invocation.transfer_type
                if invocation
                else checkpoint.resolved_transfer_type
            ),
            scrape=invocation.scrape if invocation else checkpoint.need_scrape,
            library_type_folder=(
                invocation.library_type_folder
                if invocation
                else bool(options.get("library_type_folder"))
            ),
            library_category_folder=(
                invocation.library_category_folder
                if invocation
                else bool(options.get("library_category_folder"))
            ),
            username=options.get("username"),
            downloader=options.get("downloader"),
            download_hash=options.get("download_hash"),
            manual=bool(options.get("manual")),
            background=True,
            preview=False,
        )
        task.bind_admission_task_id(admission.task_id)
        self.__bind_claimed_admission(task, admission)
        task.bind_planning_input(checkpoint.planning_input)
        task.bind_plan_checkpoint(checkpoint)
        if execution_checkpoint is not None:
            task.bind_execution_checkpoint(execution_checkpoint)
        self.__restore_planned_task(task)
        return self.put_to_queue(task)

    @staticmethod
    def __build_replay_fileitem(
            storage: str,
            src_path: str,
            planning_input: Optional[TransferPlanningInput] = None,
    ) -> Tuple[Optional[FileItem], bool]:
        """
        为回放构造文件项。

        必须用 stat 的异常类型区分「文件真的没了」和「挂载暂时读不到」，不能用
        Path.exists()：它在任何 OSError 下都返回 False，会把挂载抖动
        （Transport endpoint is not connected）误判成文件消失，进而注销登记
        ——那等于在故障期间主动丢件，正是本表要防的事。
        :param storage: 存储
        :param src_path: 源文件路径，以 / 结尾表示蓝光原盘目录
        :param planning_input: 准入时保存的完整源文件身份快照
        :return: (文件项, 是否应注销登记)。文件项为 None 表示本次不回放；
                 只有确认源文件已经消失时才注销登记
        """
        # 蓝光原盘目录在登记时保留了尾部斜杠，这里据此还原类型
        is_dir = src_path.endswith("/")
        path = Path(src_path)
        size, modify_time = None, None
        if storage == "local":
            try:
                file_stat = path.stat()
                size, modify_time = file_stat.st_size, file_stat.st_mtime
            except FileNotFoundError:
                logger.info(f"待整理文件已不存在，注销登记：{src_path}")
                return None, True
            except OSError as err:
                # 挂载未就绪或无响应属于暂时性故障，保留登记等下次启动再回放
                logger.warn(f"读取待整理文件失败，保留登记等待下次回放：{src_path} - {err}")
                return None, False
        snapshot = (
            planning_input.source_fileitem
            if planning_input and planning_input.source_fileitem
            else {}
        )
        fileitem = FileItem.model_validate({
            **snapshot,
            "storage": storage,
            "path": src_path if is_dir else path.as_posix(),
            "type": "dir" if is_dir else snapshot.get("type") or "file",
            "name": snapshot.get("name") or path.name,
            "basename": snapshot.get("basename") or path.stem,
            "extension": (
                snapshot.get("extension")
                if snapshot.get("extension") is not None
                else path.suffix[1:] if not is_dir else None
            ),
            "size": size if size is not None else snapshot.get("size"),
            "modify_time": (
                modify_time
                if modify_time is not None
                else snapshot.get("modify_time")
            ),
        })
        return fileitem, False

    def __admit_transfer(self, task: TransferTask) -> TransferAdmission:
        """在内存入队前持久化源文件并返回稳定任务身份。"""
        fileitem = task.fileitem if task else None
        if not fileitem or not fileitem.storage or not fileitem.path:
            raise ValueError("整理任务缺少源文件身份")
        planning_input = task.planning_input or self.__build_planning_input(task)
        task.bind_planning_input(planning_input)
        return self._transfer_admissions.admit(
            storage=fileitem.storage,
            src_path=fileitem.path,
            planning_input=planning_input,
        )

    @staticmethod
    def __json_snapshot(value: Any) -> Any:
        """把受控领域对象投影为可持久化 JSON 值。"""
        if value is None:
            return None
        return to_jsonable_python(value, serialize_unknown=True)

    def __build_planning_input(
            self,
            task: TransferTask,
            *,
            cleanup_dest_fileitem: Optional[FileItem] = None,
    ) -> TransferPlanningInput:
        """冻结准入时已知的请求参数，供 accepted 任务跨重启重新规划。"""
        target_directory = task.target_directory
        options = {
            "scrape": task.scrape,
            "library_type_folder": task.library_type_folder,
            "library_category_folder": task.library_category_folder,
            "manual": task.manual,
            "background": task.background,
            "username": task.username,
            "downloader": task.downloader,
            "download_hash": task.download_hash,
            "cleanup_dest_fileitem": self.__json_snapshot(cleanup_dest_fileitem),
            "_meta_kind": type(task.meta).__name__ if task.meta else None,
            "_mediainfo_kind": (
                type(task.mediainfo).__name__ if task.mediainfo else None
            ),
        }
        return TransferPlanningInput(
            source_fileitem=self.__json_snapshot(task.fileitem),
            meta=self.__json_snapshot(task.meta),
            mediainfo=self.__json_snapshot(task.mediainfo),
            target_directory=self.__json_snapshot(target_directory),
            target_storage=task.target_storage,
            target_path=task.target_path.as_posix() if task.target_path else None,
            requested_transfer_type=task.transfer_type,
            media_source=task.media_source.value if task.media_source else None,
            media_id=task.media_id,
            media_type=task.mtype.value if task.mtype else None,
            need_scrape=bool(task.scrape),
            need_rename=bool(target_directory.renaming) if target_directory else True,
            need_notify=bool(target_directory.notify) if target_directory else False,
            overwrite_mode=(target_directory.overwrite_mode if target_directory else None),
            episodes_info=tuple(
                self.__json_snapshot(episode) for episode in (task.episodes_info or [])
            ),
            preview=bool(task.preview),
            options=options,
        )

    def __build_provider_invocation_snapshot(
            self,
            task: TransferTask,
    ) -> TransferProviderInvocationSnapshot:
        """冻结目录解析后的旧 ABI 参数，并保留 None 与 False 的原值差异。"""
        return TransferProviderInvocationSnapshot(
            fileitem=self.__json_snapshot(task.fileitem),
            meta=self.__json_snapshot(task.meta),
            meta_kind=type(task.meta).__name__ if task.meta else None,
            mediainfo=self.__json_snapshot(task.mediainfo),
            mediainfo_kind=(
                type(task.mediainfo).__name__ if task.mediainfo else None
            ),
            target_directory=self.__json_snapshot(task.target_directory),
            target_storage=task.target_storage,
            target_path=task.target_path.as_posix() if task.target_path else None,
            transfer_type=task.transfer_type,
            scrape=task.scrape,
            library_type_folder=task.library_type_folder,
            library_category_folder=task.library_category_folder,
            episodes_info=tuple(
                self.__json_snapshot(episode) for episode in (task.episodes_info or [])
            ),
            preview=bool(task.preview),
        )

    @staticmethod
    def __restore_meta_snapshot(
            payload: Optional[dict[str, Any]],
            kind: object,
    ) -> MetaBase:
        """从持久快照恢复元数据对象，不重新运行文件名解析。"""
        if not payload:
            raise TransferPlanningStateError("整理持久快照缺少已解析元数据")
        if kind == "MetaMusic" or payload.get("type") == MediaType.MUSIC.value:
            return MetaMusic.from_dict(payload)
        meta_class = (
            MetaAnime
            if kind == "MetaAnime"
            else MetaVideo
        )
        meta = meta_class("")
        for key, value in payload.items():
            descriptor = getattr(meta_class, key, None)
            if isinstance(descriptor, property) and descriptor.fset is None:
                continue
            if key == "type" and isinstance(value, str):
                value = MediaType(value)
            elif key == "media_source" and isinstance(value, str):
                value = MediaSource(value)
            setattr(meta, key, value)
        return meta

    @staticmethod
    def __restore_mediainfo_snapshot(
            payload: Optional[dict[str, Any]],
            kind: object,
    ) -> Union[MediaInfo, MusicInfo]:
        """从持久快照恢复媒体信息，不访问任何在线数据源。"""
        if not payload:
            raise TransferPlanningStateError("整理持久快照缺少已识别媒体信息")
        if kind == "MusicInfo" or payload.get("type") == MediaType.MUSIC.value:
            return MusicInfo.from_dict(payload)
        mediainfo = MediaInfo()
        mediainfo.from_dict(payload)
        return mediainfo

    def __restore_planned_task(self, task: TransferTask) -> None:
        """用冻结检查点覆盖易受配置和在线识别变化影响的任务字段。"""
        checkpoint = task.plan_checkpoint
        if checkpoint is None:
            raise TransferPlanningStateError("planned 任务缺少整理计划检查点")
        if checkpoint.provider_invocation is not None:
            invocation = checkpoint.provider_invocation
            task.fileitem = FileItem.model_validate(invocation.fileitem)
            task.meta = self.__restore_meta_snapshot(
                invocation.meta,
                invocation.meta_kind,
            )
            task.mediainfo = self.__restore_mediainfo_snapshot(
                invocation.mediainfo,
                invocation.mediainfo_kind,
            )
            task.target_directory = (
                TransferDirectoryConf.model_validate(invocation.target_directory)
                if invocation.target_directory
                else None
            )
            task.target_storage = invocation.target_storage
            task.target_path = (
                Path(invocation.target_path) if invocation.target_path else None
            )
            task.transfer_type = invocation.transfer_type
            task.scrape = invocation.scrape
            task.library_type_folder = invocation.library_type_folder
            task.library_category_folder = invocation.library_category_folder
            task.episodes_info = [
                TmdbEpisode.model_validate(item)
                for item in invocation.episodes_info
            ]
            task.mark_planning_context_restored()
            return
        if checkpoint.resolved_meta:
            task.meta = self.__restore_meta_snapshot(
                checkpoint.resolved_meta,
                checkpoint.resolved_meta_kind,
            )
        elif task.meta is None and checkpoint.rejection_error is None:
            raise TransferPlanningStateError("整理计划检查点缺少已解析元数据")
        if checkpoint.resolved_mediainfo:
            task.mediainfo = self.__restore_mediainfo_snapshot(
                checkpoint.resolved_mediainfo,
                checkpoint.resolved_mediainfo_kind,
            )
        elif task.mediainfo is None and checkpoint.rejection_error is None:
            raise TransferPlanningStateError("整理计划检查点缺少已识别媒体信息")
        task.episodes_info = [
            TmdbEpisode.model_validate(item)
            for item in checkpoint.resolved_episodes_info
        ]
        task.target_storage = checkpoint.target_storage
        task.target_path = Path(checkpoint.root_target_path)
        task.transfer_type = checkpoint.resolved_transfer_type
        task.scrape = checkpoint.need_scrape
        task.mark_planning_context_restored()

    def __select_storage_oper(self, storage: Optional[str]) -> Any:
        """按冻结存储标识请求插件存储适配器，未接管时返回空值。"""
        event_data = StorageOperSelectionEventData(storage=storage)
        event = self.eventmanager.send_event(
            ChainEventType.StorageOperSelection,
            event_data,
        )
        if event and event.event_data:
            resolved = event.event_data
            return resolved.storage_oper
        return None

    def __record_uncheckpointed_failure(
            self,
            task: TransferTask,
            error: object,
    ) -> None:
        """记录 checkpoint 前失败，保留 accepted 任务供后续重新规划。"""
        if (
                task.preview
                or task.plan_checkpoint
                or not task.admission_task_id
                or not task.lease_token
        ):
            return
        try:
            self._transfer_admissions.record_planning_failure(
                task_id=task.admission_task_id,
                lease_token=task.lease_token,
                error=str(error),
            )
        except Exception as record_error:
            logger.error(f"记录整理规划前失败原因出错：{record_error}")

    def __checkpoint_planning_rejection(
            self,
            task: TransferTask,
            error: str,
    ) -> TransferInfo:
        """把确定性规划拒绝冻结为零文件副作用计划并走统一终态结算。"""
        if task.preview:
            return TransferInfo(
                success=False,
                message=error,
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=task.transfer_type,
                need_notify=False,
            )
        self.__claim_task_for_execution(task)
        self.__assert_owned_lease(task)
        if getattr(self, "durable_event_writer", None) is None:
            raise RuntimeError(
                "非预览整理缺少 durable 原子写入端口，拒绝提交规划终态"
            )
        planning_input = task.planning_input or self.__build_planning_input(task)
        task.bind_planning_input(planning_input)
        source_path = task.fileitem.path
        resolved_transfer_type = (
            task.transfer_type
            or planning_input.requested_transfer_type
            or "copy"
        )
        checkpoint = TransferPlanCheckpoint(
            planning_input=planning_input,
            target_storage=task.fileitem.storage,
            root_target_path=source_path,
            final_target_path=source_path,
            resolved_transfer_type=resolved_transfer_type,
            items=(),
            resolved_meta=self.__json_snapshot(task.meta),
            resolved_meta_kind=(type(task.meta).__name__ if task.meta else None),
            resolved_mediainfo=self.__json_snapshot(task.mediainfo),
            resolved_mediainfo_kind=(
                type(task.mediainfo).__name__ if task.mediainfo else None
            ),
            resolved_episodes_info=tuple(
                self.__json_snapshot(item) for item in (task.episodes_info or [])
            ),
            need_notify=planning_input.need_notify,
            overwrite_mode=planning_input.overwrite_mode,
            rejection_error=error,
        )
        persisted = self.__persist_transfer_checkpoint(
            task,
            planning_input=planning_input,
            checkpoint=checkpoint,
        )
        task.bind_plan_checkpoint(persisted)
        return self._plan_checkpoint_and_execute(task)

    def __execute_planning_rejection(
            self,
            task: TransferTask,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferInfo:
        """以可重放内部步骤确认冻结拒绝，并建立统一 execution checkpoint。"""
        error = checkpoint.rejection_error
        if not error:
            raise TransferPlanningStateError("整理拒绝计划缺少失败原因")
        step_runner = self.__build_durable_step_runner(task, checkpoint)
        if step_runner is None:
            raise RuntimeError("非预览整理拒绝缺少 durable 步骤 runner")
        evidence = TransferStepResult(payload={"error": error})
        step_runner.run(
            phase="planning",
            kind="reject",
            payload={"error": error},
            execute=lambda: evidence,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.APPLIED,
                evidence=evidence,
            ),
        )
        transferinfo = TransferInfo(
            success=False,
            message=error,
            fileitem=task.fileitem,
            fail_list=[task.fileitem.path],
            transfer_type=checkpoint.resolved_transfer_type,
            need_notify=checkpoint.need_notify,
        )
        task.bind_execution_checkpoint(step_runner.checkpoint(transferinfo))
        return transferinfo

    def __handle_planned_transfer(
            self,
            task: TransferTask,
            callback: Optional[Callable[[TransferTask, TransferInfo], Tuple[bool, str]]],
    ) -> Tuple[bool, str]:
        """直接执行已提交检查点，不再触发识别、分类、选目录或重命名。"""
        self.__restore_planned_task(task)
        self.jobview.running_task(task)
        settling_result = self.__restore_settling_transfer_result(task)
        if settling_result is not None:
            if callback:
                return callback(task, settling_result)
            return settling_result.success, settling_result.message or ""
        checkpoint = task.plan_checkpoint
        assert checkpoint is not None
        target_storage = (
            checkpoint.provider_invocation.target_storage
            if checkpoint.provider_invocation
            else checkpoint.target_storage
        )
        transferinfo = self._plan_checkpoint_and_execute(
            task,
            source_oper=self.__select_storage_oper(task.fileitem.storage),
            target_oper=self.__select_storage_oper(target_storage),
        )
        if not transferinfo:
            raise RuntimeError("文件整理模块未返回检查点执行结果")
        if callback:
            return callback(task, transferinfo)
        return transferinfo.success, transferinfo.message or ""

    def __restore_settling_transfer_result(
            self,
            task: TransferTask,
    ) -> Optional[TransferInfo]:
        """从 execution checkpoint 恢复终态结果，禁止再次执行外部步骤。"""
        repository: Optional[TransferExecutionRepository] = getattr(
            self,
            "_transfer_executions",
            None,
        )
        task_id = task.admission_task_id
        if repository is None or not task_id:
            if task.execution_checkpoint is not None:
                raise TransferExecutionConflictError(
                    "绑定执行检查点的整理任务缺少持久状态仓储"
                )
            return None
        snapshot = repository.get_snapshot(task_id=task_id)
        if snapshot is None:
            if task.execution_checkpoint is not None:
                raise TransferExecutionConflictError(
                    "绑定执行检查点的整理任务缺少持久执行状态"
                )
            return None
        if snapshot.state is not TransferExecutionState.SETTLING:
            return None
        checkpoint = snapshot.checkpoint
        bound_checkpoint = task.execution_checkpoint
        if (
                bound_checkpoint is not None
                and checkpoint is not None
                and bound_checkpoint.fingerprint != checkpoint.fingerprint
        ):
            raise TransferExecutionConflictError(
                "内存整理任务与持久结算检查点不一致"
            )
        if checkpoint is None:
            raise TransferExecutionConflictError(
                "settling 整理任务缺少可重放终态检查点"
            )
        self.__assert_owned_lease(task)
        payload = checkpoint.payload
        raw_outcome = payload.get("outcome")
        try:
            outcome = TransferExecutionOutcome(
                raw_outcome if isinstance(raw_outcome, str) else ""
            )
        except (TypeError, ValueError) as error:
            raise TransferExecutionConflictError(
                "整理执行检查点缺少确定终态"
            ) from error
        transfer_payload = payload.get("transferinfo")
        if isinstance(transfer_payload, dict):
            transferinfo = cast(
                TransferInfo,
                TransferInfo.model_validate(transfer_payload),
            )
        elif outcome is TransferExecutionOutcome.FAILED:
            plan_checkpoint = task.plan_checkpoint
            transferinfo = TransferInfo(
                success=False,
                message=str(payload.get("error") or "整理失败"),
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=(
                    plan_checkpoint.resolved_transfer_type
                    if plan_checkpoint is not None
                    else None
                ),
                need_notify=(
                    plan_checkpoint.need_notify
                    if plan_checkpoint is not None
                    else False
                ),
            )
        else:
            raise TransferExecutionConflictError(
                "成功整理执行检查点缺少可重放 TransferInfo"
            )
        expected_success = outcome is TransferExecutionOutcome.SUCCEEDED
        expected_overwrite_skip = (
            outcome is TransferExecutionOutcome.OVERWRITE_SKIPPED
        )
        if (
                bool(transferinfo.success) != expected_success
                or bool(transferinfo.overwrite_skipped) != expected_overwrite_skip
        ):
            raise TransferExecutionConflictError(
                "整理执行检查点终态与 TransferInfo 不一致"
            )
        task.bind_execution_checkpoint(checkpoint)
        return transferinfo

    def __build_durable_step_runner(
            self,
            task: TransferTask,
            checkpoint: TransferPlanCheckpoint,
    ) -> Optional[_DurableTransferStepRunner]:
        """为非预览持久任务构造绑定计划指纹和当前租约的步骤 runner。"""
        if task.preview:
            return None
        repository = getattr(self, "_transfer_executions", None)
        if repository is None:
            raise RuntimeError("非预览整理缺少 execution repository")
        if not task.admission_task_id or not task.lease_token:
            raise TransferLeaseLostError("整理执行缺少持久任务身份或租约")
        return _DurableTransferStepRunner(
            task_id=task.admission_task_id,
            lease_token=task.lease_token,
            checkpoint_fingerprint=self.__transfer_plan_fingerprint(checkpoint),
            repository=repository,
        )

    def __execute_host_transfer_plan(
            self,
            task: TransferTask,
            checkpoint: TransferPlanCheckpoint,
            *,
            source_oper: Any,
            target_oper: Any,
            step_runner: Optional[_DurableTransferStepRunner],
    ) -> Optional[TransferInfo]:
        """通过模块边界执行宿主计划，并只向持久任务注入内部步骤 runner。"""
        if step_runner is None:
            return self.execute_transfer_plan(
                checkpoint,
                meta=task.meta,
                mediainfo=task.mediainfo,
                source_oper=source_oper,
                target_oper=target_oper,
                cleanup_media_file=self.__cleanup_transfer_destination,
            )
        return cast(
            Optional[TransferInfo],
            self.run_module(
                "execute_transfer_plan",
                checkpoint=checkpoint,
                meta=task.meta,
                mediainfo=task.mediainfo,
                source_oper=source_oper,
                target_oper=target_oper,
                cleanup_media_file=self.__cleanup_transfer_destination,
                observe_cleanup_media_file=self.__observe_cleanup_destination,
                step_runner=step_runner,
            ),
        )

    def _plan_checkpoint_and_execute(
            self,
            task: TransferTask,
            *,
            source_oper: Any = None,
            target_oper: Any = None,
    ) -> TransferInfo:
        """先提交冻结 provider 调用，空结果时再提交并执行宿主计划。"""
        planning_input = task.planning_input or self.__build_planning_input(task)
        task.bind_planning_input(planning_input)
        if not task.preview:
            self.__claim_task_for_execution(task)
            self.__assert_owned_lease(task)
            if getattr(self, "durable_event_writer", None) is None:
                raise RuntimeError(
                    "非预览整理缺少 durable 原子写入端口，拒绝开始外部执行"
                )

        checkpoint = task.plan_checkpoint
        if checkpoint is None:
            try:
                frozen_providers = tuple(
                    TransferProviderReference(
                        plugin_id=provider.plugin_id,
                        plugin_name=provider.plugin_name,
                        method=provider.method,
                    )
                    for provider in self._module_dispatcher.freeze_plugin_providers(
                        "transfer"
                    )
                )
                if frozen_providers:
                    invocation = self.__build_provider_invocation_snapshot(task)
                    checkpoint = TransferPlanCheckpoint(
                        planning_input=planning_input,
                        target_storage="",
                        root_target_path="",
                        final_target_path="",
                        resolved_transfer_type="",
                        items=(),
                        resolved_meta=invocation.meta,
                        resolved_meta_kind=invocation.meta_kind,
                        resolved_mediainfo=invocation.mediainfo,
                        resolved_mediainfo_kind=invocation.mediainfo_kind,
                        resolved_episodes_info=invocation.episodes_info,
                        legacy_transfer_providers=frozen_providers,
                        provider_invocation=invocation,
                        preview=invocation.preview,
                    )
                else:
                    checkpoint = self.__plan_host_transfer(
                        task,
                        planning_input=planning_input,
                        source_oper=source_oper,
                    )
                checkpoint = self.__persist_transfer_checkpoint(
                    task,
                    planning_input=planning_input,
                    checkpoint=checkpoint,
                )
                task.bind_plan_checkpoint(checkpoint)
            except Exception as error:
                self.__record_checkpoint_failure(task, error)
                raise

        self.__restore_planned_task(task)
        self.__assert_owned_lease(task)

        if checkpoint.rejection_error:
            return self.__execute_planning_rejection(task, checkpoint)

        step_runner = self.__build_durable_step_runner(task, checkpoint)
        try:
            legacy_result = self.__execute_legacy_transfer_providers(
                task,
                checkpoint=checkpoint,
                source_oper=source_oper,
                target_oper=target_oper,
                step_runner=step_runner,
            )
        except _TransferRetryExhausted as error:
            assert error.snapshot.checkpoint is not None
            task.bind_execution_checkpoint(error.snapshot.checkpoint)
            return TransferInfo(
                success=False,
                message=str(error),
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=checkpoint.resolved_transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if legacy_result is not None:
            if step_runner is not None:
                task.bind_execution_checkpoint(step_runner.checkpoint(legacy_result))
            return legacy_result

        if checkpoint.is_provider_pending:
            try:
                checkpoint = replace(
                    self.__plan_host_transfer(
                        task,
                        planning_input=planning_input,
                        source_oper=source_oper,
                    ),
                    pre_execution_cleanup_completed=True,
                )
                checkpoint = self.__persist_transfer_checkpoint(
                    task,
                    planning_input=planning_input,
                    checkpoint=checkpoint,
                )
                task.bind_plan_checkpoint(checkpoint)
                self.__restore_planned_task(task)
                step_runner = self.__build_durable_step_runner(task, checkpoint)
            except Exception as error:
                self.__record_checkpoint_failure(task, error)
                raise

        self.__assert_owned_lease(task)
        try:
            result = self.__execute_host_transfer_plan(
                task,
                checkpoint,
                source_oper=source_oper,
                target_oper=target_oper,
                step_runner=step_runner,
            )
        except _TransferRetryExhausted as error:
            assert error.snapshot.checkpoint is not None
            task.bind_execution_checkpoint(error.snapshot.checkpoint)
            return TransferInfo(
                success=False,
                message=str(error),
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=checkpoint.resolved_transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if result is None:
            raise RuntimeError("文件整理模块未返回检查点执行结果")
        if step_runner is not None:
            task.bind_execution_checkpoint(step_runner.checkpoint(result))
        return result

    def __plan_host_transfer(
            self,
            task: TransferTask,
            *,
            planning_input: TransferPlanningInput,
            source_oper: Any,
    ) -> TransferPlanCheckpoint:
        """在 provider 未接管时生成纯宿主计划，不执行任何文件写入。"""
        checkpoint = self.plan_transfer(
            fileitem=task.fileitem,
            meta=cast(MetaBase, task.meta),
            mediainfo=cast(Union[MediaInfo, MusicInfo], task.mediainfo),
            target_directory=task.target_directory,
            target_storage=task.target_storage,
            target_path=task.target_path,
            transfer_type=task.transfer_type,
            episodes_info=task.episodes_info,
            scrape=task.scrape,
            library_type_folder=task.library_type_folder,
            library_category_folder=task.library_category_folder,
            source_oper=source_oper,
            preview=bool(task.preview),
            planning_input=planning_input,
        )
        if checkpoint is None:
            raise RuntimeError("文件整理模块未返回规划检查点")
        return checkpoint

    def __persist_transfer_checkpoint(
            self,
            task: TransferTask,
            *,
            planning_input: TransferPlanningInput,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferPlanCheckpoint:
        """提交当前阶段检查点，并只消费仓储回读的持久投影。"""
        if task.preview:
            return checkpoint
        if not task.admission_task_id:
            raise RuntimeError("整理计划提交前缺少持久任务身份")
        self.__assert_owned_lease(task)
        assert task.lease_token is not None
        persisted = self._transfer_admissions.checkpoint_plan(
            task_id=task.admission_task_id,
            lease_token=task.lease_token,
            input_fingerprint=planning_input.fingerprint,
            checkpoint=checkpoint,
        )
        if persisted.checkpoint is None:
            raise TransferPlanningStateError("持久投影缺少整理检查点")
        return persisted.checkpoint

    def __record_checkpoint_failure(
            self,
            task: TransferTask,
            error: Exception,
    ) -> None:
        """记录当前 checkpoint 阶段失败并保留原状态供重启恢复。"""
        if task.preview or not task.admission_task_id or not task.lease_token:
            return
        try:
            self._transfer_admissions.record_planning_failure(
                task_id=task.admission_task_id,
                lease_token=task.lease_token,
                error=str(error),
            )
            setattr(error, "_transfer_planning_failure_recorded", True)
        except Exception as record_error:
            logger.error(f"记录整理规划失败原因出错：{record_error}")

    def __execute_legacy_transfer_providers(
            self,
            task: TransferTask,
            *,
            checkpoint: TransferPlanCheckpoint,
            source_oper: Any,
            target_oper: Any,
            step_runner: Optional[_DurableTransferStepRunner] = None,
    ) -> Optional[TransferInfo]:
        """在 checkpoint 提交后按冻结身份执行旧插件 transfer provider。"""
        providers = checkpoint.legacy_transfer_providers
        if not providers:
            return None
        invocation = checkpoint.provider_invocation
        if invocation is None:
            raise TransferPlanningStateError(
                "冻结旧 transfer provider 缺少调用快照"
            )
        target_directory = (
            TransferDirectoryConf.model_validate(invocation.target_directory)
            if invocation.target_directory
            else None
        )
        provider_meta = (
            self.__restore_meta_snapshot(invocation.meta, invocation.meta_kind)
            if invocation.meta
            else task.meta
        )
        provider_mediainfo = (
            self.__restore_mediainfo_snapshot(
                invocation.mediainfo,
                invocation.mediainfo_kind,
            )
            if invocation.mediainfo
            else task.mediainfo
        )
        provider_episodes = [
            TmdbEpisode.model_validate(item)
            for item in invocation.episodes_info
        ]
        def invoke_provider_sequence() -> Optional[TransferInfo]:
            """按冻结顺序调用旧 provider，并校验其兼容返回类型。"""
            result = self._module_dispatcher.execute_frozen_plugin_providers(
                "transfer",
                providers,
                before_invoke=lambda: self.__prepare_legacy_provider_execution(
                    checkpoint=checkpoint
                ),
                fileitem=FileItem.model_validate(invocation.fileitem),
                meta=provider_meta,
                mediainfo=provider_mediainfo,
                target_directory=target_directory,
                target_storage=invocation.target_storage,
                target_path=(Path(invocation.target_path) if invocation.target_path else None),
                transfer_type=invocation.transfer_type,
                scrape=invocation.scrape,
                library_type_folder=invocation.library_type_folder,
                library_category_folder=invocation.library_category_folder,
                episodes_info=provider_episodes,
                source_oper=source_oper,
                target_oper=target_oper,
                preview=invocation.preview,
            )
            if result is not None and not isinstance(result, TransferInfo):
                raise TypeError("旧插件 transfer provider 返回了不支持的结果类型")
            return result

        def execute_provider_sequence() -> TransferStepResult:
            """执行冻结旧 provider 序列，并冻结是否接管及兼容结果。"""
            result = invoke_provider_sequence()
            return TransferStepResult(payload={
                "handled": result is not None,
                "transferinfo": (
                    result.model_dump(mode="json") if result is not None else None
                ),
            })

        if step_runner is None:
            return invoke_provider_sequence()
        provider_result = step_runner.run(
            phase="provider",
            kind="legacy_transfer_provider_sequence",
            payload={
                "providers": [provider.to_payload() for provider in providers],
                "invocation": invocation.to_payload(),
            },
            execute=execute_provider_sequence,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={
                    "reason": "legacy provider has no stable operation receipt",
                }),
            ),
        )
        if not provider_result.payload.get("handled"):
            return None
        payload = provider_result.payload.get("transferinfo")
        if not isinstance(payload, dict):
            raise TransferPlanningStateError("旧 provider 成功证据缺少整理结果")
        validated_result: TransferInfo = TransferInfo.model_validate(payload)
        return validated_result

    def __prepare_legacy_provider_execution(
            self,
            *,
            checkpoint: TransferPlanCheckpoint,
    ) -> None:
        """按旧批次语义在 provider 副作用前执行冻结 cleanup intent。"""
        invocation = checkpoint.provider_invocation
        if invocation is None:
            raise TransferPlanningStateError("旧 provider 执行缺少调用快照")
        if invocation.preview:
            return

        cleanup_payload = checkpoint.planning_input.options.get(
            "cleanup_dest_fileitem"
        )
        if isinstance(cleanup_payload, dict):
            cleanup_fileitem = FileItem.model_validate(cleanup_payload)
            if not self.__cleanup_transfer_destination(cleanup_fileitem):
                raise RuntimeError(
                    f"{cleanup_fileitem.path} 删除失败，整理计划保留待重试"
                )

    def __cleanup_transfer_destination(self, fileitem: FileItem) -> bool:
        """经插件兼容的 StorageChain 幂等删除旧目标并治理父空目录。"""
        storage_chain = self._transfer_storage_chain()
        if not fileitem.path:
            return False
        current_item = storage_chain.get_file_item_strict(
            storage=fileitem.storage,
            path=Path(fileitem.path),
        )
        if current_item is None:
            return True
        return bool(storage_chain.delete_media_file(current_item))

    def __observe_cleanup_destination(self, fileitem: FileItem) -> bool:
        """只读确认统一清理目标已经不存在，查询错误由步骤 runner 隔离。"""
        if not fileitem.path:
            return False
        return self._transfer_storage_chain().get_file_item_strict(
            storage=fileitem.storage,
            path=Path(fileitem.path),
        ) is None

    def execute_legacy_transfer_command(
            self,
            fileitem: FileItem,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            target_directory: Optional[TransferDirectoryConf] = None,
            target_storage: Optional[str] = None,
            target_path: Optional[Path] = None,
            transfer_type: Optional[str] = None,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            source_oper: Any = None,
            target_oper: Any = None,
            preview: bool = False,
    ) -> TransferInfo:
        """把旧同步 ABI 适配为准入、检查点和终态结算共用的唯一命令。"""
        task = TransferTask(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_directory=target_directory,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            episodes_info=episodes_info,
            manual=True,
            background=False,
            preview=preview,
        )
        try:
            result = self._plan_checkpoint_and_execute(
                task,
                source_oper=source_oper,
                target_oper=target_oper,
            )
        except Exception as error:
            logger.error(f"旧整理兼容命令执行失败：{error}")
            self.__release_task_claim(task, error=str(error))
            return TransferInfo(
                success=False,
                message=str(error),
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
            )
        if preview:
            return result
        try:
            self.__settle_legacy_transfer_result(task, result)
        except Exception as error:
            message = f"旧整理兼容命令 durable 终态结算失败：{error}"
            logger.error(message)
            self.__release_task_claim(task, error=message)
            return TransferInfo(
                success=False,
                message=message,
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
            )
        return result

    def __record_enqueue_failure(
            self,
            task: TransferTask,
            error: Exception,
    ) -> None:
        """按是否已经 claim 选择 fencing 失败记录，并撤销批次占位。"""
        try:
            if task.lease_token:
                self.__release_task_claim(task, error=str(error))
            elif task.admission_task_id:
                self._transfer_admissions.record_enqueue_failure(
                    task_id=task.admission_task_id,
                    error=str(error),
                )
        except Exception as record_error:
            logger.error(
                "记录整理任务入队失败原因异常："
                f"{task.admission_task_id} - {record_error}"
            )
        finally:
            self._finish_scrape_batch_task(task)
            self.__ensure_recovery_scheduler(immediate=False)

    def __put_to_jobview(self, task: TransferTask) -> bool:
        """
        添加到作业视图
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        return self.jobview.add_task(task)

    def __mark_torrent_completed_if_done(
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
        if not self.__is_torrent_download_completed(download_hash, downloader):
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

    def __is_torrent_download_completed(
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

    def remove_from_queue(self, fileitem: FileItem):
        """
        从待整理队列移除
        """
        self._transfer_queue_service().remove(fileitem)

    def __start_job_execution(self, task: TransferTask):
        """在作业视图支持执行租约时标记主程序任务开始执行。"""
        marker = getattr(self.jobview, "start_execution", None)
        if marker:
            marker(task)

    def __finish_job_execution(
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
                    self.__forget_owned_lease(
                        task.admission_task_id,
                        task.lease_token,
                    )
                return True
            self.__release_task_claim(
                task,
                error="整理终态未完成 durable 原子结算",
            )
            return False
        self.__release_task_claim(task)
        return True

    def __expire_stale_transfer_tasks(self):
        """清理外部接管后失去状态心跳的运行中整理任务。"""
        timeout_minutes = max(int(self.runtime_config.transfer_task_timeout), 0)
        expire_tasks = getattr(self.jobview, "expire_stale_running_tasks", None)
        expired_tasks = (
            expire_tasks(timeout_seconds=timeout_minutes * 60)
            if expire_tasks
            else []
        )
        for fileitem, inactive_seconds in expired_tasks:
            logger.error(
                f"整理任务 {fileitem.path} 已连续 {inactive_seconds // 60} 分钟无状态心跳，"
                "已标记失败并从整理队列视图清理"
            )

    def __fail_transfer_task(self, task: TransferTask):
        """
        标记异常整理任务失败并清理作业视图
        """
        self.jobview.fail_unfinished_task(task)
        self.jobview.try_remove_job(task)
        self._finish_scrape_batch_task(task)

    def __settle_transfer_progress_if_idle(self) -> None:
        """在没有 active 或未结算真实任务时结束进度并重置本批计数。"""
        with task_lock:
            # unfinished_tasks 同时覆盖队列内任务和已被其他 worker 取走、尚未来得及
            # 登记 active 的任务。持有 Queue 自身互斥锁完成判断和计数重置，使并发
            # enqueue 只能发生在旧批次归零之后；仍在 deque 中的停止哨兵不算真实任务。
            with self._queue.all_tasks_done:
                queued_stop_sentinels = sum(
                    item is self._QUEUE_STOP_SENTINEL
                    for item in self._queue.queue
                )
                has_unsettled_tasks = (
                    self._queue.unfinished_tasks > queued_stop_sentinels
                )
                if (
                        self._active_tasks != 0
                        or self._processed_num <= 0
                        or has_unsettled_tasks
                ):
                    return
                processed_num = self._processed_num
                fail_num = self._fail_num
                self._total_num = 0
                self._processed_num = 0
                self._fail_num = 0
            __end_msg = (
                f"整理队列处理完成，共整理 {processed_num} 个文件，"
                f"失败 {fail_num} 个"
            )
            logger.info(__end_msg)
            self._progress.update(value=100, text=__end_msg)
            self._progress.end()

    def __start_transfer(self, stop_event: threading.Event) -> None:
        """
        处理当前 worker 代的队列，停止后不领取下一项任务。

        :param stop_event: 当前 worker 代专属停止信号，热更新后不会被重新清除
        """
        while not runtime_stop_state.is_system_stopped and not stop_event.is_set():
            try:
                item: TransferQueue = self._queue.get(
                    block=True, timeout=self._transfer_interval
                )
                if item is self._QUEUE_STOP_SENTINEL:
                    self._queue.task_done()
                    self.__settle_transfer_progress_if_idle()
                    if stop_event.is_set() or runtime_stop_state.is_system_stopped:
                        break
                    continue
                if stop_event.is_set() or runtime_stop_state.is_system_stopped:
                    # 关闭信号与 queue.get 竞态时，把尚未处理的任务放回队列；其
                    # TransferPending 登记保持不变，供同进程重启 worker 或下次启动回放。
                    self._queue.put(item)
                    self._queue.task_done()
                    break
                if not item:
                    continue

                task = item.task
                if not task:
                    self._queue.task_done()
                    self.__settle_transfer_progress_if_idle()
                    continue

                if task.admission_task_id and task.lease_token:
                    with self._worker_state_lock:
                        self._queued_lease_tokens.discard(
                            (task.admission_task_id, task.lease_token)
                        )
                try:
                    self.__claim_task_for_execution(task)
                except TransferLeaseLostError as err:
                    logger.info(f"跳过未取得执行租约的整理任务：{err}")
                    self.__release_task_claim(task, error=str(err))
                    self.jobview.try_remove_job(task)
                    self._finish_scrape_batch_task(task)
                    self._queue.task_done()
                    self.__settle_transfer_progress_if_idle()
                    continue
                except Exception as err:
                    logger.error(
                        f"整理任务 claim 失败，保留 durable admission：{err}"
                    )
                    self.jobview.try_remove_job(task)
                    self._finish_scrape_batch_task(task)
                    self._queue.task_done()
                    self.__settle_transfer_progress_if_idle()
                    self.__ensure_recovery_scheduler(immediate=False)
                    continue

                # 文件信息
                fileitem = task.fileitem

                with task_lock:
                    # 批次总数 = 本批已处理数 + 未终态数。作业视图会残留上一批
                    # 已完成的任务（作业要等关联任务全部终态才移除），用全量
                    # total() 会把历史任务计入本批（如显示 8 个实际只处理 2 个），
                    # 且进度分母虚高导致百分比走不满
                    current_total = self._processed_num + self.jobview.pending_total()
                    # 更新总数，取当前总数和当前已处理+运行中+队列中的最大值
                    self._total_num = max(self._total_num, current_total)

                    # 如果当前没有在运行的任务且处理数为0，说明是一个新序列的开始
                    if self._active_tasks == 0 and self._processed_num == 0:
                        logger.info("开始整理队列处理...")
                        # 启动进度
                        self._progress.start()
                        # 重置计数
                        self._processed_num = 0
                        self._fail_num = 0
                        __process_msg = (
                            f"开始整理队列处理，当前共 {self._total_num} 个文件 ..."
                        )
                        logger.info(__process_msg)
                        self._progress.update(value=0, text=__process_msg)
                    # 增加运行中的任务数
                    self._active_tasks += 1

                terminal = False
                terminal_settlement: Optional[bool] = None
                state = False
                err_msg = ""

                def callback_after_terminal_settlement(
                        callback_task: TransferTask,
                        transferinfo: TransferInfo,
                ) -> Tuple[bool, str]:
                    """由默认回调原子提交历史、事件与 durable 终态。"""
                    nonlocal terminal_settlement
                    if item.callback:
                        try:
                            callback_result: Tuple[bool, str] = item.callback(
                                callback_task,
                                transferinfo,
                            )
                            return callback_result
                        finally:
                            if not callback_task.preview:
                                terminal_settlement = callback_task.terminal_settled
                    return transferinfo.success, transferinfo.message or ""

                try:
                    self.__start_job_execution(task)
                    # 更新进度
                    __process_msg = f"正在整理 {fileitem.name} ..."
                    logger.info(__process_msg)
                    with task_lock:
                        self._progress.update(
                            value=(self._processed_num / self._total_num * 100)
                            if self._total_num
                            else 0,
                            text=__process_msg,
                        )
                    # 整理
                    state, err_msg = self.__handle_transfer(
                        task=task,
                        callback=callback_after_terminal_settlement,
                    )
                    terminal = task.plan_checkpoint is not None

                    with task_lock:
                        if not state:
                            # 任务失败
                            self._fail_num += 1
                        # 更新进度
                        self._processed_num += 1
                        __process_msg = f"{fileitem.name} 整理完成"
                        logger.info(__process_msg)
                        self._progress.update(
                            value=(self._processed_num / self._total_num * 100)
                            if self._total_num
                            else 100,
                            text=__process_msg,
                        )
                except Exception as e:
                    if terminal_settlement is not None:
                        terminal = True
                    logger.error(
                        f"{fileitem.name} 整理任务处理出现错误：{e} - {traceback.format_exc()}"
                    )
                    self.__fail_transfer_task(task)
                    with task_lock:
                        self._processed_num += 1
                        self._fail_num += 1
                finally:
                    durable_settled = False
                    try:
                        durable_settled = self.__finish_job_execution(
                            task,
                            terminal=terminal,
                            terminal_settlement=terminal_settlement,
                        )
                    except Exception as err:
                        logger.error(
                            f"整理任务终态结算异常：{task.admission_task_id} - {err}"
                        )
                    finally:
                        self._queue.task_done()
                        with task_lock:
                            # 减少运行中的任务数
                            self._active_tasks -= 1
                            if terminal and state and not durable_settled:
                                self._fail_num += 1
                        self.__settle_transfer_progress_if_idle()

            except queue.Empty:
                # 即使队列空了，如果还有任务在运行，也不应该结束进度
                # 这部分逻辑已经在 finally 的 active_tasks == 0 中处理了
                self.__expire_stale_transfer_tasks()
                continue
            except Exception as e:
                logger.error(f"整理队列处理出现错误：{e} - {traceback.format_exc()}")

    def __handle_transfer(
            self, task: TransferTask, callback: Optional[Callable] = None
    ) -> Optional[Tuple[bool, str]]:
        """执行整理并统一记录 checkpoint 前的返回失败或异常。"""
        try:
            result = self.__perform_transfer(task, callback)
        except Exception as error:
            if not getattr(error, "_transfer_planning_failure_recorded", False):
                self.__record_uncheckpointed_failure(task, error)
            raise
        if result and not result[0]:
            self.__record_uncheckpointed_failure(task, result[1])
        return result

    def __perform_transfer(
            self, task: TransferTask, callback: Optional[Callable] = None
    ) -> Optional[Tuple[bool, str]]:
        """
        处理整理任务
        """
        try:
            if task.plan_checkpoint is not None:
                return self.__handle_planned_transfer(task, callback)
            # 识别
            transferhis = self.transfer_history_repository
            # 显式标注联合：下面既会赋回音乐识别结果（MusicInfo），也会赋回影视识别
            # 结果（MediaInfo），不标注时会被推断成其中一种，另一种就成了假错误
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = task.mediainfo
            mediainfo_changed = False
            need_obtain_images = False
            if not mediainfo:
                download_history = task.download_history
                # 下载用户
                if download_history:
                    task.username = download_history.username
                    # 识别媒体信息
                    history_year_conflict = self._is_movie_year_conflict(
                        task.meta, download_history
                    )
                    if (
                            download_history.media_source
                            and download_history.media_id
                            and not history_year_conflict
                    ):
                        # 下载记录中已存在识别信息。这里不再重复标注类型：函数开头
                        # 已把 mediainfo 声明为 MediaInfo | MusicInfo | None，重复
                        # 声明会遮蔽它，把音乐识别结果判成类型错误
                        mediainfo = MediaChain().recognize_media(
                            mtype=task.mtype or MediaType(download_history.type),
                            media_source=download_history.media_source,
                            media_id=download_history.media_id,
                            music_type=self._download_history_music_type(download_history),
                            episode_group=download_history.episode_group,
                        )
                        need_obtain_images = True
                        if mediainfo:
                            # 更新自定义媒体类别
                            if download_history.media_category:
                                mediainfo.category = download_history.media_category
                    else:
                        if history_year_conflict:
                            logger.info(
                                f"{task.fileitem.name} 文件年份 {task.meta.year} 与下载记录年份 "
                                f"{download_history.year} 不一致，按文件名重新识别"
                            )
                        recognize_kwargs = {"obtain_images": True}
                        if task.media_source:
                            recognize_kwargs["media_source"] = task.media_source
                        if task.mtype:
                            recognize_kwargs["mtype"] = task.mtype
                        mediainfo = MediaChain().recognize_by_meta(
                            task.meta, **recognize_kwargs
                        )
                        if mediainfo and download_history.media_category:
                            mediainfo.category = download_history.media_category
                else:
                    # 识别媒体信息
                    recognize_kwargs = {"obtain_images": True}
                    if task.media_source:
                        recognize_kwargs["media_source"] = task.media_source
                    if task.mtype:
                        recognize_kwargs["mtype"] = task.mtype
                    mediainfo = MediaChain().recognize_by_meta(
                        task.meta, **recognize_kwargs
                    )

                # 音乐必须先经过音乐元数据模块识别；远端不可用时再保留本地标签结果，
                # 避免因离线兜底提前赋值而跳过音乐识别链。
                if not mediainfo and isinstance(task.meta, MetaMusic):
                    mediainfo = self._music_info_from_meta(task.meta)

                # 按名称识别时已在识别链路补图，这里只补齐显式ID识别的场景。
                if mediainfo and need_obtain_images:
                    self.obtain_images(mediainfo=mediainfo)

                if mediainfo and task.media_source:
                    mediainfo.scrape_source = task.media_source

                if not mediainfo:
                    if task.preview:
                        return False, "未识别到媒体信息"
                    transferinfo = self.__checkpoint_planning_rejection(
                        task,
                        "未识别到媒体信息",
                    )
                    if callback:
                        return cast(Tuple[bool, str], callback(task, transferinfo))
                    return transferinfo.success, transferinfo.message or ""

                mediainfo_changed = True

            # accepted/planned 恢复使用持久快照；在线补充会让首次执行与重放产生漂移。
            if not task.planning_context_restored:
                mediainfo = MediaChain().supplement_tmdb_info(mediainfo, task.meta)
            task.mediainfo = mediainfo

            # 只有 TMDB 主源沿用历史 TMDB 标题，避免辅助 ID 改写其它识别源标题。
            if (
                    not self.runtime_config.scrape_follow_tmdb
                    and mediainfo.media_source == MediaSource.TMDB
            ):
                transfer_history = transferhis.get_by_media_identity(
                    media_source=mediainfo.media_source.value,
                    media_id=mediainfo.media_id,
                    mtype=mediainfo.type.value,
                )
                if transfer_history and mediainfo.title != transfer_history.title:
                    mediainfo.title = transfer_history.title
                    mediainfo_changed = True

            if mediainfo_changed:
                # 更新任务信息
                task.mediainfo = mediainfo
                # 更新队列任务
                if not self.jobview.migrate_task(task):
                    logger.info(f"{task.fileitem.name} 已存在整理任务，跳过重复处理")
                    return False, f"{task.fileitem.name} 已在整理队列中"

            # 获取集数据
            if (
                    task.mediainfo.type == MediaType.TV
                    and task.mediainfo.tmdb_id
                    and not task.episodes_info
                    and not task.planning_context_restored
            ):
                # 判断注意season为0的情况
                season_num = task.mediainfo.season
                if season_num is None and task.meta.season_seq:
                    if task.meta.season_seq.isdigit():
                        season_num = int(task.meta.season_seq)
                # 默认值1
                if season_num is None:
                    season_num = 1
                task.episodes_info = TmdbChain().tmdb_episodes(
                    tmdbid=task.mediainfo.tmdb_id,
                    season=season_num,
                    episode_group=task.mediainfo.episode_group,
                )

            # 查询整理目标目录
            if not task.target_directory:
                if task.target_path:
                    # 指定目标路径，`手动整理`场景下使用，忽略源目录匹配，使用指定目录匹配
                    task.target_directory = DirectoryHelper().get_dir(
                        media=task.mediainfo,
                        dest_path=task.target_path,
                        target_storage=task.target_storage,
                    )
                else:
                    # 启用源目录匹配时，根据源目录匹配下载目录，否则按源目录同盘优先原则，如无源目录，则根据媒体信息获取目标目录
                    task.target_directory = DirectoryHelper().get_dir(
                        media=task.mediainfo,
                        storage=task.fileitem.storage,
                        src_path=Path(task.fileitem.path),
                        target_storage=task.target_storage,
                    )
            if not task.target_storage and task.target_directory:
                task.target_storage = task.target_directory.library_storage

            if self._requires_automatic_category(task) and not task.mediainfo.category:
                # MusicInfo 无 tmdb_id 字段，但模型 __getattr__ 已兜底返回 None
                if task.mediainfo.tmdb_id:
                    error_message = "TMDB 信息未匹配到媒体分类，无法按媒体类别整理"
                else:
                    error_message = "未识别到 TMDB 辅助信息，无法按媒体类别整理"
                logger.error(f"{task.fileitem.name} {error_message}")
                if task.preview:
                    return False, error_message
                transferinfo = self.__checkpoint_planning_rejection(
                    task,
                    error_message,
                )
                if callback:
                    return cast(Tuple[bool, str], callback(task, transferinfo))
                return transferinfo.success, transferinfo.message or ""

            # 正在处理
            self.jobview.running_task(task)

            # 广播事件，请示额外的源、目标存储支持。
            source_oper = self.__select_storage_oper(task.fileitem.storage)
            target_oper = self.__select_storage_oper(task.target_storage)

            # 纯规划先提交 durable checkpoint，任何文件副作用只能发生在提交之后。
            transferinfo = self._plan_checkpoint_and_execute(
                task,
                source_oper=source_oper,
                target_oper=target_oper,
            )
            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"

            # 回调，位置传参：任务、整理结果
            if callback:
                return callback(task, transferinfo)

            return transferinfo.success, transferinfo.message

        finally:
            # 移除已完成的任务
            self.jobview.try_remove_job(task)
            self._finish_scrape_batch_task(task)

    def get_queue_tasks(self) -> List[TransferJob]:
        """
        获取整理任务列表
        """
        return self._transfer_queue_service().list()

    def process(self, progress_callback: Optional[Callable[..., None]] = None) -> bool:
        """
        获取下载器中的种子列表，并执行整理

        :param progress_callback: 定时服务进度更新回调
        """
        # 全局锁，避免定时服务重复
        with downloader_lock:
            # 获取下载器监控目录
            download_dirs = DirectoryHelper().get_download_dirs()

            # 如果没有下载器监控的目录则不处理
            if not any(
                    dir_info.monitor_type == "downloader" and dir_info.storage == "local"
                    for dir_info in download_dirs
            ):
                if progress_callback:
                    progress_callback(value=100, text="未配置下载器监控目录，跳过整理")
                return True

            logger.info("开始整理下载器中已经完成下载的文件 ...")
            if progress_callback:
                progress_callback(value=0, text="正在查询已完成下载任务 ...")

            # 从下载器获取种子列表
            if torrents_list := self.list_torrents(status=TorrentStatus.TRANSFER):
                seen = set()
                existing_hashes = self.jobview.get_all_torrent_hashes()
                torrents = [
                    torrent
                    for torrent in torrents_list
                    if (h := torrent.hash) not in existing_hashes
                       # 排除多下载器返回的重复种子
                       and (h not in seen and (seen.add(h) or True))
                ]
            else:
                torrents = []

            if not torrents:
                logger.info("没有已完成下载但未整理的任务")
                if progress_callback:
                    progress_callback(value=100, text="没有已完成下载但未整理的任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"获取到 {len(torrents)} 个已完成下载任务",
                    data={"total": len(torrents), "finished": 0},
                )

            try:
                total_num = len(torrents)
                for index, torrent in enumerate(torrents, start=1):
                    if runtime_stop_state.is_system_stopped:
                        break
                    if progress_callback:
                        torrent_name = (
                                getattr(torrent, "title", None)
                                or getattr(torrent, "name", None)
                                or torrent.hash
                        )
                        progress_callback(
                            value=(index - 1) / total_num * 100,
                            text=f"正在整理下载任务（{index}/{total_num}）{torrent_name} ...",
                            data={
                                "total": total_num,
                                "finished": index - 1,
                                "current": torrent.hash,
                            },
                        )

                    # 文件路径
                    file_path = torrent.path
                    if not file_path.exists():
                        logger.warn(f"文件不存在：{file_path}")
                        continue

                    # 检查是否为下载器监控目录中的文件
                    is_downloader_monitor = False
                    for dir_info in download_dirs:
                        if dir_info.monitor_type != "downloader":
                            continue
                        if not dir_info.download_path:
                            continue
                        if file_path.is_relative_to(Path(dir_info.download_path)):
                            is_downloader_monitor = True
                            break
                    if not is_downloader_monitor:
                        logger.debug(
                            f"文件 {file_path} 不在下载器监控目录中，不通过下载器进行整理"
                        )
                        continue

                    # 查询下载记录识别情况
                    downloadhis: Optional[DownloadHistorySnapshot] = (
                        self.download_history_repository.get_by_hash(torrent.hash)
                        if torrent.hash
                        else None
                    )
                    # 下载记录中的媒体类型作为整理类型来源，无下载记录时留空由文件后缀兜底
                    mtype: Optional[MediaType] = None
                    if downloadhis:
                        # 类型
                        try:
                            mtype = MediaType(downloadhis.type)
                        except ValueError:
                            mtype = MediaType.TV
                        # 识别媒体信息
                        mediainfo = MediaChain().recognize_media(
                            mtype=mtype,
                            media_source=downloadhis.media_source,
                            media_id=downloadhis.media_id,
                            music_type=self._download_history_music_type(downloadhis),
                            episode_group=downloadhis.episode_group,
                        )
                        if mediainfo:
                            # 补充图片
                            self.obtain_images(mediainfo)
                            # 更新自定义媒体类别
                            if downloadhis.media_category:
                                mediainfo.category = downloadhis.media_category

                    else:
                        # 非MoviePilot下载的任务，按文件识别
                        mediainfo = None

                    # 执行异步整理，匹配源目录
                    self.do_transfer(
                        fileitem=self._build_transfer_fileitem(torrent),
                        mediainfo=mediainfo,
                        mtype=mtype,
                        downloader=torrent.downloader,
                        download_hash=torrent.hash,
                    )
                    if progress_callback:
                        progress_callback(
                            value=index / total_num * 100,
                            text=f"下载任务（{index}/{total_num}）整理处理完成",
                            data={"total": total_num, "finished": index},
                        )

            finally:
                torrents.clear()
                del torrents

            return True

    @staticmethod
    def _build_transfer_fileitem(torrent: TorrentInfo) -> FileItem:
        """把下载器任务路径转换为整理链使用的本地文件项。"""
        file_path = torrent.path
        return FileItem(
            storage="local",
            path=file_path.as_posix() + ("/" if file_path.is_dir() else ""),
            type="dir" if not file_path.is_file() else "file",
            name=file_path.name,
            size=file_path.stat().st_size,
            extension=file_path.suffix.lstrip("."),
        )

    def __get_trans_fileitems(
            self,
            fileitem: FileItem,
            predicate: Optional[Callable[[FileItem, bool], bool]],
            verify_file_exists: bool = True,
    ) -> List[Tuple[FileItem, bool]]:
        """
        获取待整理文件项列表

        :param fileitem: 源文件项
        :param predicate: 用于筛选目录或文件项
            该函数接收两个参数：

            - `file_item`: 需要判断的文件项（类型为 `FileItem`）
            - `is_bluray_dir`: 表示该项是否为蓝光原盘目录（布尔值）

            函数应返回 `True` 表示保留该项，`False` 表示过滤掉

            若 `predicate` 为 `None`，则默认保留所有项
        :param verify_file_exists: 验证目录或文件是否存在，默认值为 `True`
        """
        if runtime_stop_state.is_system_stopped:
            raise OperationInterrupted()

        storagechain = StorageChain()

        def __is_bluray_sub(_path: str) -> bool:
            """
            判断是否蓝光原盘目录内的子目录或文件
            """
            return (
                True if re.search(r"BDMV[/\\]STREAM", _path, re.IGNORECASE) else False
            )

        def __get_bluray_dir(_storage: str, _path: Path) -> Optional[FileItem]:
            """
            获取蓝光原盘BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return storagechain.get_file_item(storage=_storage, path=p.parent)
            return None

        def _apply_predicate(
                file_item: FileItem, is_bluray_dir: bool
        ) -> List[Tuple[FileItem, bool]]:
            if predicate is None or predicate(file_item, is_bluray_dir):
                return [(file_item, is_bluray_dir)]
            return []

        if verify_file_exists:
            latest_fileitem = storagechain.get_item(fileitem)
            if not latest_fileitem:
                logger.warn(f"目录或文件不存在：{fileitem.path}")
                return []
            # 确保从历史记录重新整理时 能获得最新的源文件大小、修改日期等
            fileitem = latest_fileitem

        # 是否蓝光原盘子目录或文件
        if __is_bluray_sub(fileitem.path):
            if bluray_dir := __get_bluray_dir(fileitem.storage, Path(fileitem.path)):
                # 返回该文件所在的原盘根目录
                return _apply_predicate(bluray_dir, True)

        # 单文件
        if fileitem.type == "file":
            return _apply_predicate(fileitem, False)

        # 是否蓝光原盘根目录
        sub_items = storagechain.list_files(fileitem, recursion=False) or []
        if storagechain.contains_bluray_subdirectories(sub_items):
            # 当前目录是原盘根目录，不需要递归
            return _apply_predicate(fileitem, True)

        # 不是原盘根目录 递归获取目录内需要整理的文件项列表
        return [
            item
            for sub_item in sub_items
            for item in (
                self.__get_trans_fileitems(
                    sub_item, predicate, verify_file_exists=False
                )
                if sub_item.type == "dir"
                else _apply_predicate(sub_item, False)
            )
        ]

    @staticmethod
    def _get_shared_download_roots(file_path: Path) -> set[str]:
        """
        获取当前文件所在的共享下载根目录边界。

        父目录兜底回查只应在种子自身目录内进行，不能越过共享下载根目录，
        否则历史中的单文件/无子目录任务会污染同级其它文件的识别结果。
        """
        shared_roots: set[str] = set()
        media_type_dirs = {mtype.value for mtype in MediaType}
        media_categories = None

        for dir_info in DirectoryHelper().get_download_dirs():
            if not dir_info.download_path:
                continue

            download_root = Path(dir_info.download_path)
            if not file_path.is_relative_to(download_root):
                continue

            shared_roots.add(download_root.as_posix())
            relative_parts = file_path.relative_to(download_root).parts
            current_root = download_root
            part_index = 0
            media_type = dir_info.media_type

            if (
                    not dir_info.media_type
                    and dir_info.download_type_folder
                    and len(relative_parts) > part_index
                    and relative_parts[part_index] in media_type_dirs
            ):
                current_root = current_root / relative_parts[part_index]
                shared_roots.add(current_root.as_posix())
                media_type = relative_parts[part_index]
                part_index += 1

            if (
                    not dir_info.media_category
                    and dir_info.download_category_folder
                    and len(relative_parts) > part_index
            ):
                category_root = current_root / relative_parts[part_index]
                shared_roots.add(category_root.as_posix())
                if media_categories is None:
                    media_categories = MediaChain().media_category() or {}
                if media_type:
                    category_names = media_categories.get(media_type, [])
                else:
                    category_names = {
                        category
                        for categories in media_categories.values()
                        for category in categories
                    }
                category_paths = sorted(
                    (Path(category).parts for category in category_names if category),
                    key=len,
                )
                for category_parts in category_paths:
                    relative_category_parts = tuple(
                        relative_parts[part_index:part_index + len(category_parts)]
                    )
                    if relative_category_parts != category_parts:
                        continue
                    category_root = current_root
                    for category_part in category_parts:
                        category_root = category_root / category_part
                        shared_roots.add(category_root.as_posix())

        return shared_roots

    @staticmethod
    def _normalize_transfer_identity(
        mediainfo: Optional[Union[MediaInfo, MusicInfo]],
        mtype: Optional[MediaType],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        meta: Optional[MetaBase],
    ) -> Tuple[
        Optional[Union[MediaInfo, MusicInfo]],
        Optional[MediaSource],
        Optional[str],
        Optional[str],
    ]:
        """
        规范整理请求的媒体身份，并在显式身份缺失时短路。

        :return: ``(媒体信息、媒体来源、媒体 ID、错误信息)``；错误信息为空表示可继续执行
        """
        explicit_identity = media_source is not None or media_id is not None
        normalized_source, normalized_media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (
                not normalized_source or not normalized_media_id
        ):
            return (
                mediainfo,
                normalized_source,
                normalized_media_id,
                "整理任务需要同时提供有效的 media_source 和 media_id",
            )
        if not explicit_identity and mediainfo:
            normalized_source, normalized_media_id = resolve_media_identity(
                media=mediainfo
            )
        if explicit_identity and not mediainfo:
            mediainfo = MediaChain().recognize_media(
                mtype=mtype,
                media_source=normalized_source,
                media_id=normalized_media_id,
                music_type=getattr(meta, "music_type", None),
            )
            if not mediainfo:
                return (
                    mediainfo,
                    normalized_source,
                    normalized_media_id,
                    "未识别到媒体信息，"
                    f"media_source：{normalized_source}，media_id：{normalized_media_id}",
                )
        return mediainfo, normalized_source, normalized_media_id, None

    def _collect_transfer_candidates(
        self,
        fileitem: FileItem,
        batch_mtype: Optional[MediaType],
        min_filesize: int,
        epformat: Optional[EpisodeFormat],
        season: Optional[int],
        continue_callback: Optional[Callable],
    ) -> Tuple[List[Tuple[FileItem, bool]], bool]:
        """
        收集并过滤本次整理的候选文件。

        候选遍历只负责发现文件，业务过滤集中在此阶段；返回模板命中状态供公开整理流程
        保持“未命中自定义集数模板时跳过”的旧行为。
        """
        format_handler = (
            FormatParser(
                eformat=epformat.format,
                details=epformat.detail,
                part=epformat.part,
                offset=epformat.offset,
            )
            if epformat
            else None
        )
        has_template = bool(epformat and epformat.format)
        exclude_words = get_configured_system_config().get(
            SystemConfigKey.TransferExcludeWords
        )
        matched_template = False

        def keep_candidate(item: FileItem, _is_bluray_dir: bool) -> bool:
            """候选遍历阶段只响应取消请求，不提前应用业务过滤。"""
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            return True

        def is_allowed(item: FileItem, is_bluray_dir: bool) -> bool:
            """判断候选文件是否符合格式、后缀、大小和屏蔽词约束。"""
            nonlocal matched_template
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            if has_template and format_handler:
                if not format_handler.match(item.name):
                    return False
                matched_template = True
            if batch_mtype == MediaType.MUSIC:
                if self._is_music_lyrics_file(item):
                    return not self._is_blocked_by_exclude_words(item.path, exclude_words)
                if not self._is_media_file(item, batch_mtype):
                    return False
                if not self._is_allow_filesize(item, min_filesize):
                    return False
            elif (
                not is_bluray_dir
                and not self._is_subtitle_file(item)
                and not self._is_audio_file(item)
            ):
                if not self._is_media_file(item, batch_mtype):
                    return False
                if not self._is_allow_filesize(item, min_filesize):
                    return False
            if any(
                marker in item.path
                for marker in ("/@Recycle/", "/#recycle/", "/.", "/@eaDir")
            ):
                logger.debug(f"{item.path} 是回收站或隐藏的文件")
                return False
            return not self._is_blocked_by_exclude_words(item.path, exclude_words)

        candidates = self.__get_trans_fileitems(fileitem, predicate=keep_candidate)
        return [
            (item, is_bluray_dir)
            for item, is_bluray_dir in candidates
            if is_allowed(item, is_bluray_dir)
        ], matched_template

    def do_transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            target_directory: TransferDirectoryConf = None,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            transfer_type: Optional[str] = None,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            season: Optional[int] = None,
            epformat: EpisodeFormat = None,
            min_filesize: Optional[int] = 0,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
            force: Optional[bool] = False,
            background: Optional[bool] = True,
            manual: Optional[bool] = False,
            preview: Optional[bool] = False,
            sync_extra_files: Optional[bool] = False,
            cleanup_dest_fileitem: Optional[FileItem] = None,
            continue_callback: Callable = None,
            reorganize: Optional[bool] = False,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        兼容公开整理入口，委托给内部批次执行阶段。

        公开签名是 API、工作流、监控器和插件共同使用的稳定契约；具体整理阶段保留在
        内部方法中，后续可以独立拆分规划、执行和结算，而不迫使调用方迁移参数。
        """
        return self._execute_transfer(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            target_directory=target_directory,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            season=season,
            epformat=epformat,
            min_filesize=min_filesize,
            downloader=downloader,
            download_hash=download_hash,
            force=force,
            background=background,
            manual=manual,
            preview=preview,
            sync_extra_files=sync_extra_files,
            cleanup_dest_fileitem=cleanup_dest_fileitem,
            continue_callback=continue_callback,
            reorganize=reorganize,
        )

    def _execute_transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            target_directory: TransferDirectoryConf = None,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            transfer_type: Optional[str] = None,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            season: Optional[int] = None,
            epformat: EpisodeFormat = None,
            min_filesize: Optional[int] = 0,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
            force: Optional[bool] = False,
            background: Optional[bool] = True,
            manual: Optional[bool] = False,
            preview: Optional[bool] = False,
            sync_extra_files: Optional[bool] = False,
            cleanup_dest_fileitem: Optional[FileItem] = None,
            continue_callback: Callable = None,
            reorganize: Optional[bool] = False,
            recovery_admission: Optional[TransferAdmission] = None,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        执行一个复杂目录的整理操作
        :param fileitem: 文件项
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param mtype: 未提供媒体信息时使用的媒体类型提示
        :param media_source: 请求级识别与刮削数据源
        :param media_id: 数据源原生 ID；显式指定身份时与 media_source 成对传入
        :param target_directory:  目标目录配置
        :param target_storage: 目标存储器
        :param target_path: 目标路径
        :param transfer_type: 整理类型
        :param scrape: 是否刮削元数据
        :param library_type_folder: 媒体库类型子目录
        :param library_category_folder: 媒体库类别子目录
        :param season: 季
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param downloader: 下载器
        :param download_hash: 下载记录hash
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param manual: 是否手动整理
        :param preview: 是否仅预览
        :param reorganize: 是否清理已有成功记录后重新整理
        :param sync_extra_files: 是否在整理主视频文件时同步整理同媒体附加文件
        :param cleanup_dest_fileitem: 确认存在待整理任务后需要清理的旧目标文件
        :param continue_callback: 继续处理回调
        :param recovery_admission: 内部恢复调用绑定的既有 durable 记录
        返回：成功标识，错误信息
        """
        mediainfo, media_source, media_id, identity_error = (
            self._normalize_transfer_identity(
                mediainfo=mediainfo,
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                meta=meta,
            )
        )
        if identity_error:
            return False, identity_error

        # 是否全部成功
        all_success = True
        transfer_batch_id = str(uuid.uuid4())
        batch_mtype = getattr(mediainfo, "type", None)
        if batch_mtype in (None, MediaType.UNKNOWN):
            batch_mtype = mtype
        if preview:
            # 预览模式始终同步执行，避免进入异步队列
            background = False
        # 自定义格式
        has_episode_format_template = bool(epformat and epformat.format)
        formaterHandler = (
            FormatParser(
                eformat=epformat.format,
                details=epformat.detail,
                part=epformat.part,
                offset=epformat.offset,
            )
            if epformat
            else None
        )

        # 汇总错误信息
        err_msgs: List[str] = []
        transfer_exclude_words = get_configured_system_config().get(
            SystemConfigKey.TransferExcludeWords
        )

        def _build_file_meta(
                source_path: Path,
                custom_word_list: Optional[List[str]] = None,
        ) -> Optional[MetaBase]:
            """
            构建整理任务使用的文件元数据，并应用手动季集/自定义格式覆盖。
            """
            built_meta = deepcopy(meta) if meta else _build_path_meta(
                source_path, custom_word_list=custom_word_list
            )
            if not built_meta:
                return None
            if not meta:
                # _build_path_meta 已经应用过手动季集/自定义格式覆盖；
                # 这里避免再次偏移集数，导致手动整理的集数偏移翻倍。
                return built_meta
            return _apply_meta_overrides(built_meta, source_path)

        def _has_reliable_video_source() -> bool:
            """
            是否存在可靠的影视类型来源；存在时音频按附加音轨解析，
            避免影视场景的音频文件误入音乐识别。
            """
            if batch_mtype is not None:
                return batch_mtype != MediaType.MUSIC
            # 预载媒体信息为非音乐时，整批整理视为影视上下文
            return mediainfo is not None and not isinstance(mediainfo, MusicInfo)

        def _build_path_meta(
                source_path: Path,
                custom_word_list: Optional[List[str]] = None,
                force_video: Optional[bool] = False,
        ) -> Optional[MetaBase]:
            """
            从文件路径识别媒体信息，用于判断附加文件是否属于当前主视频。
            :param force_video: 强制按视频解析，附加文件归属匹配专用，避免音乐判定干扰归属比较
            """
            # 音频后缀且无可靠影视类型来源时按音乐解析，走 MusicBrainz 识别链
            if (
                    not force_video
                    and source_path.suffix.lower() in self._audio_exts
                    and not _has_reliable_video_source()
            ):
                path_meta = MediaChain.read_path_meta(source_path)
            else:
                # 影视场景附加音轨（如评论音轨）强制按视频解析，保留季集归属
                path_meta = MetaInfoPath(
                    source_path, custom_words=custom_word_list, force_video=True
                )
            if not path_meta:
                return None
            return _apply_meta_overrides(path_meta, source_path)

        def _apply_meta_overrides(
                current_meta: MetaBase, source_path: Path
        ) -> Optional[MetaBase]:
            """
            应用手动传入的季集覆盖和自定义识别格式。
            """
            # 合并季
            if season is not None:
                current_meta.begin_season = season

            # 自定义识别
            if formaterHandler:
                # 开始集、结束集、PART
                begin_ep, end_ep, part = formaterHandler.split_episode(
                    file_name=source_path.name, file_meta=current_meta
                )
                if begin_ep is not None:
                    current_meta.begin_episode = begin_ep
                if part is not None:
                    current_meta.part = part
                if end_ep is not None:
                    current_meta.end_episode = end_ep

            return current_meta

        def _is_allowed_transfer_item(item: FileItem, _is_bluray_dir: bool) -> bool:
            """筛选单文件模式额外读取的字幕/音频，保持模板和屏蔽词语义。"""
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            if has_episode_format_template and formaterHandler and not formaterHandler.match(item.name):
                return False
            if any(
                marker in item.path
                for marker in ("/@Recycle/", "/#recycle/", "/.", "/@eaDir")
            ):
                return False
            return not self._is_blocked_by_exclude_words(item.path, transfer_exclude_words)

        def _build_main_meta(
                main_fileitem: FileItem,
                main_bluray_dir: bool,
                download_history_repository: DownloadHistoryQueryPort,
        ) -> Optional[MetaBase]:
            """
            构建主视频元数据。
            """
            main_path = Path(main_fileitem.path)
            main_download_history = self._resolve_download_history(
                repository=download_history_repository,
                file_path=main_path,
                bluray_dir=main_bluray_dir,
                download_hash=download_hash,
            )
            return _build_file_meta(
                main_path,
                custom_word_list=self._get_subscribe_custom_words(main_download_history),
            )

        def _append_item(
                planned_items: List[Tuple[FileItem, bool]],
                seen_file_keys: set[Tuple[str, str]],
                item: FileItem,
                is_bluray_dir: bool,
        ) -> bool:
            """
            添加待整理文件项并去重。
            """
            file_key = self._get_file_key(item)
            if file_key in seen_file_keys:
                return False
            planned_items.append((item, is_bluray_dir))
            seen_file_keys.add(file_key)
            return True

        def _build_directory_index(
                items: List[Tuple[FileItem, bool]]
        ) -> Tuple[
            Dict[Tuple[str, str], List[FileItem]],
            Dict[Tuple[str, str], List[Tuple[FileItem, bool]]],
        ]:
            """
            基于已遍历结果构建同目录主视频和附加文件索引。
            """
            main_items_by_dir: Dict[Tuple[str, str], List[FileItem]] = {}
            extra_items_by_dir: Dict[Tuple[str, str], List[Tuple[FileItem, bool]]] = {}
            for item, is_bluray_dir in items:
                if not item or item.type != "file":
                    continue
                dir_key = self._get_file_parent_key(item)
                if not is_bluray_dir and self._is_media_file(item, batch_mtype):
                    main_items_by_dir.setdefault(dir_key, []).append(item)
                elif (
                        self._is_subtitle_file(item)
                        or self._is_audio_file(item)
                        or self._is_music_lyrics_file(item)
                ):
                    extra_items_by_dir.setdefault(dir_key, []).append((item, is_bluray_dir))
            return main_items_by_dir, extra_items_by_dir

        def _get_single_file_sibling_items(
                current_fileitem: FileItem,
        ) -> Tuple[List[FileItem], List[Tuple[FileItem, bool]]]:
            """
            单文件整理时只额外读取一次父目录，收集同目录主视频和附加文件。
            """
            storagechain = StorageChain()
            if not hasattr(storagechain, "get_parent_item") or not hasattr(
                    storagechain, "list_files"
            ):
                return [], []
            parent_item = storagechain.get_parent_item(current_fileitem)
            if not parent_item:
                return [], []
            main_fileitems: List[FileItem] = []
            extra_items: List[Tuple[FileItem, bool]] = []
            for item in storagechain.list_files(parent_item, recursion=False) or []:
                if not item or item.type != "file":
                    continue
                if self._is_media_file(item, batch_mtype):
                    main_fileitems.append(item)
                    continue
                if not (
                        self._is_subtitle_file(item)
                        or self._is_audio_file(item)
                        or self._is_music_lyrics_file(item)
                ):
                    continue
                if not _is_allowed_transfer_item(item, False):
                    continue
                extra_items.append((item, False))
            return main_fileitems, extra_items

        def _plan_file_items(
                items: List[Tuple[FileItem, bool]]
        ) -> Tuple[List[Tuple[FileItem, bool]], Dict[Tuple[str, str], MetaBase]]:
            """
            生成最终整理顺序：主视频优先，同名附加文件跟随，剩余附加文件最后处理。
            """
            if not items:
                return [], {}

            download_history_repository = self.download_history_repository
            inherited_map: Dict[Tuple[str, str], MetaBase] = {}
            main_items_by_dir, extra_items_by_dir = _build_directory_index(items)
            main_items = [
                (item, is_bluray_dir)
                for item, is_bluray_dir in items
                if item
                   and (
                           is_bluray_dir
                           or (
                                   item.type == "file"
                                   and self._is_media_file(item, batch_mtype)
                           )
                   )
            ]

            single_file_mode = len(items) == 1 and fileitem.type == "file"
            if single_file_mode:
                current_item, current_bluray_dir = items[0]
                if current_item.type == "file":
                    sibling_main_items, sibling_extra_items = _get_single_file_sibling_items(
                        current_item
                    )
                    current_dir_key = self._get_file_parent_key(current_item)
                    if not current_bluray_dir and self._is_media_file(
                            current_item, batch_mtype
                    ):
                        main_items = [(current_item, current_bluray_dir)]
                        main_items_by_dir[current_dir_key] = [current_item]
                        extra_items_by_dir[current_dir_key] = sibling_extra_items
                    elif (
                            self._is_subtitle_file(current_item)
                            or self._is_audio_file(current_item)
                            or self._is_music_lyrics_file(current_item)
                    ):
                        related_main_file_key = self._get_related_main_file_key(
                            extra_fileitem=current_item,
                            main_fileitems=sibling_main_items,
                        )
                        related_main_fileitem = next(
                            (
                                main_item
                                for main_item in sibling_main_items
                                if self._get_file_key(main_item) == related_main_file_key
                            ),
                            None,
                        )
                        if related_main_fileitem:
                            main_meta = _build_main_meta(
                                related_main_fileitem,
                                False,
                                download_history_repository,
                            )
                            if main_meta:
                                inherited_map[self._get_file_key(current_item)] = deepcopy(main_meta)
                        return list(items), inherited_map

            if not main_items:
                remaining = [
                    item
                    for item in items
                    if not (
                            batch_mtype == MediaType.MUSIC
                            and self._is_music_lyrics_file(item[0])
                    )
                ]
                return remaining, inherited_map

            planned_items: List[Tuple[FileItem, bool]] = []
            seen_file_keys: set[Tuple[str, str]] = set()
            extra_meta_cache: Dict[Tuple[str, Tuple[str, ...]], Optional[MetaBase]] = {}

            def _get_cached_extra_meta(
                    extra_path: Path,
                    custom_word_list: Optional[List[str]],
            ) -> Optional[MetaBase]:
                """
                同一组识别词下的附加文件只解析一次。
                """
                custom_words_key = tuple(custom_word_list or [])
                cache_key = (extra_path.as_posix(), custom_words_key)
                if cache_key not in extra_meta_cache:
                    # 归属匹配专用视频解析：此处目的是判断附加文件是否跟随主视频，
                    # 若按音乐解析会导致影视目录内的音频无法与主视频比较归属
                    extra_meta_cache[cache_key] = _build_path_meta(
                        extra_path,
                        custom_word_list=list(custom_words_key) or None,
                        force_video=True,
                    )
                return extra_meta_cache[cache_key]

            for main_item, main_bluray_dir in main_items:
                _append_item(planned_items, seen_file_keys, main_item, main_bluray_dir)
                if main_bluray_dir or not self._is_media_file(
                        main_item, batch_mtype
                ):
                    continue

                main_path = Path(main_item.path)
                main_download_history = self._resolve_download_history(
                    repository=download_history_repository,
                    file_path=main_path,
                    bluray_dir=main_bluray_dir,
                    download_hash=download_hash,
                )
                subscribe_custom_words = self._get_subscribe_custom_words(
                    main_download_history
                )
                main_meta = _build_file_meta(
                    main_path,
                    custom_word_list=subscribe_custom_words,
                )
                if not main_meta:
                    continue

                dir_key = self._get_file_parent_key(main_item)
                main_fileitems = main_items_by_dir.get(dir_key) or [main_item]
                main_file_key = self._get_file_key(main_item)
                for extra_item, extra_bluray_dir in extra_items_by_dir.get(dir_key, []):
                    if self._get_file_key(extra_item) in seen_file_keys:
                        continue
                    related_main_file_key = self._get_related_main_file_key(
                        extra_fileitem=extra_item,
                        main_fileitems=main_fileitems,
                    )
                    if related_main_file_key:
                        if related_main_file_key == main_file_key:
                            if _append_item(
                                    planned_items,
                                    seen_file_keys,
                                    extra_item,
                                    extra_bluray_dir,
                            ):
                                inherited_map[self._get_file_key(extra_item)] = deepcopy(main_meta)
                        continue

                    if single_file_mode or not sync_extra_files:
                        continue

                    extra_meta = _get_cached_extra_meta(
                        Path(extra_item.path),
                        subscribe_custom_words,
                    )
                    if not self._is_same_media_meta(main_meta, extra_meta):
                        continue
                    if _append_item(
                            planned_items,
                            seen_file_keys,
                            extra_item,
                            extra_bluray_dir,
                    ):
                        inherited_map[self._get_file_key(extra_item)] = deepcopy(extra_meta)

            for item, is_bluray_dir in items:
                if (
                        batch_mtype == MediaType.MUSIC
                        and self._is_music_lyrics_file(item)
                        and self._get_file_key(item) not in inherited_map
                ):
                    continue
                _append_item(planned_items, seen_file_keys, item, is_bluray_dir)

            return planned_items, inherited_map

        try:
            file_items, matched_episode_format_template = self._collect_transfer_candidates(
                fileitem=fileitem,
                batch_mtype=batch_mtype,
                min_filesize=min_filesize or 0,
                epformat=epformat,
                season=season,
                continue_callback=continue_callback,
            )
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"

        if not file_items:
            if has_episode_format_template and not matched_episode_format_template:
                logger.info(f"{fileitem.path} 未匹配到集数定位模板，跳过整理")
                if preview:
                    return True, {
                        "summary": {"total": 0, "success": 0, "failed": 0},
                        "items": [],
                        "message": "",
                    }
                return True, ""
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        file_items, inherited_meta_map = _plan_file_items(file_items)

        planned_file_count = len(file_items)

        if preview:
            logger.info(f"正在预览 {planned_file_count} 个文件的整理路径...")
        else:
            logger.info(f"正在计划整理 {planned_file_count} 个文件...")

        # 整理所有文件
        transfer_tasks: List[TransferTask] = []
        skipped_history_count = 0
        skipped_torrents = set()
        cleanup_intent_assigned = False
        try:
            for file_item, bluray_dir in file_items:
                if runtime_stop_state.is_system_stopped:
                    raise OperationInterrupted()
                if continue_callback and not continue_callback():
                    raise OperationInterrupted()
                file_path = Path(file_item.path)

                # 自动整理按 app/application/history.py 的统一判定去重（失败记录放行重试、
                # 成功但源文件已变化放行交 overwrite_mode 决断）；手动整理可清理失败记录，
                # 或按用户确认清理成功记录。
                if (not force or reorganize) and not preview:
                    transfer_history_oper = self.transfer_history_repository
                    transferd = self._get_manual_transfer_history(
                        fileitem=file_item,
                        transfer_history_oper=transfer_history_oper,
                        include_move_dest=bool(manual and reorganize),
                    )
                    if transferd:
                        should_reorganize = manual and (
                                reorganize or not transferd.status
                        )
                        if should_reorganize:
                            durable_retry = self._request_durable_transfer_retry(
                                transferd,
                                requested_by="manual_reorganize",
                            )
                            if durable_retry is not None:
                                accepted, message = durable_retry
                                if accepted:
                                    logger.info(message)
                                else:
                                    all_success = False
                                    logger.error(message)
                                    err_msgs.append(message)
                                # durable 历史只登记唯一调度重试，不在当前调用重新准入。
                                continue
                            state, message = self._delete_manual_transfer_history(
                                history=transferd,
                                transfer_history_oper=transfer_history_oper,
                            )
                            if not state:
                                all_success = False
                                logger.error(message)
                                err_msgs.append(message)
                                continue
                            logger.info(
                                f"{file_item.path} 已清理旧整理记录，继续重新整理。"
                            )
                            transferd = None

                    if transferd:
                        history_description = describe_history_gate(
                            transferd,
                            file_size=file_item.size,
                            file_modify_time=file_item.modify_time,
                            fileid=file_item.fileid,
                        )
                        if not manual:
                            # 自动路径（目录监控、下载器轮询）与监控分发共用同一套判定，
                            # 否则监控层刚放行的失败重试与升级请求会在这里被全额收回
                            gate_action = evaluate_history_gate(
                                transferd,
                                file_size=file_item.size,
                                file_modify_time=file_item.modify_time,
                                fileid=file_item.fileid,
                            )
                            if not is_skip_action(gate_action):
                                logger.info(
                                    f"{file_item.path} 命中"
                                    f"{history_description}"
                                    f"，重新送入整理"
                                )
                                transferd = None

                        if transferd:
                            skipped_history_count += 1
                            if not transferd.status:
                                all_success = False
                            # 失败记录能走到这里说明重试次数已用尽，此时同样要打已整理标签让种子
                            # 退出轮询，否则下载器每一轮都会重新扫描并在这里被拦一次，空转且刷屏
                            candidate_hash = download_hash or transferd.download_hash
                            candidate_downloader = downloader or transferd.downloader
                            if candidate_hash and candidate_downloader:
                                skipped_torrents.add(
                                    (candidate_hash, candidate_downloader)
                                )
                            logger.info(
                                f"{file_item.path} 已整理过（"
                                f"{history_description}"
                                f"），如需重新处理，请删除整理记录。"
                            )
                            err_msgs.append(f"{file_item.name} 已整理过")
                            continue

                # 提前获取下载历史，以便获取自定义识别词
                download_history_repository = self.download_history_repository
                download_history = self._resolve_download_history(
                    repository=download_history_repository,
                    file_path=file_path,
                    bluray_dir=bluray_dir,
                    download_hash=download_hash,
                )

                history_music_meta, history_music_info = self._restore_music_download_context(
                    download_history=download_history,
                    file_path=file_path,
                )

                if not meta:
                    # 文件元数据(优先使用订阅识别词)
                    inherited_meta = inherited_meta_map.get(
                        self._get_file_key(file_item)
                    )
                    if history_music_meta:
                        file_meta = history_music_meta
                    elif inherited_meta:
                        file_meta = deepcopy(inherited_meta)
                    else:
                        file_meta = _build_file_meta(
                            file_path,
                            custom_word_list=self._get_subscribe_custom_words(download_history),
                        )
                else:
                    file_meta = _build_file_meta(file_path)

                if not file_meta:
                    all_success = False
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    err_msgs.append(f"{file_path.name} 无法识别有效信息")
                    continue

                # 获取下载Hash
                if download_history and (not downloader or not download_hash):
                    _downloader = download_history.downloader
                    _download_hash = download_history.download_hash
                else:
                    _downloader = downloader
                    _download_hash = download_hash

                # 自动整理预载的媒体信息来自整条下载历史；电影合集内文件年份冲突时逐文件识别。
                task_mediainfo = mediainfo or history_music_info
                if not task_mediainfo and isinstance(file_meta, MetaMusic):
                    # 无标签音频按目录级专辑匹配补齐曲目身份，命中结果带缓存不会逐文件重复请求
                    file_meta, task_mediainfo = self._match_music_album_context(
                        file_item, file_path, file_meta
                    )
                if (
                        not manual
                        and task_mediainfo
                        and self._is_movie_year_conflict(file_meta, task_mediainfo)
                ):
                    task_mediainfo = None

                # 后台整理
                transfer_task = TransferTask(
                    fileitem=file_item,
                    meta=file_meta,
                    mediainfo=task_mediainfo,
                    media_source=media_source,
                    media_id=media_id,
                    mtype=batch_mtype,
                    target_directory=target_directory,
                    target_storage=target_storage,
                    target_path=target_path,
                    transfer_type=transfer_type,
                    scrape=scrape,
                    library_type_folder=library_type_folder,
                    library_category_folder=library_category_folder,
                    downloader=_downloader,
                    download_hash=_download_hash,
                    download_history=download_history,
                    transfer_batch_id=transfer_batch_id,
                    manual=manual,
                    background=background,
                    preview=preview,
                )
                cleanup_intent = (
                    cleanup_dest_fileitem
                    if not preview and not cleanup_intent_assigned
                    else None
                )
                transfer_task.bind_planning_input(
                    self.__build_planning_input(
                        transfer_task,
                        cleanup_dest_fileitem=cleanup_intent,
                    )
                )
                if (
                        recovery_admission
                        and file_item.storage == recovery_admission.storage
                        and file_item.path == recovery_admission.src_path
                ):
                    transfer_task.bind_admission_task_id(recovery_admission.task_id)
                    self.__bind_claimed_admission(
                        transfer_task,
                        recovery_admission,
                    )
                    if recovery_admission.planning_input:
                        transfer_task.bind_planning_input(
                            recovery_admission.planning_input
                        )
                    if recovery_admission.checkpoint:
                        transfer_task.bind_plan_checkpoint(
                            recovery_admission.checkpoint
                        )
                if background:
                    try:
                        queued = self.put_to_queue(task=transfer_task)
                    except Exception as err:
                        all_success = False
                        message = f"{file_path.name} 加入整理队列失败：{err}"
                        err_msgs.append(message)
                        logger.error(message)
                        continue
                    if queued:
                        if cleanup_intent:
                            cleanup_intent_assigned = True
                        logger.info(f"{file_path.name} 已添加到整理队列")
                    else:
                        logger.debug(f"{file_path.name} 已在整理队列中，跳过")
                else:
                    # 加入列表
                    if self.__put_to_jobview(transfer_task):
                        self._register_scrape_batch_task(transfer_task)
                        transfer_tasks.append(transfer_task)
                        if cleanup_intent:
                            cleanup_intent_assigned = True
                    else:
                        logger.debug(f"{file_path.name} 已在整理列表中，跳过")
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"
        finally:
            file_items.clear()
            del file_items
            self._close_scrape_batch(transfer_batch_id)

        # 实时整理
        preview_items: List[dict[str, Any]] = []

        def _preview_callback(task: TransferTask, transferinfo: TransferInfo) -> Tuple[bool, str]:
            item_meta = task.meta
            item_media = task.mediainfo
            preview_items.append(
                {
                    "source": task.fileitem.path,
                    "target": transferinfo.target_item.path if transferinfo.target_item else None,
                    "target_dir": transferinfo.target_diritem.path if transferinfo.target_diritem else None,
                    "success": transferinfo.success,
                    "message": transferinfo.message,
                    "type": item_media.type.value if item_media and item_media.type else None,
                    "title": item_media.title_year if item_media else None,
                    "season": item_meta.begin_season if item_meta else None,
                    "episode": item_meta.begin_episode if item_meta else None,
                    "episode_end": item_meta.end_episode if item_meta else None,
                    "part": item_meta.part if item_meta else None,
                    "org_string": item_meta.org_string if item_meta else None,
                    "apply_words": item_meta.apply_words if item_meta else [],
                    "resource_team": item_meta.resource_team if item_meta else None,
                    "customization": item_meta.customization if item_meta else None,
                }
            )
            return transferinfo.success, transferinfo.message

        if transfer_tasks:
            # 总数量
            total_num = len(transfer_tasks)
            # 已处理数量
            processed_num = 0
            # 失败数量
            fail_num = 0
            # 已完成文件
            finished_files = []

            progress = None
            if not preview:
                # 启动进度
                progress = ProgressHelper(ProgressKey.FileTransfer)
                progress.start()
                __process_msg = f"开始整理，共 {total_num} 个文件 ..."
                logger.info(__process_msg)
                progress.update(value=0, text=__process_msg)
            try:
                for transfer_task in transfer_tasks:
                    if runtime_stop_state.is_system_stopped:
                        break
                    if continue_callback and not continue_callback():
                        break
                    if not preview:
                        # 更新进度
                        __process_msg = f"正在整理 （{processed_num + fail_num + 1}/{total_num}）{transfer_task.fileitem.name} ..."
                        logger.info(__process_msg)
                        progress.update(
                            value=(processed_num + fail_num) / total_num * 100,
                            text=__process_msg,
                            data={
                                "current": Path(transfer_task.fileitem.path).as_posix(),
                                "finished": finished_files,
                            },
                        )
                    terminal = False
                    terminal_settlement: Optional[bool] = None

                    def callback_after_terminal_settlement(
                            callback_task: TransferTask,
                            transferinfo: TransferInfo,
                    ) -> Tuple[bool, str]:
                        """同步路径也由默认回调原子提交历史、事件与 durable 终态。"""
                        nonlocal terminal_settlement
                        callback = (
                            _preview_callback
                            if preview
                            else self.__default_callback
                        )
                        try:
                            return callback(callback_task, transferinfo)
                        finally:
                            if not callback_task.preview:
                                terminal_settlement = callback_task.terminal_settled

                    try:
                        self.__claim_task_for_execution(transfer_task)
                        self.__start_job_execution(transfer_task)
                        state, err_msg = self.__handle_transfer(
                            task=transfer_task,
                            callback=callback_after_terminal_settlement,
                        )
                        terminal = bool(preview or transfer_task.plan_checkpoint is not None)
                    except Exception as e:
                        if terminal_settlement is not None:
                            terminal = True
                        logger.error(
                            f"{transfer_task.fileitem.name} 整理任务处理出现错误："
                            f"{e} - {traceback.format_exc()}"
                        )
                        if not preview:
                            self.__fail_transfer_task(transfer_task)
                        state, err_msg = False, str(e)
                    finally:
                        durable_settled = self.__finish_job_execution(
                            transfer_task,
                            terminal=terminal,
                            terminal_settlement=terminal_settlement,
                        )
                    if terminal and not durable_settled:
                        state = False
                        err_msg = "整理任务 durable 终态结算失去租约"
                    if not state:
                        all_success = False
                        logger.warn(f"{transfer_task.fileitem.name} {err_msg}")
                        err_msgs.append(f"{transfer_task.fileitem.name} {err_msg}")
                        if preview:
                            # 预览模式不走默认回调，这里需要手动收敛任务状态，避免残留 running
                            self.jobview.fail_task(transfer_task)
                            self.jobview.try_remove_job(transfer_task)
                        if preview and (
                                not preview_items or preview_items[-1].get("source") != transfer_task.fileitem.path):
                            preview_items.append(
                                {
                                    "source": transfer_task.fileitem.path,
                                    "target": None,
                                    "target_dir": None,
                                    "success": False,
                                    "message": err_msg,
                                    "type": None,
                                    "title": None,
                                    "season": transfer_task.meta.begin_season if transfer_task.meta else None,
                                    "episode": transfer_task.meta.begin_episode if transfer_task.meta else None,
                                    "episode_end": transfer_task.meta.end_episode if transfer_task.meta else None,
                                    "part": transfer_task.meta.part if transfer_task.meta else None,
                                    "org_string": transfer_task.meta.org_string if transfer_task.meta else None,
                                    "apply_words": transfer_task.meta.apply_words if transfer_task.meta else [],
                                    "resource_team": transfer_task.meta.resource_team if transfer_task.meta else None,
                                    "customization": transfer_task.meta.customization if transfer_task.meta else None,
                                }
                            )
                        fail_num += 1
                    else:
                        if preview:
                            # 预览模式手动标记完成，确保可重复预览
                            self.jobview.finish_task(transfer_task)
                            self.jobview.try_remove_job(transfer_task)
                        processed_num += 1
                    # 记录已完成
                    finished_files.append(Path(transfer_task.fileitem.path).as_posix())
            finally:
                transfer_tasks.clear()
                del transfer_tasks

            # 整理结束
            if not preview:
                __end_msg = (
                    f"整理队列处理完成，共整理 {total_num} 个文件，失败 {fail_num} 个"
                )
                logger.info(__end_msg)
                progress.update(value=100, text=__end_msg, data={})
                progress.end()

        # 下载器任务在这一轮可能因为历史记录全部命中而没有进入整理队列，
        # 这里补打一遍已整理标签，避免同一种子被重复扫描。
        if (
                skipped_history_count == planned_file_count
                and skipped_torrents
        ):
            for skipped_hash, skipped_downloader in skipped_torrents:
                logger.info(f"补充设置下载任务已整理标签：{skipped_hash}")
                self.__mark_torrent_completed_if_done(
                    skipped_hash, skipped_downloader
                )

        error_msg = "、".join(err_msgs[:2]) + (
            f"，等{len(err_msgs)}个文件错误！" if len(err_msgs) > 2 else ""
        )
        if preview:
            return all_success, {
                "summary": {
                    "total": len(preview_items),
                    "success": len([item for item in preview_items if item.get("success")]),
                    "failed": len([item for item in preview_items if not item.get("success")]),
                },
                "items": preview_items,
                "message": error_msg,
            }
        return all_success, error_msg

    def remote_transfer(
            self,
            arg_str: str,
            channel: NotificationChannel,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        远程重新整理，参数为历史记录 ID，或媒体来源、原生 ID 与类型。
        """

        def args_error():
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="请输入正确的命令格式：/redo [id] 或 "
                          "/redo [id] [media_source]|[media_id]|[类型]，"
                          "[id] 为整理记录编号",
                    userid=userid,
                    save_history=False,
                )
            )

        if not arg_str:
            args_error()
            return
        arg_strs = str(arg_str).split()
        if len(arg_strs) not in (1, 2):
            args_error()
            return
        # 历史记录ID
        logid = arg_strs[0]
        if not logid.isdigit():
            args_error()
            return
        if len(arg_strs) == 1:
            state, errmsg = self.redo_transfer_history(int(logid))
            if not state:
                self.post_message(
                    Message(
                        channel=channel,
                        title="手动整理失败",
                        source=source,
                        text=errmsg,
                        userid=userid,
                        link=self.runtime_config.history_url,
                        save_history=False,
                    )
                )
            return
        # 显式媒体身份固定为来源、原生 ID 和媒体类型三个字段。
        id_strs = arg_strs[1].split("|")
        if len(id_strs) != 3:
            args_error()
            return
        media_source, media_id, type_str = id_strs
        try:
            normalized_source = MediaSource(media_source)
        except ValueError:
            args_error()
            return
        if not type_str or type_str not in [
            MediaType.MOVIE.value,
            MediaType.TV.value,
            MediaType.MUSIC.value,
        ]:
            args_error()
            return
        state, errmsg = self._re_transfer(
            logid=int(logid),
            mtype=MediaType(type_str),
            media_source=normalized_source,
            media_id=media_id,
        )
        if not state:
            self.post_message(
                Message(
                    channel=channel,
                    title="手动整理失败",
                    source=source,
                    text=errmsg,
                    userid=userid,
                    link=self.runtime_config.history_url,
                    save_history=False,
                )
            )
            return

    def manual_transfer(
            self,
            fileitem: FileItem,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            mtype: MediaType = None,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            transfer_type: Optional[str] = None,
            epformat: EpisodeFormat = None,
            min_filesize: Optional[int] = 0,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            force: Optional[bool] = False,
            background: Optional[bool] = False,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
            preview: Optional[bool] = False,
            sync_extra_files: Optional[bool] = True,
            cleanup_dest_fileitem: Optional[FileItem] = None,
            reorganize: Optional[bool] = False,
            music_type: Optional[str] = None,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        手动整理，支持复杂条件，带进度显示
        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID，必须与 media_source 成对提供
        :param mtype: 媒体类型
        :param season: 季度
        :param episode_group: 剧集组
        :param transfer_type: 整理类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型建立目录
        :param library_category_folder: 是否按类别建立目录
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param downloader: 下载器名称
        :param download_hash: 下载任务哈希
        :param preview: 是否仅预览
        :param reorganize: 是否清理已有成功记录后重新整理
        :param sync_extra_files: 是否同步整理同媒体附加文件
        :param cleanup_dest_fileitem: 确认存在待整理任务后需要清理的旧目标文件
        :param music_type: 音乐实体类型；为保持位置参数兼容，必须追加在签名末尾
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        explicit_identity = media_source is not None or media_id is not None
        if explicit_identity and (not media_source or not media_id):
            return False, "手动整理需要同时提供 media_source 和 media_id"
        if media_source and media_id:
            # 有输入媒体ID时预先识别，音乐与影视统一走 recognize_media 按类型分发
            mediainfo = MediaChain().recognize_media(
                media_source=media_source,
                media_id=media_id,
                music_type=music_type,
                mtype=mtype,
                episode_group=episode_group,
            )
            if not mediainfo:
                return (
                    False,
                    f"媒体信息识别失败，media_source：{media_source}，media_id：{media_id}，"
                    f"type: {mtype.value if mtype else None}",
                )
            if media_source and not isinstance(mediainfo, MusicInfo):
                mediainfo.scrape_source = media_source
            if not isinstance(mediainfo, MusicInfo):
                self.obtain_images(mediainfo=mediainfo)

            # 开始整理
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                mediainfo=mediainfo,
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                transfer_type=transfer_type,
                season=season,
                epformat=epformat,
                min_filesize=min_filesize,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                force=force,
                background=background,
                manual=True,
                downloader=downloader,
                download_hash=download_hash,
                preview=preview,
                reorganize=reorganize,
                sync_extra_files=sync_extra_files,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
            )
            if not state:
                return False, errmsg

            logger.info(f"{fileitem.path} 整理完成")
            return True, errmsg if preview else ""
        else:
            # 没有输入媒体ID时，按文件识别
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                media_source=media_source,
                mtype=mtype,
                transfer_type=transfer_type,
                season=season,
                epformat=epformat,
                min_filesize=min_filesize,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                force=force,
                background=background,
                manual=True,
                downloader=downloader,
                download_hash=download_hash,
                preview=preview,
                reorganize=reorganize,
                sync_extra_files=sync_extra_files,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
            )
            return state, errmsg

    def send_transfer_message(
            self,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            transferinfo: TransferInfo,
            season_episode: Optional[str] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            username: Optional[str] = None,
    ):
        """
        发送入库成功的消息
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param transferinfo: 文件整理信息
        :param season_episode: 已入库季集文本
        :param episodes_info: 当前季的全部集信息
        :param username: 用户名
        """
        self.post_message(
            Message(
                mtype=MessageType.Organize,
                ctype=ContentType.OrganizeSuccess,
                image=mediainfo.get_message_image(),
                username=username,
                link=self.runtime_config.history_url,
            ),
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode=season_episode,
            episodes_info=episodes_info,
            username=username,
        )

    @staticmethod
    def _is_blocked_by_exclude_words(file_path: str, exclude_words: list) -> bool:
        """
        检查文件是否被整理屏蔽词阻止处理
        :param file_path: 文件路径
        :param exclude_words: 整理屏蔽词列表
        :return: 如果被屏蔽返回True，否则返回False
        """
        if not exclude_words:
            return False

        for keyword in exclude_words:
            if keyword and re.search(r"%s" % keyword, file_path, re.IGNORECASE):
                logger.warn(f"{file_path} 命中屏蔽词 {keyword}")
                return True
        return False

    def _can_delete_torrent(
            self, download_hash: str, downloader: str, transfer_exclude_words
    ) -> bool:
        """
        检查是否可以删除种子文件
        :param download_hash: 种子Hash
        :param downloader: 下载器名称
        :param transfer_exclude_words: 整理屏蔽词
        :return: 如果可以删除返回True，否则返回False
        """
        try:
            # 获取种子信息
            torrents = self.list_torrents(hashs=download_hash, downloader=downloader)
            if not torrents:
                return False

            # 未下载完成
            if torrents[0].progress < 100:
                return False

            # 获取种子文件列表
            torrent_files = self.torrent_files(download_hash, downloader)
            if not torrent_files:
                return False

            if not isinstance(torrent_files, list):
                torrent_files = torrent_files.data

            # 检查是否有媒体文件未被屏蔽且存在
            save_path = torrents[0].path.parent
            for file in torrent_files:
                file_path = save_path / file.name
                # 如果存在未被屏蔽的媒体文件，则不删除种子
                if (
                        file_path.suffix in self._allowed_exts
                        and not self._is_blocked_by_exclude_words(
                    file_path.as_posix(), transfer_exclude_words
                )
                        and file_path.exists()
                ):
                    return False

            # 所有媒体文件都被屏蔽或不存在，可以删除种子
            return True

        except Exception as e:
            logger.error(f"检查种子 {download_hash} 是否需要删除失败：{e}")
            return False
