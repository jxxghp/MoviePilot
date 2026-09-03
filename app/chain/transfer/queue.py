"""整理队列、工作线程、租约与恢复调度。"""

import asyncio
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic_core import to_jsonable_python

from app.application.chain.context import ChainRuntimeContext
from app.application.directory import DirectoryHelper
from app.application.history import (
    DownloadHistorySnapshot,
)
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionConflictError,
    TransferExecutionRepository,
    TransferExecutionSnapshot,
    TransferExecutionState,
)
from app.application.transfer.workflow import (
    JobManager,
    TransferAdmission,
    TransferFailureNotificationAggregator,
    TransferLeaseLostError,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferQueue,
    TransferQueueService,
    TransferTask,
)
from app.chain.media import MediaChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.chain.transfer.records import apply_download_history_classification
from app.runtime.log import logger
from app.runtime.progress import ProgressHelper
from app.runtime.stop import runtime_stop_state
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo, TransferJob
from app.schemas.types import (
    MediaSource,
    MediaType,
    ProgressKey,
    TorrentStatus,
)
from app.schemas.workflow import FileItem

task_lock = threading.Lock()
downloader_lock = threading.Lock()

_WORKER_RESTART_TIMEOUT_SECONDS = 30.0
_WORKER_CLOSE_TIMEOUT_SECONDS = 30.0


