"""Agent 会话队列、worker 与资源状态的唯一 owner。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, cast

from app.agent.contracts import ReplyMode
from app.agent.memory import MemoryManager, memory_manager
from app.agent.orchestrator import MoviePilotAgent, _SessionUsageSnapshot
from app.application.agent import AgentDataContext
from app.chain.agent import AgentChain
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.observability import record_metric
from app.runtime.settings import get_runtime_setting
from app.schemas.types import NotificationChannel


def _agent_task_metric_type(source: object, channel: object = None) -> str:
    """把 Agent 来源归一为交互、调度、后台三类稳定标签。"""
    normalized = str(source or "").strip().lower()
    if normalized in {"scheduler", "scheduled", "heartbeat", "agent_task"}:
        return "scheduled"
    if channel:
        return "interactive"
    return "background"


def _finish_processing_status(
    status: Optional[dict[str, Any]],
    user_id: Optional[str] = None,
) -> None:
    """结束入站消息的渠道处理状态。"""
    if not status:
        return
    AgentChain().finish_message_processing_status(
        status=status,
        userid=user_id,
    )


async def _async_start_processing_status(
    task: "_MessageTask",
) -> Optional[dict[str, Any]]:
    """
    在 Agent worker 中启动渠道处理状态。
    渠道启动可能触发外部 API，同步实现需切到线程池避免阻塞事件循环。
    """
    if not task.channel:
        return None

    def _start() -> Optional[dict[str, Any]]:
        """在线程池中通过统一 Chain 接口启动处理状态。"""
        try:
            return cast(
                Optional[dict[str, Any]],
                AgentChain().start_message_processing_status(
                channel=NotificationChannel(task.channel),
                source=task.source,
                userid=task.user_id,
                message_id=task.original_message_id,
                chat_id=task.original_chat_id,
                text=task.message,
                ),
            )
        except Exception as err:
            logger.debug(f"启动Agent消息处理状态失败: {err}")
            return None

    return cast(Optional[dict[str, Any]], await run_in_threadpool(_start))


async def _async_finish_processing_status(
        status: Optional[dict[str, Any]], user_id: Optional[str] = None
) -> None:
    """
    在 Agent worker 中结束渠道处理状态。
    渠道收口可能触发外部 API，同步实现需切到线程池避免阻塞事件循环。
    """
    if not status:
        return
    await run_in_threadpool(_finish_processing_status, status, user_id)



@dataclass
class _MessageTask:
    """
    待处理的消息任务
    """

    session_id: str
    user_id: str
    message: str
    images: Optional[List[str]] = None
    files: Optional[List[dict[str, Any]]] = None
    has_audio_input: bool = False
    channel: Optional[str] = None
    source: Optional[str] = None
    username: Optional[str] = None
    is_channel_admin: Optional[bool] = None
    original_message_id: Optional[str] = None
    original_chat_id: Optional[str] = None
    processing_status: Optional[dict[str, Any]] = None
    reply_mode: ReplyMode = ReplyMode.DISPATCH
    allow_message_tools: bool = True
    output_callback: Optional[Callable[[str], None]] = None
    protected_output_callback: Optional[Callable[[str], Optional[bool]]] = None
    message_callback: Optional[Callable[[Any], Awaitable[None] | None]] = None
    agent_factory: Optional[Callable[..., MoviePilotAgent]] = None
    agent_setup: Optional[Callable[[MoviePilotAgent], None]] = None
    completion_future: Optional[asyncio.Future[str]] = None
    enqueued_at: Optional[float] = None


class AgentManagerUnavailableError(RuntimeError):
    """AgentManager 未运行或已开始关闭，不能再接收新任务。"""

    code = "agent_manager_unavailable"


class AgentManagerQueueFullError(RuntimeError):
    """Agent 会话的待处理消息达到容量上限。"""

    code = "agent_manager_queue_full"

    def __init__(self, session_id: str, limit: int) -> None:
        self.session_id = session_id
        self.limit = limit
        super().__init__(
            f"Agent 会话当前排队消息已达上限（{limit} 条），请稍后重试"
        )


AGENT_SESSION_QUEUE_MAX_SIZE = 8
AGENT_MANAGER_SHUTDOWN_TIMEOUT = 10.0


class AgentSessionOwner:
    """管理 Agent 会话队列、worker、活动实例与清理状态。"""

    def __init__(
        self,
        data: Optional[AgentDataContext] = None,
        memory: Optional[MemoryManager] = None,
    ) -> None:
        """创建会话 owner，并保存组合根注入的 Agent 数据与记忆能力。"""
        self._data = data
        self._memory = memory or memory_manager
        if data is not None:
            self._memory.configure(data.chat, data.chat_persistence)
        self.active_agents: Dict[str, MoviePilotAgent] = {}
        # 每个会话的消息队列
        self._session_queues: Dict[str, asyncio.Queue[_MessageTask]] = {}
        # 每个会话的worker任务
        self._session_workers: Dict[str, asyncio.Task[None]] = {}
        # 每个会话最后活动时间，用于回收空闲 Agent 实例
        self._session_last_used: Dict[str, tuple[str, datetime]] = {}
        self._idle_cleanup_task: Optional[asyncio.Task[None]] = None
        self._idle_session_ttl = timedelta(hours=24)
        self._idle_cleanup_interval = 60 * 60
        self._session_queue_rejections: Dict[str, int] = {}
        self._session_last_queue_wait_ms: Dict[str, float] = {}
        self._session_shutdown_pending: Dict[str, asyncio.Task[None]] = {}
        self._session_cleanup_pending: set[str] = set()
        self._session_deferred_cleanup_tasks: Dict[str, asyncio.Task[None]] = {}
        self._session_cancel_requested: set[str] = set()
        self._close_finalizer_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._shutdown_timeout = AGENT_MANAGER_SHUTDOWN_TIMEOUT
        # 接收门禁与队列写入共用一把锁，确保关闭开始后不会再创建 worker。
        self._lifecycle_lock = asyncio.Lock()
        self._accepting_tasks = False

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """获取会话当前模型与 token 使用状态。"""
        agent = self.active_agents.get(session_id)
        if agent:
            status = agent.get_session_status()
        else:
            status = _SessionUsageSnapshot(
                model=get_runtime_setting('LLM_MODEL'),
                context_window_tokens=(
                    get_runtime_setting('LLM_MAX_CONTEXT_TOKENS') * 1000
                    if get_runtime_setting('LLM_MAX_CONTEXT_TOKENS')
                    else None
                ),
            ).to_dict(session_id)

        queue = self._session_queues.get(session_id)
        status["pending_messages"] = queue.qsize() if queue else 0
        status["queue_capacity"] = AGENT_SESSION_QUEUE_MAX_SIZE
        status["queue_saturated"] = bool(queue and queue.full())
        status["queue_rejections"] = self._session_queue_rejections.get(
            session_id,
            0,
        )
        status["last_queue_wait_ms"] = self._session_last_queue_wait_ms.get(
            session_id,
            0.0,
        )
        pending_shutdown = self._session_shutdown_pending.get(session_id)
        status["shutdown_pending"] = bool(
            pending_shutdown and not pending_shutdown.done()
        )
        status["is_processing"] = (
                session_id in self._session_workers
                and not self._session_workers[session_id].done()
        )
        return status

    def matches_secret_confirmation(
            self,
            session_id: str,
            user_id: str,
            channel: Optional[str] = None,
            source: Optional[str] = None,
    ) -> bool:
        """判断指定用户是否可继续当前会话的敏感设置确认。"""
        agent = self.active_agents.get(session_id)
        pending = agent._pending_secret_confirmation if agent else None
        return bool(
            agent
            and pending
            and str(agent.user_id) == str(user_id)
            and (channel is None or pending.channel == str(channel))
            and (source is None or pending.source == str(source))
        )

    def _record_session_activity(self, session_id: str, user_id: str) -> None:
        """
        记录会话最近活动时间，供空闲会话清理任务判断是否可释放资源。
        """
        self._session_last_used[session_id] = (user_id, datetime.now())

    def _is_session_busy(self, session_id: str) -> bool:
        """
        判断会话是否仍有正在执行的 worker 或待处理消息，避免误清理活跃会话。
        """
        worker = self._session_workers.get(session_id)
        if worker and not worker.done():
            return True
        queue = self._session_queues.get(session_id)
        return bool(queue and not queue.empty())

    def is_session_busy(self, session_id: str) -> bool:
        """
        查询会话是否仍有正在执行或排队的任务。
        """
        return self._is_session_busy(session_id)

    def _expired_idle_sessions(self) -> list[tuple[str, str]]:
        """
        收集已经超过空闲时间且当前不忙的会话。
        """
        expire_before = datetime.now() - self._idle_session_ttl
        expired = []
        for session_id, (user_id, last_used) in list(self._session_last_used.items()):
            if last_used < expire_before and not self._is_session_busy(session_id):
                expired.append((session_id, user_id))
        return expired

    async def process_message(
            self,
            session_id: str,
            user_id: str,
            message: str,
            images: Optional[List[str]] = None,
            files: Optional[List[dict[str, Any]]] = None,
            has_audio_input: bool = False,
            channel: Optional[str] = None,
            source: Optional[str] = None,
            username: Optional[str] = None,
            is_channel_admin: Optional[bool] = None,
            original_message_id: Optional[str] = None,
            original_chat_id: Optional[str] = None,
            reply_mode: ReplyMode = ReplyMode.DISPATCH,
            allow_message_tools: bool = True,
            output_callback: Optional[Callable[[str], None]] = None,
            protected_output_callback: Optional[Callable[[str], Optional[bool]]] = None,
            message_callback: Optional[Callable[[Any], Awaitable[None] | None]] = None,
            agent_factory: Optional[Callable[..., MoviePilotAgent]] = None,
            agent_setup: Optional[Callable[[MoviePilotAgent], None]] = None,
            wait_for_completion: bool = False,
    ) -> str:
        """
        处理用户消息：将消息放入会话队列，按顺序依次处理。
        同一会话的消息排队等待，不同会话之间互不影响。
        """
        completion_future: Optional[asyncio.Future[str]] = (
            asyncio.get_running_loop().create_future() if wait_for_completion else None
        )
        task = _MessageTask(
            session_id=session_id,
            user_id=user_id,
            message=message,
            images=images,
            files=files,
            has_audio_input=has_audio_input,
            channel=channel,
            source=source,
            username=username,
            is_channel_admin=is_channel_admin,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            reply_mode=reply_mode,
            allow_message_tools=allow_message_tools,
            output_callback=output_callback,
            protected_output_callback=protected_output_callback,
            message_callback=message_callback,
            agent_factory=agent_factory,
            agent_setup=agent_setup,
            completion_future=completion_future,
        )
        async with self._lifecycle_lock:
            if not self._accepting_tasks:
                if completion_future and not completion_future.done():
                    completion_future.cancel()
                raise AgentManagerUnavailableError("AgentManager 未运行或已关闭")
            pending_shutdown = self._session_shutdown_pending.get(session_id)
            if pending_shutdown:
                if pending_shutdown.done():
                    self._session_shutdown_pending.pop(session_id, None)
                else:
                    if completion_future and not completion_future.done():
                        completion_future.cancel()
                    raise AgentManagerUnavailableError(
                        f"Agent 会话 {session_id} 仍在停止，暂时不能接收新任务"
                    )
            self._record_session_activity(session_id, user_id)

            # 获取或创建会话队列
            if session_id not in self._session_queues:
                self._session_queues[session_id] = asyncio.Queue(
                    maxsize=AGENT_SESSION_QUEUE_MAX_SIZE
                )

            queue = self._session_queues[session_id]
            queue_size = queue.qsize()

            if queue.full():
                self._session_queue_rejections[session_id] = (
                    self._session_queue_rejections.get(session_id, 0) + 1
                )
                logger.warning(
                    f"会话 {session_id} 的 Agent 排队已满，拒绝新消息 "
                    f"(上限: {AGENT_SESSION_QUEUE_MAX_SIZE})"
                )
                if completion_future and not completion_future.done():
                    completion_future.cancel()
                raise AgentManagerQueueFullError(
                    session_id=session_id,
                    limit=AGENT_SESSION_QUEUE_MAX_SIZE,
                )

            # 如果队列中已有等待的消息，通知用户消息已排队
            if queue_size > 0 or (
                    session_id in self._session_workers
                    and not self._session_workers[session_id].done()
            ):
                logger.info(
                    f"会话 {session_id} 有任务正在处理，消息已排队等待 "
                    f"(队列中待处理: {queue_size} 条)"
                )

            # 非阻塞入队与 worker 创建在同一生命周期锁内完成，关闭期间不会留下悬挂入队。
            task.enqueued_at = asyncio.get_running_loop().time()
            queue.put_nowait(task)
            if (
                    session_id not in self._session_workers
                    or self._session_workers[session_id].done()
            ):
                self._session_workers[session_id] = asyncio.create_task(
                    self._session_worker(session_id)
                )

        if completion_future:
            return await completion_future
        return ""

    async def _session_worker(self, session_id: str) -> None:
        """
        会话消息处理worker：从队列中逐条取出消息并处理。
        处理完当前消息后才会处理下一条，确保同一会话的消息顺序执行。
        """
        queue = self._session_queues.get(session_id)
        if not queue:
            return

        try:
            while True:
                try:
                    # 等待消息，超时后自动退出worker
                    task = await asyncio.wait_for(queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    # 超时回调与入队可能在同一轮事件循环就绪；已有消息时继续消费，
                    # 避免旧 worker 退出后留下没有消费者的非空队列。
                    if not queue.empty():
                        continue
                    # 队列空闲超时，退出worker
                    logger.debug(f"会话 {session_id} 的消息队列空闲，worker退出")
                    break

                task_type = _agent_task_metric_type(task.source, task.channel)
                active_metric_recorded = False
                try:
                    if task.enqueued_at is not None:
                        queue_wait_ms = max(
                            0.0,
                            (
                                asyncio.get_running_loop().time()
                                - task.enqueued_at
                            )
                            * 1000,
                        )
                        self._session_last_queue_wait_ms[session_id] = round(
                            queue_wait_ms,
                            3,
                        )
                    await self._start_task_processing_status(task)
                    record_metric(
                        "agent.active_tasks",
                        1,
                        task_type=task_type,
                    )
                    active_metric_recorded = True
                    result = await self._process_message_internal(task)
                    if task.completion_future and not task.completion_future.done():
                        if (
                                not self._accepting_tasks
                                or session_id in self._session_cancel_requested
                        ):
                            task.completion_future.cancel()
                        else:
                            task.completion_future.set_result(result)
                except asyncio.CancelledError:
                    if task.completion_future and not task.completion_future.done():
                        if self._accepting_tasks:
                            task.completion_future.cancel()
                        else:
                            task.completion_future.set_exception(
                                AgentManagerUnavailableError("AgentManager 已关闭")
                            )
                    raise
                except Exception as e:
                    logger.error(f"处理会话 {session_id} 的消息失败: {e}")
                    if task.completion_future and not task.completion_future.done():
                        task.completion_future.set_exception(e)
                finally:
                    if active_metric_recorded:
                        record_metric(
                            "agent.active_tasks",
                            -1,
                            task_type=task_type,
                        )
                    await self._finish_task_processing_status(task)
                    queue.task_done()
                if session_id in self._session_cancel_requested:
                    break

        except asyncio.CancelledError:
            logger.info(f"会话 {session_id} 的worker被取消")
        finally:
            # 清理已完成的worker记录
            current_worker = asyncio.current_task()
            if self._session_workers.get(session_id) is current_worker:
                self._session_workers.pop(session_id, None)  # noqa
            self._session_cancel_requested.discard(session_id)
            # 如果队列为空，清理队列
            if (
                    self._session_queues.get(session_id) is queue
                    and queue.empty()
            ):
                self._session_queues.pop(session_id, None)

    @staticmethod
    def _discard_queued_messages(
            queue: asyncio.Queue[_MessageTask],
            error: Optional[Exception] = None,
    ) -> None:
        """丢弃会话队列时同步结束等待任务完成的调用方。"""
        while not queue.empty():
            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if task.completion_future and not task.completion_future.done():
                if error is None:
                    task.completion_future.cancel()
                else:
                    task.completion_future.set_exception(error)
            queue.task_done()

    @staticmethod
    async def _start_task_processing_status(task: _MessageTask) -> None:
        """
        在 Agent worker 真正开始处理消息时启动渠道处理状态。
        """
        if task.processing_status:
            return
        task.processing_status = await _async_start_processing_status(task)

    @staticmethod
    async def _finish_task_processing_status(task: _MessageTask) -> None:
        """
        在 Agent worker 完成或异常后结束本条消息的渠道处理状态。
        """
        await _async_finish_processing_status(task.processing_status, task.user_id)
        task.processing_status = None

    async def _process_message_internal(self, task: _MessageTask) -> str:
        """
        实际处理单条消息
        """
        session_id = task.session_id
        existing_agent = self.active_agents.get(session_id)
        if (
                existing_agent
                and task.agent_factory
                and isinstance(task.agent_factory, type)
                and not isinstance(existing_agent, task.agent_factory)
        ):
            if await existing_agent.cleanup() is False:
                raise AgentManagerUnavailableError(
                    f"Agent 会话 {session_id} 仍有子代理任务在停止"
                )
            self.active_agents.pop(session_id, None)

        if session_id not in self.active_agents:
            logger.info(
                f"创建新的AI智能体实例，session_id: {session_id}, user_id: {task.user_id}"
            )
            if task.agent_factory is None:
                agent = MoviePilotAgent(
                    session_id=session_id,
                    user_id=task.user_id,
                    channel=task.channel,
                    source=task.source,
                    username=task.username,
                    is_channel_admin=task.is_channel_admin,
                    original_message_id=task.original_message_id,
                    original_chat_id=task.original_chat_id,
                    replay_mode=task.reply_mode,
                    allow_message_tools=task.allow_message_tools,
                    output_callback=task.output_callback,
                    protected_output_callback=task.protected_output_callback,
                    data=self._data,
                    memory=self._memory,
                )
            else:
                agent_kwargs: dict[str, Any] = {
                    "session_id": session_id,
                    "user_id": task.user_id,
                    "channel": task.channel,
                    "source": task.source,
                    "username": task.username,
                    "is_channel_admin": task.is_channel_admin,
                    "original_message_id": task.original_message_id,
                    "original_chat_id": task.original_chat_id,
                    "replay_mode": task.reply_mode,
                    "allow_message_tools": task.allow_message_tools,
                    "output_callback": task.output_callback,
                    "protected_output_callback": task.protected_output_callback,
                }
                if task.message_callback is not None:
                    agent_kwargs["message_callback"] = task.message_callback
                agent = task.agent_factory(**agent_kwargs)
            self.active_agents[session_id] = agent
        else:
            agent = self.active_agents[session_id]
            agent.user_id = task.user_id
            # 每条队列任务都携带完整消息上下文，None 也必须覆盖，避免后台任务
            # 复用会话 Agent 时继续沿用上一条入站消息的渠道。
            agent.channel = task.channel
            agent.source = task.source
            agent.username = task.username
            agent.is_channel_admin = task.is_channel_admin
            agent.original_message_id = task.original_message_id
            agent.original_chat_id = task.original_chat_id
            agent.reply_mode = task.reply_mode
            agent.allow_message_tools = task.allow_message_tools
            if hasattr(agent, "set_output_callback"):
                agent.set_output_callback(task.output_callback)
            else:
                agent.output_callback = task.output_callback
            agent.set_protected_output_callback(task.protected_output_callback)
            if task.message_callback is not None and hasattr(agent, "set_message_callback"):
                agent.set_message_callback(task.message_callback)

        if task.agent_setup is not None:
            task.agent_setup(agent)

        process_kwargs: dict[str, Any] = {
            "images": task.images,
            "files": task.files,
        }
        if task.has_audio_input:
            process_kwargs["has_audio_input"] = True
        return await agent.process(task.message, **process_kwargs)

    async def stop_current_task(self, session_id: str) -> bool:
        """
        应急停止当前正在执行的Agent推理任务，但保留会话和记忆。
        与 clear_session 不同，此方法不会销毁Agent实例或清除记忆，
        用户可以在停止后继续对话。
        """
        async with self._lifecycle_lock:
            return await self._stop_current_task_locked(session_id)

    async def _stop_current_task_locked(self, session_id: str) -> bool:
        """在 lifecycle 互斥域内停止会话 worker。"""
        stopped = False
        active_agent = self.active_agents.get(session_id)
        task_type = (
            _agent_task_metric_type(
                getattr(active_agent, "source", None),
                getattr(active_agent, "channel", None),
            )
            if active_agent
            else "unknown"
        )

        worker = self._session_workers.get(session_id)
        queue = self._session_queues.get(session_id)
        if queue and self._session_queues.get(session_id) is queue:
            self._session_queues.pop(session_id, None)

        # 先摘下旧队列再等待 worker 退出；lifecycle 锁保证清理期间不会并发建立新队列。
        if worker:
            self._session_cancel_requested.add(session_id)
            worker.cancel()
        if queue:
            self._discard_queued_messages(queue)
        if worker:
            stopped_cleanly = await self._wait_for_worker_shutdown(
                session_id,
                worker,
                reason="stop_current_task",
            )
            if stopped_cleanly and self._session_workers.get(session_id) is worker:
                self._session_workers.pop(session_id, None)  # noqa
            stopped = True
        if queue:
            stopped = True

        new_queue = self._session_queues.get(session_id)
        current_worker = self._session_workers.get(session_id)
        if (
                new_queue
                and not new_queue.empty()
                and (not current_worker or current_worker.done())
        ):
            if session_id not in self._session_shutdown_pending:
                self._session_workers[session_id] = asyncio.create_task(
                    self._session_worker(session_id)
                )

        if stopped:
            logger.info(f"会话 {session_id} 的Agent推理已应急停止")
        else:
            logger.debug(f"会话 {session_id} 没有正在执行的Agent任务")

        record_metric(
            "agent.cancel",
            task_type=task_type,
            outcome="stopped" if stopped else "not_found",
        )

        return stopped

    async def clear_session(self, session_id: str, user_id: str) -> None:
        """
        清空会话
        """
        async with self._lifecycle_lock:
            await self._clear_session_locked(session_id=session_id, user_id=user_id)

    async def _clear_session_locked(self, session_id: str, user_id: str) -> None:
        """在 lifecycle 互斥域内释放会话、Agent 与记忆。"""
        if session_id in self._session_cleanup_pending:
            return
        self._session_last_used.pop(session_id, None)
        # 取消该会话的worker
        if session_id in self._session_workers:
            worker = self._session_workers[session_id]
            self._session_cleanup_pending.add(session_id)
            self._session_cancel_requested.add(session_id)
            worker.cancel()
            try:
                stopped_cleanly = await self._wait_for_worker_shutdown(
                    session_id,
                    worker,
                    reason="clear_session",
                )
            except asyncio.CancelledError:
                self._defer_session_cleanup(session_id, user_id, worker)
                raise
            if not stopped_cleanly:
                self._defer_session_cleanup(session_id, user_id, worker)
                return
            if self._session_workers.get(session_id) is worker:
                self._session_workers.pop(session_id, None)  # noqa
            self._session_cleanup_pending.discard(session_id)

        # 清理队列时同步结束未执行请求，避免 wait_for_completion 调用方永久等待。
        queue = self._session_queues.pop(session_id, None)
        if queue:
            self._discard_queued_messages(queue)
        self._session_queue_rejections.pop(session_id, None)
        self._session_last_queue_wait_ms.pop(session_id, None)

        # 清理agent
        if session_id in self.active_agents:
            agent = self.active_agents[session_id]
            if await agent.cleanup() is False:
                logger.error(
                    f"会话 {session_id} 仍有子代理 owner 未收敛，保留会话与记忆"
                )
                return
            del self.active_agents[session_id]
            self._memory.clear_memory(session_id, user_id)
            logger.info(f"会话 {session_id} 的记忆已清空")

    def _defer_session_cleanup(
            self,
            session_id: str,
            user_id: str,
            worker: asyncio.Task[None],
    ) -> None:
        """把中断或超时的清理转交给 worker 终态回调。"""
        queue = self._session_queues.pop(session_id, None)
        if queue:
            self._discard_queued_messages(queue)
        self._session_shutdown_pending[session_id] = worker
        worker.add_done_callback(
            lambda done: self._schedule_deferred_session_cleanup(
                session_id,
                user_id,
                done,
            )
        )

    def _schedule_deferred_session_cleanup(
            self,
            session_id: str,
            user_id: str,
            worker: asyncio.Task[None],
    ) -> None:
        """worker 取得终态后释放会话资源，避免与仍在运行的 Agent 竞态。"""
        if self._session_shutdown_pending.get(session_id) is worker:
            existing = self._session_deferred_cleanup_tasks.get(session_id)
            if existing is not None and not existing.done():
                return
            cleanup_task = asyncio.create_task(
                self._finish_deferred_session_cleanup(
                    session_id=session_id,
                    user_id=user_id,
                    worker=worker,
                )
            )
            self._session_deferred_cleanup_tasks[session_id] = cleanup_task

    async def _finish_deferred_session_cleanup(
            self,
            session_id: str,
            user_id: str,
            worker: asyncio.Task[None],
    ) -> None:
        """等待超时 worker 真正结束后，再完成 clear_session 的资源释放。"""
        try:
            await worker
        except BaseException:
            pass
        async with self._lifecycle_lock:
            if self._session_workers.get(session_id) is worker:
                self._session_workers.pop(session_id, None)
            self._session_shutdown_pending.pop(session_id, None)
            self._session_cleanup_pending.discard(session_id)
            self._session_deferred_cleanup_tasks.pop(session_id, None)
            self._session_queue_rejections.pop(session_id, None)
            self._session_last_queue_wait_ms.pop(session_id, None)
            agent = self.active_agents.get(session_id)
            if agent:
                if await agent.cleanup() is False:
                    logger.error(
                        f"会话 {session_id} 的延迟清理仍有子代理 owner 未收敛"
                    )
                    return
                self.active_agents.pop(session_id, None)
                self._memory.clear_memory(session_id, user_id)
                logger.info(f"会话 {session_id} 的记忆已清空")

    async def _wait_for_worker_shutdown(
            self,
            session_id: str,
            worker: asyncio.Task[None],
            *,
            reason: str,
    ) -> bool:
        """有限等待 worker 结束，超时会话保持停止态直到旧 worker 收敛。"""
        try:
            await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=self._shutdown_timeout,
            )
            return True
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if worker.cancelled() and (current is None or not current.cancelling()):
                return True
            raise
        except asyncio.TimeoutError:
            self._session_shutdown_pending[session_id] = worker

            def _clear_pending(done: asyncio.Task[None]) -> None:
                if (
                        self._session_shutdown_pending.get(session_id) is done
                        and session_id not in self._session_cleanup_pending
                ):
                    self._session_shutdown_pending.pop(session_id, None)
                    logger.info(
                        f"会话 {session_id} 的 Agent worker 已在超时后收敛"
                    )

            worker.add_done_callback(_clear_pending)
            logger.error(
                f"会话 {session_id} 的 Agent worker 关闭超时，"
                f"已阻止新任务进入，reason={reason}, timeout={self._shutdown_timeout:g}s"
            )
            return False
        except Exception as error:
            logger.error(
                f"等待会话 {session_id} 的 Agent worker 关闭失败，"
                f"reason={reason}: {error}"
            )
            return True
