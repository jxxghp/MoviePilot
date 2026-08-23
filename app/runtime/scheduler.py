"""定时作业的通用执行引擎与声明类型。

引擎只认识作业声明：登记运行状态、按声明展开调度器触发、执行作业并收敛进度。
它不认识任何具体业务，业务作业清单由组合根提供。

引擎同时持有生命周期门禁与事件循环句柄：提交入口按状态放行，已投递的协程由引擎
自己拥有，关停时可以取消并等待其真实收尾；同一作业标识的每次登记分配单调代次，
热重载替换作业定义后旧代次不会改写新作业的状态与进度。
"""

import asyncio
import concurrent.futures
import inspect
import multiprocessing
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from app.runtime.config import global_vars
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.runtime.progress import AsyncProgressHelper, ProgressHelper
from app.runtime.scheduling import TimerUtils
from app.schemas.dashboard import ScheduleInfo as _SchemaScheduleInfo
from app.schemas.dashboard import ScheduleProgress as _SchemaScheduleProgress
from app.schemas.types import EventType

# 定时服务进度缓存键前缀
SCHEDULER_PROGRESS_PREFIX = "scheduler"
# 保护调度器整体启停的进程级锁，与作业状态锁分开以规避关停阻塞导致的死锁
lock = threading.Lock()

# 循环内提交得到 asyncio.Future，跨线程提交得到 concurrent.futures.Future
LoopHandle = Union[asyncio.Future, concurrent.futures.Future]

# 允许提交新运行实例的生命周期状态
_ACCEPTING_STATES = frozenset({"starting", "running"})
# 不再允许新建事件循环兜底执行的生命周期状态
_CLOSING_STATES = frozenset({"stopping", "stopped"})


@dataclass(frozen=True)
class ScheduledTrigger:
    """
    一条定时作业触发登记。

    :param trigger: 触发类型或已构建的触发器对象
    :param options: 透传给调度器的触发参数
    :param suffix: 同一作业登记多条触发时附加到调度器任务 id 的后缀
    :param name: 调度器任务显示名，缺省时沿用作业名
    :param replace_existing: 同 id 任务是否覆盖登记
    """

    trigger: Any
    options: Dict[str, Any] = field(default_factory=dict)
    suffix: str = ""
    name: Optional[str] = None
    replace_existing: bool = False


@dataclass(frozen=True)
class ScheduledJob:
    """
    一条定时作业声明。

    :param id: 作业标识，同时是运行状态登记键
    :param name: 作业显示名
    :param func: 作业执行体
    :param kwargs: 调用执行体时透传的关键字参数
    :param provider_name: 作业提供方显示名，缺省由引擎按系统作业展示
    :param manual: 是否只允许手动执行
    :param triggers: 作业展开的调度器触发登记
    """

    id: str
    name: str
    func: Callable[..., Any]
    kwargs: Optional[Dict[str, Any]] = None
    provider_name: Optional[str] = None
    manual: bool = False
    triggers: Tuple[ScheduledTrigger, ...] = ()


@dataclass(slots=True)
class SchedulerHandle:
    """
    引擎提交到事件循环的一次执行登记。

    :param job_id: 句柄所属的作业标识
    :param generation: 句柄所属的作业代次
    :param loop: 承载本次执行的事件循环
    :param handle: 提交代理，用于向所属循环请求取消
    :param completion: 真实收尾信号，用于等待协程结束而不是等待代理变为取消
    """

    job_id: str
    generation: int
    loop: asyncio.AbstractEventLoop
    handle: LoopHandle
    completion: LoopHandle