class TransferQueueOwner(_TransferOwnerBase):
    """唯一持有整理工作线程、队列、租约和恢复调度。"""

    _WORKER_RESTART_TIMEOUT_SECONDS = _WORKER_RESTART_TIMEOUT_SECONDS
    _WORKER_CLOSE_TIMEOUT_SECONDS = _WORKER_CLOSE_TIMEOUT_SECONDS

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
        self._TransferChain__init()

    def _TransferChain__init(self) -> bool:
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
                        target=self._TransferChain__start_transfer,
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
    def _TransferChain__join_threads(
            threads: List[threading.Thread], deadline: float
    ) -> List[threading.Thread]:
        """在统一截止时间内等待线程，返回仍未收敛且继续由调用方持有的线程。"""
        current_thread = threading.current_thread()
        for thread in threads:
            if thread is current_thread or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return [thread for thread in threads if thread.is_alive()]

    def _TransferChain__acquire_worker_lifecycle_lock(self, deadline: float) -> bool:
        """在统一截止时间内取得 worker 生命周期锁，并兼容同线程 RLock 重入。"""
        return self._worker_lifecycle_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        )

    def _TransferChain__request_worker_stop(self) -> List[threading.Thread]:
        """发布当前 worker 代的停止信号，并用哨兵唤醒空闲线程。"""
        with self._worker_state_lock:
            self._queue_active = False
            self._worker_stop_event.set()
            current_threads = list(self._threads)
            threads = [*self._retiring_threads, *current_threads]
            for _ in current_threads:
                self._queue.put(self._QUEUE_STOP_SENTINEL)
            return threads

    def _TransferChain__stop(self, timeout_seconds: float = _WORKER_RESTART_TIMEOUT_SECONDS) -> bool:
        """在锁等待与线程 join 的共享预算内停止当前 worker 代。"""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        if not self._TransferChain__acquire_worker_lifecycle_lock(deadline):
            logger.error(
                "未在 %.1f 秒内取得文件整理 worker 生命周期锁",
                max(0.0, timeout_seconds),
            )
            return False
        try:
            threads = self._TransferChain__request_worker_stop()
            alive_threads = self._TransferChain__join_threads(threads, deadline)
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
        self._TransferChain__ensure_lease_runtime_state()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        if not self._TransferChain__acquire_worker_lifecycle_lock(deadline):
            logger.error(
                "未在 %.1f 秒内取得整理后台生命周期锁，关闭未开始",
                max(0.0, timeout_seconds),
            )
            return False
        try:
            with self._worker_state_lock:
                self._closing = True
                worker_threads = self._TransferChain__request_worker_stop()
                replay_thread = self._replay_thread
                heartbeat_thread = self._lease_heartbeat_thread
                self._replay_stop_event.set()
                self._recovery_wakeup_event.set()

            alive_workers = self._TransferChain__join_threads(worker_threads, deadline)
            alive_replays = self._TransferChain__join_threads(
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
                release_thread = self._TransferChain__start_lease_release_owner_locked(
                    error="整理宿主关闭，释放未结算任务租约"
                )
            alive_releases = self._TransferChain__join_threads(
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
            alive_heartbeats = self._TransferChain__join_threads(
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
            if not self._TransferChain__stop(
                    timeout_seconds=self._WORKER_RESTART_TIMEOUT_SECONDS
            ):
                logger.warning(
                    "旧文件整理 worker 仍在收尾；其停止信号保持有效，新一代接管后续队列"
                )
            self._TransferChain__init()

    def _TransferChain__get_transfer_target_dir_path(
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
                return self._TransferChain__enqueue_claimed_task(task)
            return self._transfer_queue_service().put(task, self._TransferChain__default_callback)

    def _transfer_queue_service(self) -> TransferQueueService:
        """构建保持旧队列对象和私有兼容接缝的应用服务。"""
        return TransferQueueService(
            register_task=self._TransferChain__put_to_jobview,
            admit_task=self._TransferChain__admit_transfer,
            enqueue=self._queue.put,
            before_enqueue=self._register_scrape_batch_task,
            enqueue_failed=self._TransferChain__record_enqueue_failure,
            remove_task=self.jobview.remove_task,
            list_tasks=self.jobview.list_jobs,
            expire_tasks=self._TransferChain__expire_stale_transfer_tasks,
        )

    def replay_pending(self) -> None:
        """
        启动唯一恢复调度 owner，并唤醒一次即时恢复扫描。

        启动回放、同进程入队补偿和租约过期接管都经由这个入口。调度线程只负责
        claim 和重新入队；实际业务仍由普通整理 worker 执行。
        """
        self._TransferChain__ensure_recovery_scheduler(immediate=True)

    def _TransferChain__ensure_recovery_scheduler(self, *, immediate: bool) -> None:
        """确保唯一恢复调度 owner 存在，并只为显式请求执行即时扫描。"""
        self._TransferChain__ensure_lease_runtime_state()
        with self._worker_state_lock:
            if self._closing:
                logger.info("文件整理链正在关闭，跳过待处理文件回放")
                return
            self._TransferChain__start_lease_heartbeat_owner_locked()
            if self._replay_thread and self._replay_thread.is_alive():
                if immediate:
                    self._recovery_wakeup_event.set()
                return
            thread = threading.Thread(
                target=self._TransferChain__run_replay_pending,
                args=(self._replay_stop_event, immediate),
                name="MoviePilot-TransferReplay",
                daemon=True,
            )
            self._replay_thread = thread
            if immediate:
                self._recovery_wakeup_event.set()
            # 在状态锁内启动，避免 close_workers 看到尚未 start 的线程后错误 join。
            thread.start()

    def _TransferChain__run_replay_pending(
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
                self._TransferChain__replay_pending(stop_event)
                self._recovery_wakeup_event.wait(
                    timeout=self._RECOVERY_POLL_INTERVAL_SECONDS
                )
        finally:
            with self._worker_state_lock:
                if self._replay_thread is threading.current_thread():
                    self._replay_thread = None

    def _TransferChain__ensure_lease_runtime_state(self) -> None:
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

    def _TransferChain__start_lease_heartbeat_owner_locked(self) -> None:
        """在状态锁内确保当前进程只有一个租约续期线程。"""
        heartbeat_thread = self._lease_heartbeat_thread
        if heartbeat_thread and heartbeat_thread.is_alive():
            return
        heartbeat_thread = threading.Thread(
            target=self._TransferChain__run_lease_heartbeat,
            args=(self._lease_heartbeat_stop_event,),
            name="MoviePilot-TransferLeaseHeartbeat",
            daemon=True,
        )
        self._lease_heartbeat_thread = heartbeat_thread
        heartbeat_thread.start()

    def _TransferChain__ensure_lease_heartbeat_owner(self) -> None:
        """按需启动进程级租约续期 owner，供启动前到达的普通任务使用。"""
        self._TransferChain__ensure_lease_runtime_state()
        with self._worker_state_lock:
            if not self._closing:
                self._TransferChain__start_lease_heartbeat_owner_locked()

    def _TransferChain__run_lease_heartbeat(self, stop_event: threading.Event) -> None:
        """按固定周期续期本进程已 claim 且尚未结算的任务。"""
        try:
            while not stop_event.wait(self._LEASE_HEARTBEAT_INTERVAL_SECONDS):
                self._TransferChain__heartbeat_owned_leases()
        finally:
            with self._worker_state_lock:
                if self._lease_heartbeat_thread is threading.current_thread():
                    self._lease_heartbeat_thread = None

    def _TransferChain__heartbeat_owned_leases(self) -> None:
        """经 Application Port 续期所有排队中或执行中的任务租约。"""
        self._TransferChain__ensure_lease_runtime_state()
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
                    self._TransferChain__forget_owned_lease(task_id, lease_token)
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
                self._TransferChain__forget_owned_lease(task_id, lease_token)
                logger.error(f"整理任务租约已失效或被接管：{task_id}")
                continue
            with self._worker_state_lock:
                current = self._owned_leases.get(task_id)
                if current and current[0] == lease_token:
                    self._owned_leases[task_id] = (
                        lease_token,
                        time.monotonic() + self._WORKER_LEASE_SECONDS,
                    )

    def _TransferChain__forget_owned_lease(self, task_id: str, lease_token: str) -> None:
        """仅在 token 仍匹配时移除本进程租约镜像，避免删掉新接管记录。"""
        with self._worker_state_lock:
            current = self._owned_leases.get(task_id)
            if current and current[0] == lease_token:
                self._owned_leases.pop(task_id, None)
            self._queued_lease_tokens.discard((task_id, lease_token))

    def _TransferChain__is_claimed_task_enqueued(self, task_id: str, lease_token: str) -> bool:
        """返回指定 claim 是否已经成功进入普通 worker 队列。"""
        with self._worker_state_lock:
            return (task_id, lease_token) in self._queued_lease_tokens

    def _TransferChain__owns_lease(self, task_id: str, lease_token: Optional[str]) -> bool:
        """返回本地续期镜像是否仍持有指定 token。"""
        if not lease_token:
            return False
        with self._worker_state_lock:
            current = self._owned_leases.get(task_id)
            return bool(current and current[0] == lease_token)

    def _TransferChain__assert_owned_lease(self, task: TransferTask) -> None:
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

    def _TransferChain__release_task_claim(
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
            return bool(
                self._transfer_admissions.release_claim(
                    task_id=task_id,
                    lease_token=lease_token,
                    error=error,
                )
            )
        except Exception as err:
            logger.error(f"释放整理任务租约失败：{task_id} - {err}")
            return False
        finally:
            self._TransferChain__forget_owned_lease(task_id, lease_token)
            self._TransferChain__ensure_recovery_scheduler(immediate=False)

    def _TransferChain__release_admission_claim(
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
            self._TransferChain__forget_owned_lease(admission.task_id, admission.lease_token)

    def _TransferChain__release_all_owned_leases(self, *, error: str) -> None:
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
                self._TransferChain__forget_owned_lease(task_id, lease_token)

    def _TransferChain__start_lease_release_owner_locked(
            self, *, error: str
    ) -> Optional[threading.Thread]:
        """启动并持有唯一租约释放线程，使同步数据库阻塞不突破关闭预算。"""
        release_thread = self._lease_release_thread
        if release_thread is not None:
            return release_thread
        if not self._owned_leases:
            return None
        release_thread = threading.Thread(
            target=self._TransferChain__release_all_owned_leases,
            kwargs={"error": error},
            name="MoviePilot-TransferLeaseRelease",
            daemon=True,
        )
        self._lease_release_thread = release_thread
        release_thread.start()
        return release_thread

    def _TransferChain__enqueue_claimed_task(self, task: TransferTask) -> bool:
        """把已 claim 的恢复任务送入普通队列，禁止再次准入或二次 claim。"""
        self._TransferChain__assert_owned_lease(task)
        if not self._TransferChain__put_to_jobview(task):
            logger.warning(
                "恢复任务被内存作业视图判定为重复，未进入队列：task_id=%s, source=%s:%s",
                task.admission_task_id,
                task.fileitem.storage,
                task.fileitem.path,
            )
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
                TransferQueue(task=task, callback=self._TransferChain__default_callback)
            )
        except Exception as err:
            try:
                self._TransferChain__record_enqueue_failure(task, err)
            finally:
                self.jobview.remove_task(task.fileitem)
            raise
        return True

    def _TransferChain__replay_pending(
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
        self._TransferChain__ensure_lease_runtime_state()
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
                self._TransferChain__register_claimed_admission(admission)
        except Exception as err:
            logger.error(f"登记恢复任务租约失败：{err}")
            for claimed in pendings:
                self._TransferChain__release_admission_claim(claimed, error=str(err))
            return
        logger.info(f"发现 {len(pendings)} 个上次未整理完的文件，正在重新送入整理链 ...")
        replayed = 0
        for index, admission in enumerate(pendings):
            if stop_event.is_set():
                for unprocessed in pendings[index:]:
                    self._TransferChain__release_admission_claim(
                        unprocessed,
                        error="整理宿主关闭，恢复任务尚未入队",
                    )
                break
            storage = admission.storage
            src_path = admission.src_path
            try:
                execution_snapshot = self._TransferChain__execution_replay_snapshot(admission)
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
                    fileitem, should_discard = self._TransferChain__build_replay_fileitem(
                        storage,
                        src_path,
                        admission.planning_input,
                    )
                # stat 等同步 I/O 返回后重新检查，关闭期间不得注销尚未完成的登记。
                if stop_event.is_set():
                    self._TransferChain__release_admission_claim(
                        admission,
                        error="整理宿主关闭，恢复任务尚未入队",
                    )
                    for unprocessed in pendings[index + 1:]:
                        self._TransferChain__release_admission_claim(
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
                            self._TransferChain__release_admission_claim(
                                admission,
                                error="源已消失但任务状态已变化，保留登记供恢复",
                            )
                        else:
                            self._TransferChain__forget_owned_lease(
                                admission.task_id,
                                lease_token,
                            )
                    else:
                        self._TransferChain__release_admission_claim(
                            admission,
                            error="恢复源文件暂时不可读取",
                        )
                    continue
                if admission.checkpoint:
                    if self._TransferChain__queue_planned_replay(
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
                        self._TransferChain__release_admission_claim(
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
                    if self._TransferChain__queue_accepted_replay(fileitem, admission):
                        replayed += 1
                    else:
                        self._TransferChain__release_admission_claim(
                            admission,
                            error="恢复任务未进入内存队列",
                        )
                    continue
                replay_kwargs = self._TransferChain__build_replay_kwargs(planning_input)
                self._execute_transfer(
                    fileitem=fileitem,
                    recovery_admission=admission,
                    **replay_kwargs,
                )
                assert admission.lease_token is not None
                if self._TransferChain__is_claimed_task_enqueued(
                        admission.task_id,
                        admission.lease_token,
                ):
                    replayed += 1
                else:
                    self._TransferChain__release_admission_claim(
                        admission,
                        error="旧恢复入口未产生可执行队列任务",
                    )
            except Exception as err:
                logger.error(f"回放待整理文件失败：{storage}:{src_path} - {err}")
                if self._TransferChain__owns_lease(admission.task_id, admission.lease_token):
                    self._TransferChain__release_admission_claim(admission, error=str(err))
        if stop_event.is_set():
            logger.info(
                "待整理文件回放收到关闭请求，已送入 %s 个文件，其余登记保持待处理",
                replayed,
            )
        else:
            self._TransferChain__log_replay_summary(replayed, len(pendings))

    @staticmethod
    def _TransferChain__log_replay_summary(replayed: int, claimed: int) -> None:
        """按实际入队结果记录恢复汇总，避免零任务仍输出成功标记。"""
        if replayed:
            logger.info(f"✓ 待整理文件回放完成，{replayed} 个文件已重新送入整理链")
            return
        logger.warning(
            f"待整理文件回放未入队：claim {claimed} 个，"
            "原因见前序日志与 transferpending.last_error"
        )

    def _TransferChain__execution_replay_snapshot(
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
    def _TransferChain__build_replay_kwargs(
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

    def _TransferChain__queue_accepted_replay(
            self,
            fileitem: FileItem,
            admission: TransferAdmission,
    ) -> bool:
        """从 accepted 输入离线恢复显式领域上下文并送入单任务队列。"""
        planning_input = admission.planning_input
        if planning_input is None:
            raise TransferPlanningStateError("accepted 回放缺少整理规划输入")
        options = planning_input.options
        replay_kwargs = self._TransferChain__build_replay_kwargs(planning_input)
        task = TransferTask(
            fileitem=fileitem,
            meta=(
                self._TransferChain__restore_meta_snapshot(
                    planning_input.meta,
                    options.get("_meta_kind"),
                )
                if planning_input.meta
                else None
            ),
            mediainfo=(
                self._TransferChain__restore_mediainfo_snapshot(
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
        self._TransferChain__bind_claimed_admission(task, admission)
        task.bind_planning_input(planning_input)
        if planning_input.mediainfo:
            task.mark_planning_context_restored()
        return self.put_to_queue(task)

    def _TransferChain__queue_planned_replay(
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
        self._TransferChain__bind_claimed_admission(task, admission)
        task.bind_planning_input(checkpoint.planning_input)
        task.bind_plan_checkpoint(checkpoint)
        if execution_checkpoint is not None:
            task.bind_execution_checkpoint(execution_checkpoint)
        self._TransferChain__restore_planned_task(task)
        return self.put_to_queue(task)

    @staticmethod
    def _TransferChain__build_replay_fileitem(
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

    @staticmethod
    def _TransferChain__json_snapshot(value: Any) -> Any:
        """把受控领域对象投影为可持久化 JSON 值。"""
        if value is None:
            return None
        return to_jsonable_python(value, serialize_unknown=True)

    def _TransferChain__record_enqueue_failure(
            self,
            task: TransferTask,
            error: Exception,
    ) -> None:
        """按是否已经 claim 选择 fencing 失败记录，并撤销批次占位。"""
        try:
            if task.lease_token:
                self._TransferChain__release_task_claim(task, error=str(error))
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
            self._TransferChain__ensure_recovery_scheduler(immediate=False)

    def _TransferChain__put_to_jobview(self, task: TransferTask) -> bool:
        """
        添加到作业视图
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        return bool(self.jobview.add_task(task))



    def remove_from_queue(self, fileitem: FileItem):
        """
        从待整理队列移除
        """
        self._transfer_queue_service().remove(fileitem)

    def _TransferChain__start_job_execution(self, task: TransferTask):
        """在作业视图支持执行租约时标记主程序任务开始执行。"""
        marker = getattr(self.jobview, "start_execution", None)
        if marker:
            marker(task)


    def _TransferChain__expire_stale_transfer_tasks(self) -> None:
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


    def _TransferChain__settle_transfer_progress_if_idle(self) -> None:
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

    def _TransferChain__start_transfer(self, stop_event: threading.Event) -> None:
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
                    self._TransferChain__settle_transfer_progress_if_idle()
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
                    self._TransferChain__settle_transfer_progress_if_idle()
                    continue

                if task.admission_task_id and task.lease_token:
                    with self._worker_state_lock:
                        self._queued_lease_tokens.discard(
                            (task.admission_task_id, task.lease_token)
                        )
                try:
                    self._TransferChain__claim_task_for_execution(task)
                except TransferLeaseLostError as err:
                    logger.info(f"跳过未取得执行租约的整理任务：{err}")
                    self._TransferChain__release_task_claim(task, error=str(err))
                    self.jobview.try_remove_job(task)
                    self._finish_scrape_batch_task(task)
                    self._queue.task_done()
                    self._TransferChain__settle_transfer_progress_if_idle()
                    continue
                except Exception as err:
                    logger.error(
                        f"整理任务 claim 失败，保留 durable admission：{err}"
                    )
                    self.jobview.try_remove_job(task)
                    self._finish_scrape_batch_task(task)
                    self._queue.task_done()
                    self._TransferChain__settle_transfer_progress_if_idle()
                    self._TransferChain__ensure_recovery_scheduler(immediate=False)
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
                    self._TransferChain__start_job_execution(task)
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
                    state, err_msg = self._TransferChain__handle_transfer(
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
                    self._TransferChain__fail_transfer_task(task)
                    with task_lock:
                        self._processed_num += 1
                        self._fail_num += 1
                finally:
                    durable_settled = False
                    try:
                        durable_settled = self._TransferChain__finish_job_execution(
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
                        self._TransferChain__settle_transfer_progress_if_idle()

            except queue.Empty:
                # 即使队列空了，如果还有任务在运行，也不应该结束进度
                # 这部分逻辑已经在 finally 的 active_tasks == 0 中处理了
                self._TransferChain__expire_stale_transfer_tasks()
                continue
            except Exception as e:
                logger.error(f"整理队列处理出现错误：{e} - {traceback.format_exc()}")



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
                            mediainfo = apply_download_history_classification(
                                mediainfo,
                                downloadhis,
                            )

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