class SchedulerEngine:
    """定时作业的执行引擎：登记、触发、执行与进度收敛。"""

    def __init__(self):
        """创建引擎状态；后台调度器由宿主显式装配。"""
        # 定时服务
        self._scheduler = None
        # 退出事件
        self._event = threading.Event()
        # 锁
        self._lock = threading.RLock()
        # 各服务的运行状态
        self._jobs = {}
        # 生命周期门禁与事件循环句柄由引擎实例独立持有
        self._lifecycle_state = "new"
        self._handles: Dict[int, SchedulerHandle] = {}
        self._job_generations: Dict[str, int] = {}
        # 运行所有权独立于可热重建的作业定义，避免重载期间同 ID 作业并行执行
        self._active_job_generations: Dict[str, Set[int]] = {}
        # 显式触发入口的独占预约，值为持有预约的线程标识
        self._agent_task_reservations: Dict[str, int] = {}

    @staticmethod
    def _get_progress_key(job_id: str) -> str:
        """
        获取定时服务进度缓存键。
        """
        return f"{SCHEDULER_PROGRESS_PREFIX}:{job_id}"

    @staticmethod
    def _format_time(value: Optional[datetime] = None) -> str:
        """
        格式化进度事件时间。
        """
        return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    def _accepting_submissions(self) -> bool:
        """
        判断引擎是否仍允许提交新的运行实例。
        """
        return self._lifecycle_state in _ACCEPTING_STATES

    def _next_job_generation(self, job_id: str) -> int:
        """
        为同一作业标识的下一次登记分配单调代次。

        :param job_id: 作业标识
        :return: 本次登记的代次
        """
        generation = self._job_generations.get(job_id, 0) + 1
        self._job_generations[job_id] = generation
        return generation

    def _assign_job_generation(self, job_id: str, job: Dict[str, Any]) -> None:
        """
        把登记代次写入作业的可变运行状态。

        :param job_id: 作业标识
        :param job: 作业运行状态
        """
        job["_generation"] = self._next_job_generation(job_id)

    def _is_job_active(self, job_id: str) -> bool:
        """
        判断任一代次的同标识作业是否仍在真实执行。

        :param job_id: 作业标识
        """
        return bool(self._active_job_generations.get(job_id))

    def _release_job_generation(self, job_id: str, generation: int) -> None:
        """
        在作业真实收尾后释放对应代次的运行所有权。

        :param job_id: 作业标识
        :param generation: 作业代次
        """
        active_generations = self._active_job_generations.get(job_id)
        if not active_generations:
            return
        active_generations.discard(generation)
        if not active_generations:
            self._active_job_generations.pop(job_id, None)

    def _finish_unsubmitted_job(
            self,
            job_id: str,
            job: Dict[str, Any],
            generation: int,
            error: Optional[str],
    ) -> None:
        """
        收尾协程无法提交时同步释放作业状态与运行所有权。

        :param job_id: 作业标识
        :param job: 作业运行状态
        :param generation: 作业代次
        :param error: 写入终态的错误信息
        """
        finished_at = self._format_time()
        with self._lock:
            if generation not in self._active_job_generations.get(job_id, set()):
                return
            current_job = self._jobs.get(job_id)
            if current_job is job and current_job.get("_generation", 0) == generation:
                job.update(
                    running=False,
                    last_finished_at=finished_at,
                    last_error=error,
                )
            self._release_job_generation(job_id, generation)

    def _remove_handle(self, handle: LoopHandle) -> None:
        """
        执行句柄完成后从所有权登记中移除。

        :param handle: 已完成的收尾信号
        """
        with self._lock:
            self._handles.pop(id(handle), None)

    def _accepts_handle(self, job_id: str, generation: int) -> bool:
        """
        判断新句柄是否属于当前运行期或热重载中的既有作业。

        :param job_id: 作业标识
        :param generation: 作业代次
        """
        if self._accepting_submissions():
            return True
        current_job = self._jobs.get(job_id)
        return bool(
            self._lifecycle_state == "reloading"
            and current_job is not None
            and current_job.get("_generation", 0) == generation
            and current_job.get("running")
        )

    def _register_handle(
            self,
            job_id: str,
            generation: int,
            loop: asyncio.AbstractEventLoop,
            handle: LoopHandle,
            completion: Optional[LoopHandle] = None,
    ) -> bool:
        """
        登记引擎拥有的句柄；关停竞态下拒绝并取消新句柄。

        :param job_id: 作业标识
        :param generation: 作业代次
        :param loop: 承载执行的事件循环
        :param handle: 提交代理
        :param completion: 真实收尾信号，缺省与提交代理相同
        :return: 是否登记成功
        """
        if completion is None:
            completion = handle
        with self._lock:
            if not self._accepts_handle(job_id, generation):
                if isinstance(handle, concurrent.futures.Future):
                    handle.cancel()
                elif loop.is_running():
                    loop.call_soon_threadsafe(handle.cancel)
                else:
                    handle.cancel()
                return False
            self._handles[id(completion)] = SchedulerHandle(
                job_id=job_id,
                generation=generation,
                loop=loop,
                handle=handle,
                completion=completion,
            )
        completion.add_done_callback(self._remove_handle)
        return True

    @staticmethod
    def _cancel_handle(handle: SchedulerHandle) -> None:
        """
        从句柄所属线程安全地请求取消。

        :param handle: 引擎持有的执行登记
        """
        target = handle.handle
        if isinstance(target, concurrent.futures.Future):
            target.cancel()
            return
        if target.done():
            return
        if target.get_loop().is_running():
            target.get_loop().call_soon_threadsafe(target.cancel)
        else:
            target.cancel()

    @staticmethod
    async def _wait_handle(handle: SchedulerHandle) -> None:
        """
        等待取消请求到达协程 finally，而不是只等待提交代理变为取消。

        :param handle: 引擎持有的执行登记
        """
        target = handle.completion
        if isinstance(target, concurrent.futures.Future):
            await asyncio.shield(asyncio.wrap_future(target))
            return
        if target.get_loop() is asyncio.get_running_loop():
            await asyncio.shield(target)

    async def _await_cancelled_handles(
            self,
            handles: Tuple[SchedulerHandle, ...],
    ) -> None:
        """
        等待已投递协程结束，关停总预算由应用生命周期统一控制。

        :param handles: 关停快照中的执行登记
        """
        if not handles:
            return
        await asyncio.gather(
            *(self._wait_handle(handle) for handle in handles),
            return_exceptions=True,
        )

    @staticmethod
    def _track_cross_thread_completion(
            coro: Any,
            completion: concurrent.futures.Future,
            started: threading.Event,
    ) -> Any:
        """
        把跨线程提交代理与协程真实终态分离。

        :param coro: 待执行协程
        :param completion: 真实收尾信号
        :param started: 协程已开始执行的标记
        :return: 包装后的协程
        """

        async def _tracked() -> None:
            """在目标循环上执行协程并把终态写入完成信号。"""
            started.set()
            try:
                result = await coro
            except asyncio.CancelledError:
                if not completion.done():
                    completion.cancel()
            except Exception as err:
                if not completion.done():
                    completion.set_exception(err)
            else:
                if not completion.done():
                    completion.set_result(result)

        return _tracked()

    def _submit_cross_thread(
            self,
            coro: Any,
            *,
            target_loop: asyncio.AbstractEventLoop,
            job_id: str,
            generation: int,
            on_unstarted_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        向目标循环提交协程，并以独立完成信号跟踪真实收尾。

        :param coro: 待执行协程
        :param target_loop: 承载执行的目标事件循环
        :param job_id: 作业标识
        :param generation: 作业代次
        :param on_unstarted_cancel: 协程未开始即被取消时的回调
        :return: 是否提交并登记成功
        """
        completion: concurrent.futures.Future = concurrent.futures.Future()
        handle: concurrent.futures.Future = concurrent.futures.Future()
        started = threading.Event()
        tracked = self._track_cross_thread_completion(coro, completion, started)
        task_lock = threading.Lock()
        target_task: Optional[asyncio.Task] = None

        def complete_target_task(task: asyncio.Task) -> None:
            """把目标循环上的任务终态回填到提交代理与完成信号。"""
            if task.cancelled() and not started.is_set():
                if on_unstarted_cancel:
                    on_unstarted_cancel()
                if not completion.done():
                    completion.cancel()
            elif not completion.done():
                error = task.exception()
                if error is None:
                    completion.set_result(None)
                else:
                    completion.set_exception(error)
            if not handle.done():
                handle.set_result(None)

        def start_on_target_loop() -> None:
            """在目标循环线程内创建任务；代理已取消时释放两层协程。"""
            nonlocal target_task
            with task_lock:
                if handle.cancelled():
                    tracked.close()
                    coro.close()
                    if on_unstarted_cancel:
                        on_unstarted_cancel()
                    completion.cancel()
                    return
                target_task = target_loop.create_task(tracked)
                target_task.add_done_callback(complete_target_task)

        def cancel_target_task(submitted: concurrent.futures.Future) -> None:
            """提交代理被取消后把取消请求转达到目标循环上的任务。"""
            if not submitted.cancelled():
                return
            with task_lock:
                task = target_task
            if task is not None and not task.done():
                target_loop.call_soon_threadsafe(task.cancel)

        with self._lock:
            if not self._accepts_handle(job_id, generation):
                tracked.close()
                coro.close()
                return False
            try:
                target_loop.call_soon_threadsafe(start_on_target_loop)
            except RuntimeError:
                tracked.close()
                coro.close()
                return False

            registered = self._register_handle(
                job_id=job_id,
                generation=generation,
                loop=target_loop,
                handle=handle,
                completion=completion,
            )
            handle.add_done_callback(cancel_target_task)
        return registered

    def _register_job(self, job: ScheduledJob) -> None:
        """
        登记一条定时作业的运行状态与全部调度器触发。

        :param job: 作业声明
        """
        state = {
            "name": job.name,
            "func": job.func,
            "running": False,
        }
        if job.kwargs:
            state["kwargs"] = job.kwargs
        if job.provider_name:
            state["provider_name"] = job.provider_name
        if job.manual:
            state["manual"] = True
        self._assign_job_generation(job.id, state)
        self._jobs[job.id] = state
        for trigger in job.triggers:
            self._scheduler.add_job(
                self.start,
                trigger=trigger.trigger,
                id=f"{job.id}{trigger.suffix}",
                name=trigger.name or job.name,
                kwargs={"job_id": job.id},
                **({"replace_existing": True} if trigger.replace_existing else {}),
                **trigger.options,
            )

    def __prepare_job(self, job_id: str) -> Optional[dict]:
        """
        准备定时任务
        """
        started_at = self._format_time()
        with self._lock:
            if not self._accepting_submissions():
                return None
            reservation_owner = self._agent_task_reservations.get(job_id)
            if reservation_owner is not None:
                if reservation_owner != threading.get_ident():
                    return None
                self._agent_task_reservations.pop(job_id, None)
            job = self._jobs.get(job_id)
            if not job:
                return None
            if self._is_job_active(job_id) or job.get("running"):
                logger.warning(f"定时任务 {job_id} - {job.get('name')} 正在运行 ...")
                return None
            job.update(
                running=True,
                last_started_at=started_at,
                last_finished_at=None,
                last_error=None,
            )
            generation = job.get("_generation", 0)
            self._active_job_generations.setdefault(job_id, set()).add(generation)
        progress = ProgressHelper(self._get_progress_key(job_id))
        progress.start()
        progress.update(
            value=0,
            text=f"{job.get('name') or job_id} 开始执行 ...",
            data={
                "id": job_id,
                "_generation": job.get("_generation", 0),
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
                "started_at": started_at,
                "finished_at": None,
                "error": None,
            },
        )
        return job

    async def __finish_job(
            self,
            job_id: str,
            job: dict,
            generation: int,
            success: bool = True,
            error: Optional[str] = None,
    ) -> None:
        """
        完成定时任务
        """
        finished_at = self._format_time()
        with self._lock:
            current_job = self._jobs.get(job_id)
            if current_job is not job or current_job.get("_generation", 0) != generation:
                # 热重载已替换同 ID 作业定义，旧代次只释放所有权，不改写新状态
                self._release_job_generation(job_id, generation)
                return
            job.update(
                running=False,
                last_finished_at=finished_at,
                last_error=error,
            )
        job_name = job.get("name") if job else job_id
        # 收尾可能发生在事件循环上（__run_coro_job），使用异步进度后端避免阻塞
        progress = AsyncProgressHelper(self._get_progress_key(job_id))
        try:
            current_progress = await progress.get() or {}
            progress_value = 100 if success else current_progress.get("value", 0)
            await progress.end(
                text=f"{job_name} {'执行完成' if success else '执行失败'}",
                data={
                    "id": job_id,
                    "_generation": generation,
                    "name": job_name,
                    "provider": job.get("provider_name", "[系统]") if job else None,
                    "status": "success" if success else "failed",
                    "success": success,
                    "finished_at": finished_at,
                    "error": error,
                },
                value=progress_value,
            )
        finally:
            with self._lock:
                self._release_job_generation(job_id, generation)

    def _read_job_snapshot(self, job_id: str) -> Tuple[Optional[dict], dict]:
        """
        读取作业状态快照，避免进度查询期间持锁访问外部后端。

        :param job_id: 作业标识
        :return: 作业运行状态与展示所需的快照字段
        """
        with self._lock:
            job = self._jobs.get(job_id)
            return job, {
                "job_name": job.get("name") if job else job_id,
                "provider_name": job.get("provider_name", "[系统]") if job else None,
                "running": bool(
                    job and (self._is_job_active(job_id) or job.get("running"))
                ),
                "last_started_at": job.get("last_started_at") if job else None,
                "last_finished_at": job.get("last_finished_at") if job else None,
                "last_error": job.get("last_error") if job else None,
            }

    @staticmethod
    def _build_progress(
            job_id: str,
            job: Optional[dict],
            snapshot: dict,
            detail: dict,
    ) -> _SchemaScheduleProgress:
        """
        把作业快照与进度明细合成对外的进度视图。

        :param job_id: 作业标识
        :param job: 作业运行状态
        :param snapshot: 作业状态快照
        :param detail: 进度后端明细
        :return: 进度视图
        """
        data = dict(detail.get("data") or {})
        progress_generation = data.pop("_generation", None)
        if (
                job
                and progress_generation is not None
                and progress_generation != job.get("_generation", 0)
        ):
            # 缓存里的是旧代次进度，新代次不继承其数值与文案
            detail = {}
            data = {}
        value = detail.get("value", 0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        running = snapshot["running"]
        return _SchemaScheduleProgress(
            id=job_id,
            name=data.get("name") or snapshot["job_name"],
            provider=data.get("provider") or snapshot["provider_name"],
            enable=bool(detail.get("enable", running)),
            value=max(min(value, 100), 0),
            text=detail.get("text"),
            status=data.get("status") or ("running" if running else "waiting"),
            success=data.get("success"),
            started_at=data.get("started_at") or snapshot["last_started_at"],
            finished_at=data.get("finished_at") or snapshot["last_finished_at"],
            error=data.get("error") or snapshot["last_error"],
            data=data,
        )

    def get_progress(self, job_id: str) -> Optional[_SchemaScheduleProgress]:
        """
        查询指定定时服务的执行进度。
        """
        if not job_id:
            return None
        job, snapshot = self._read_job_snapshot(job_id)
        detail = ProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        return self._build_progress(job_id, job, snapshot, detail)

    async def aget_progress(self, job_id: str) -> Optional[_SchemaScheduleProgress]:
        """
        查询指定定时服务的执行进度（异步版本，供事件循环上的端点使用）。
        """
        if not job_id:
            return None
        job, snapshot = self._read_job_snapshot(job_id)
        # 异步后端读取，避免在事件循环上阻塞
        detail = await AsyncProgressHelper(self._get_progress_key(job_id)).get() or {}
        if not job and not detail:
            return None
        return self._build_progress(job_id, job, snapshot, detail)

    def notify_job_failure(self, title: str, message: str) -> None:
        """
        投递作业失败提示。

        引擎只负责日志与系统错误事件，是否再向用户提示由宿主决定，默认不投递。

        :param title: 提示标题
        :param message: 提示正文
        """

    def __handle_job_error(self, job_id: str, job: dict, error: Exception) -> None:
        """
        记录定时任务执行异常并发送系统错误事件。
        """
        logger.error(
            f"定时任务 {job.get('name')} 执行失败：{str(error)} - {traceback.format_exc()}"
        )
        self.notify_job_failure(
            title=f"{job.get('name')} 执行失败", message=str(error)
        )
        eventmanager.send_event(
            EventType.SystemError,
            {
                "type": "scheduler",
                "scheduler_id": job_id,
                "scheduler_name": job.get("name"),
                "error": str(error),
                "traceback": traceback.format_exc(),
            },
        )

    def __build_progress_callback(self, job_id: str, job: dict) -> Callable[..., None]:
        """
        构建传递给定时任务内部的进度更新回调。
        """
        generation = job.get("_generation", 0)

        def update_progress(
                value: Optional[float] = None,
                text: Optional[str] = None,
                data: Optional[dict] = None,
        ) -> None:
            """
            更新当前定时任务进度。
            """
            progress_data = {
                "id": job_id,
                "_generation": generation,
                "name": job.get("name"),
                "provider": job.get("provider_name", "[系统]"),
                "status": "running",
                "success": None,
            }
            if data:
                progress_data.update(data)
            key = self._get_progress_key(job_id)

            async def _update() -> None:
                """在事件循环上写入进度，旧代次的延迟回调直接丢弃。"""
                with self._lock:
                    current_job = self._jobs.get(job_id)
                    if (
                            current_job is not job
                            or current_job.get("_generation", 0) != generation
                    ):
                        return
                # 异步后端更新，避免任务函数在事件循环内调用回调时阻塞
                await AsyncProgressHelper(key).update(
                    value=value,
                    text=text,
                    data=progress_data,
                )

            # 回调可能在事件循环内（async 任务）或线程池中（sync 任务）被调用，
            # 统一经事件循环提交；无运行中循环时同步执行兜底
            self._submit_to_loop(
                _update(),
                job_id=job_id,
                generation=generation,
            )

        return update_progress

    @staticmethod
    def __supports_progress_callback(func: Callable[..., Any]) -> bool:
        """
        判断定时任务函数是否显式支持进度回调参数。
        """
        try:
            parameters = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return False
        return "progress_callback" in parameters

    @staticmethod
    def __get_result_error(result: Any) -> Optional[str]:
        """
        从定时任务标准失败返回值中提取错误信息。
        """
        if (
                isinstance(result, tuple)
                and result
                and isinstance(result[0], bool)
                and result[0] is False
        ):
            return str(result[1]) if len(result) > 1 and result[1] else "定时任务返回失败"
        return None

    async def __run_coro_job(
            self,
            coro_factory: Callable[[], Any],
            job_id: str,
            job: dict,
            generation: Optional[int] = None,
    ) -> None:
        """
        在当前事件循环内执行协程定时任务并在真实完成后收敛状态。
        """
        generation = job.get("_generation", 0) if generation is None else generation
        success = True
        error = None
        try:
            result = await coro_factory()
            error = self.__get_result_error(result)
            success = error is None
        except asyncio.CancelledError:
            success = False
            error = "任务已取消"
            raise
        except Exception as err:
            success = False
            error = str(err)
            self.__handle_job_error(job_id=job_id, job=job, error=err)
        finally:
            # 协程收尾在事件循环上完成，同步路径（线程池/调用线程）提交到事件循环执行
            await self.__finish_job(
                job_id=job_id,
                job=job,
                generation=generation,
                success=success,
                error=error,
            )

    @staticmethod
    def _resolve_target_loop() -> Tuple[
        Optional[asyncio.AbstractEventLoop],
        Optional[asyncio.AbstractEventLoop],
    ]:
        """
        解析当前调用环境可用的事件循环。

        :return: 调用方运行中的循环与应用登记的主循环，不可用时为 None
        """
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        target_loop = global_vars.CURRENT_EVENT_LOOP
        if target_loop is None or not target_loop.is_running() or target_loop.is_closed():
            target_loop = None
        return running_loop, target_loop

    def start(self, job_id: str, *args, **kwargs) -> bool:
        """
        启动定时服务

        :param job_id: 作业标识
        :return: 本次触发是否被接受
        """

        def __start_coro(
                coro_factory: Callable[[], Any],
                generation: int,
        ) -> Tuple[bool, bool]:
            """
            启动协程，返回是否异步收尾以及本次提交是否被接受。
            """
            running_loop, target_loop = self._resolve_target_loop()
            if running_loop and (target_loop is None or running_loop is target_loop):
                started = threading.Event()

                async def run_owned_job() -> None:
                    """在当前循环上执行作业协程并标记其已经开始。"""
                    started.set()
                    await self.__run_coro_job(
                        coro_factory=coro_factory,
                        job_id=job_id,
                        job=job,
                        generation=generation,
                    )

                with self._lock:
                    if not self._accepts_handle(job_id, generation):
                        return False, False
                    handle = running_loop.create_task(run_owned_job())
                    registered = self._register_handle(
                        job_id=job_id,
                        generation=generation,
                        loop=running_loop,
                        handle=handle,
                    )

                    def _finish_cancelled_before_start(
                            submitted: asyncio.Future,
                    ) -> None:
                        """任务未开始即被取消时同步释放运行所有权。"""
                        if submitted.cancelled() and not started.is_set():
                            self._finish_unsubmitted_job(
                                job_id=job_id,
                                job=job,
                                generation=generation,
                                error="任务未提交",
                            )

                    handle.add_done_callback(_finish_cancelled_before_start)
                    return registered, registered
            if target_loop is not None:
                wrapped = self.__run_coro_job(
                    coro_factory=coro_factory,
                    job_id=job_id,
                    job=job,
                    generation=generation,
                )
                submitted = self._submit_cross_thread(
                    wrapped,
                    target_loop=target_loop,
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=lambda: self._finish_unsubmitted_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        error="任务未提交",
                    ),
                )
                return submitted, submitted
            if self._lifecycle_state in _CLOSING_STATES:
                return False, False
            asyncio.run(coro_factory())
            return False, True

        # 获取定时任务
        job = self.__prepare_job(job_id)
        if not job:
            return False
        generation = job.get("_generation", 0)
        success = True
        error = None
        deferred_finish = False
        accepted = True
        # 开始运行
        try:
            if not kwargs:
                kwargs = dict(job.get("kwargs") or {})
            func = job.get("func")
            if not func:
                return False
            if self.__supports_progress_callback(func) and "progress_callback" not in kwargs:
                kwargs["progress_callback"] = self.__build_progress_callback(
                    job_id=job_id, job=job
                )
            # 是否多进程运行
            run_in_process = job.get("run_in_process", False)
            if inspect.iscoroutinefunction(func):
                # 协程函数：业务协程延迟到真正执行时才创建，提交被拒时不会残留未等待协程
                deferred_finish, accepted = __start_coro(
                    lambda: func(*args, **kwargs), generation
                )
            elif run_in_process:
                # 多进程运行
                p = multiprocessing.Process(target=func, args=args, kwargs=kwargs)
                p.start()
                p.join()
            else:
                # 普通函数
                result = func(*args, **kwargs)
                error = self.__get_result_error(result)
                success = error is None
        except Exception as e:
            success = False
            error = str(e)
            self.__handle_job_error(job_id=job_id, job=job, error=e)
        finally:
            if not deferred_finish:
                def finish_without_loop() -> None:
                    """收尾协程无法投递时就地写入终态并释放所有权。"""
                    self._finish_unsubmitted_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        error=error if accepted else "任务未提交",
                    )

                # 同步上下文执行异步收尾：优先提交到当前/全局事件循环，无循环时新建循环
                finish_submitted = self._submit_to_loop(
                    self.__finish_job(
                        job_id=job_id,
                        job=job,
                        generation=generation,
                        success=success,
                        error=error,
                    ),
                    job_id=job_id,
                    generation=generation,
                    on_unstarted_cancel=finish_without_loop,
                )
                if not finish_submitted:
                    finish_without_loop()
        return accepted

    def _submit_to_loop(
            self,
            coro: Any,
            *,
            job_id: Optional[str] = None,
            generation: int = 0,
            on_unstarted_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        把协程提交到事件循环执行，兼容以下调用环境：
        - 应用主循环可用：统一由主循环拥有任务与关闭顺序
        - 仅调用方循环可用：在当前循环排队为独立任务，避免阻塞
        - 无运行中循环（测试/CLI）：新建循环同步执行，确保进度不丢失

        带有作业标识的句柄由引擎自己持有，关停时可以取消并等待。

        :param coro: 待执行协程
        :param job_id: 归属作业标识，缺省表示不纳入关停收口
        :param generation: 归属作业代次
        :param on_unstarted_cancel: 协程未开始即被取消时的回调
        :return: 是否提交成功
        """
        running_loop, target_loop = self._resolve_target_loop()
        if running_loop and (target_loop is None or running_loop is target_loop):
            if job_id is None:
                running_loop.create_task(coro)
                return True
            with self._lock:
                if not self._accepts_handle(job_id, generation):
                    coro.close()
                    return False
                handle = running_loop.create_task(coro)
                registered = self._register_handle(
                    job_id=job_id,
                    generation=generation,
                    loop=running_loop,
                    handle=handle,
                )
                if on_unstarted_cancel:
                    handle.add_done_callback(
                        lambda submitted: (
                            on_unstarted_cancel() if submitted.cancelled() else None
                        )
                    )
                return registered
        if target_loop is not None:
            if job_id is None:
                asyncio.run_coroutine_threadsafe(coro, target_loop)
                return True
            return self._submit_cross_thread(
                coro,
                target_loop=target_loop,
                job_id=job_id,
                generation=generation,
                on_unstarted_cancel=on_unstarted_cancel,
            )
        if self._lifecycle_state in _CLOSING_STATES:
            coro.close()
            return False
        asyncio.run(coro)
        return True

    def list(self) -> List[_SchemaScheduleInfo]:
        """
        当前所有任务
        """
        if not self._scheduler:
            return []
        with self._lock:
            # 返回计时任务
            schedulers = []
            # 去重
            added = []
            # 避免_scheduler.shutdown()处于阻塞状态导致的死锁
            if not self._scheduler or not self._scheduler.running:
                return []
            jobs = self._scheduler.get_jobs()
            # 按照下次运行时间排序
            jobs.sort(key=lambda x: x.next_run_time)
            # 将正在运行的任务提取出来 (保障一次性任务正常显示)
            for job_id, service in self._jobs.items():
                name = service.get("name")
                provider_name = service.get("provider_name")
                if (
                        (self._is_job_active(job_id) or service.get("running"))
                        and name
                        and provider_name
                ):
                    if job_id not in added:
                        added.append(job_id)
                    progress = self.get_progress(job_id)
                    schedulers.append(
                        _SchemaScheduleInfo(
                            id=job_id,
                            name=name,
                            provider=provider_name,
                            status="正在运行",
                            progress=progress.value if progress else 0,
                            progress_text=progress.text if progress else None,
                            progress_enable=progress.enable if progress else False,
                            progress_detail=progress,
                        )
                    )
            # 获取其他待执行任务
            for job in jobs:
                job_id = job.id.split("|")[0]
                if job_id not in added:
                    added.append(job_id)
                else:
                    continue
                service = self._jobs.get(job_id)
                if not service:
                    continue
                # 任务状态
                status = (
                    "正在运行"
                    if self._is_job_active(job_id) or service.get("running")
                    else "等待"
                )
                # 下次运行时间
                next_run = TimerUtils.time_difference(job.next_run_time)
                progress = self.get_progress(job_id)
                schedulers.append(
                    _SchemaScheduleInfo(
                        id=job_id,
                        name=job.name,
                        provider=service.get("provider_name", "[系统]"),
                        status=status,
                        next_run=next_run,
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            # 仅手动执行的任务（未注册到调度器）
            for job_id, service in self._jobs.items():
                if not service.get("manual"):
                    continue
                if job_id in added:
                    continue
                added.append(job_id)
                progress = self.get_progress(job_id)
                schedulers.append(
                    _SchemaScheduleInfo(
                        id=job_id,
                        name=service.get("name"),
                        provider=service.get("provider_name", "[系统]"),
                        status="正在运行" if self._is_job_active(job_id) else "等待",
                        progress=progress.value if progress else 0,
                        progress_text=progress.text if progress else None,
                        progress_enable=progress.enable if progress else False,
                        progress_detail=progress,
                    )
                )
            return schedulers

    def _begin_stop(self) -> Tuple[Any, Tuple[SchedulerHandle, ...]]:
        """
        关闭提交入口并摘出当前调度器与其拥有的异步句柄。

        :return: 被摘出的后台调度器与关停快照中的执行登记
        """
        with self._lock:
            self._lifecycle_state = "stopping"
            self._event.set()
            scheduler = self._scheduler
            self._scheduler = None
            self._agent_task_reservations.clear()
            handles = tuple(self._handles.values())
        if scheduler:
            logger.info("正在停止定时任务...")
            try:
                scheduler.remove_all_jobs()
            except Exception as err:
                logger.error(f"移除定时任务失败：{str(err)}")
        return scheduler, handles

    def _begin_reload(self) -> Tuple[bool, Any]:
        """
        停止旧计划的提交入口，保留已开始任务直到其自然完成。

        :return: 本次调用是否发起重载，以及被摘出的后台调度器
        """
        with self._lock:
            if (
                    global_vars.is_system_stopped
                    or self._lifecycle_state in {"stopping", "reloading"}
            ):
                return False, None
            self._lifecycle_state = "reloading"
            self._event.set()
            scheduler = self._scheduler
            self._scheduler = None
            self._agent_task_reservations.clear()
        if scheduler:
            try:
                scheduler.remove_all_jobs()
            except Exception as err:
                logger.error(f"移除定时任务失败：{str(err)}")
        return True, scheduler

    @staticmethod
    def _shutdown_scheduler_sync(scheduler: Any) -> None:
        """
        等待后台调度器自有线程池停止。

        :param scheduler: 被摘出的后台调度器
        """
        if scheduler and scheduler.running:
            scheduler.shutdown()

    def stop(self) -> None:
        """
        关闭定时服务的同步兼容入口。

        应用生命周期使用 ``stop_async``，以便等待事件循环中的协程句柄；同步调用方
        仍可请求取消并等待后台调度器自有线程池收口。
        """
        with lock:
            try:
                scheduler, handles = self._begin_stop()
                for handle in handles:
                    self._cancel_handle(handle)
                self._shutdown_scheduler_sync(scheduler)
                with self._lock:
                    self._lifecycle_state = "stopped"
                logger.info("定时任务停止完成")
            except Exception as e:
                logger.error(f"停止定时任务失败：{str(e)} - {traceback.format_exc()}")

    async def stop_async(self) -> None:
        """
        关闭定时服务并等待已投递协程收口。
        """
        scheduler, handles = self._begin_stop()
        for handle in handles:
            self._cancel_handle(handle)
        await asyncio.to_thread(self._shutdown_scheduler_sync, scheduler)
        await self._await_cancelled_handles(handles)
        with self._lock:
            self._lifecycle_state = "stopped"
        logger.info("定时任务停止完成")
