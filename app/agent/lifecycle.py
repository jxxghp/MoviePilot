"""Agent 管理器启动、关闭与空闲回收生命周期。"""

import asyncio

from app.agent.session import AgentManagerUnavailableError, AgentSessionOwner
from app.application.agent import AgentDataContext
from app.runtime.log import logger


class AgentLifecycleOwner(AgentSessionOwner):
    """管理 Agent 服务代际、接收门禁与有界关闭。"""

    def configure_data_context(self, data: AgentDataContext) -> None:
        """在服务启动前绑定唯一数据上下文并配置记忆服务。"""
        if self._data is data:
            return
        if self._accepting_tasks or self.active_agents:
            raise RuntimeError("AgentManager 已运行，不能替换数据上下文")
        self._data = data
        self._memory.configure(data.chat, data.chat_persistence)

    async def initialize(self) -> None:
        """
        初始化管理器
        """
        async with self._lifecycle_lock:
            if self._accepting_tasks:
                return
            if self._close_finalizer_task and not self._close_finalizer_task.done():
                raise AgentManagerUnavailableError("AgentManager 仍在完成上一代关闭")
            self._memory.initialize()
            if not self._idle_cleanup_task or self._idle_cleanup_task.done():
                self._idle_cleanup_task = asyncio.create_task(
                    self._cleanup_idle_sessions()
                )
            self._accepting_tasks = True
            self._closed = False

    async def close(self) -> bool:
        """
        关闭管理器，并诚实返回全部会话 owner 是否已经收敛。
        """
        async with self._lifecycle_lock:
            if self._closed:
                return True
            if self._close_finalizer_task and not self._close_finalizer_task.done():
                return False
            # 门禁必须先关闭；锁内完成清理可阻止等待中的请求在收口期间重新入队。
            self._accepting_tasks = False
            # 子代理提交门禁同样必须在第一次 await 前关闭，否则 detached task
            # 可趁 idle-cleanup 收尾窗口继续创建不属于新生命周期的任务。
            for agent in self.active_agents.values():
                begin_shutdown = getattr(agent, "begin_shutdown", None)
                if callable(begin_shutdown):
                    begin_shutdown()
            if self._idle_cleanup_task:
                self._idle_cleanup_task.cancel()
                try:
                    await self._idle_cleanup_task
                except asyncio.CancelledError:
                    pass
                self._idle_cleanup_task = None
            # 先取消所有 worker，再以有限等待收口，避免关闭阶段无限挂起。
            workers = list(self._session_workers.items())
            for session_id, task in workers:
                self._session_cancel_requested.add(session_id)
                task.cancel()
            timed_out_workers = []
            for session_id, task in workers:
                stopped = await self._wait_for_worker_shutdown(
                    session_id,
                    task,
                    reason="manager_close",
                )
                if not stopped:
                    timed_out_workers.append((session_id, task))
            for queue in list(self._session_queues.values()):
                self._discard_queued_messages(
                    queue,
                    error=AgentManagerUnavailableError("AgentManager 已关闭"),
                )
            self._session_queues.clear()
            self._session_last_used.clear()
            self._session_queue_rejections.clear()
            self._session_last_queue_wait_ms.clear()

            if timed_out_workers:
                timed_out_session_ids = {
                    session_id for session_id, _ in timed_out_workers
                }
                for session_id, task in workers:
                    if session_id in timed_out_session_ids:
                        continue
                    if self._session_workers.get(session_id) is task:
                        self._session_workers.pop(session_id, None)
                for session_id, agent in list(self.active_agents.items()):
                    if session_id not in timed_out_session_ids:
                        if await agent.cleanup() is not False:
                            self.active_agents.pop(session_id, None)
                logger.error(
                    "AgentManager 关闭时仍有 worker 未收敛，"
                    f"保留 {len(timed_out_workers)} 个会话资源直到 worker 结束"
                )
                self._close_finalizer_task = asyncio.create_task(
                    self._finish_deferred_close(timed_out_workers)
                )
                return False

            self._session_workers.clear()
            for session_id, agent in list(self.active_agents.items()):
                if await agent.cleanup() is not False:
                    self.active_agents.pop(session_id, None)
            if self.active_agents:
                logger.error(
                    "AgentManager 仍有 %d 个 detached 子代理 owner 未收敛",
                    len(self.active_agents),
                )
                return False
            await self._memory.close()
            self._closed = True
            return True

    async def _cleanup_idle_sessions(self) -> None:
        """
        周期性清理长时间没有新消息的 Agent 会话，避免长期运行后实例持续累积。
        """
        while True:
            try:
                await asyncio.sleep(self._idle_cleanup_interval)
                for session_id, user_id in self._expired_idle_sessions():
                    await self.clear_session(session_id=session_id, user_id=user_id)
                    logger.info(f"已清理空闲Agent会话: session_id={session_id}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理空闲Agent会话失败: {e}")

    async def _finish_deferred_close(
            self,
            workers: list[tuple[str, asyncio.Task[None]]],
    ) -> None:
        """关闭超时后等待遗留 worker，再释放共享 Agent 资源。"""
        try:
            await asyncio.gather(
                *(worker for _, worker in workers),
                return_exceptions=True,
            )
            async with self._lifecycle_lock:
                for session_id, worker in workers:
                    if self._session_workers.get(session_id) is worker:
                        self._session_workers.pop(session_id, None)
                for session_id, agent in list(self.active_agents.items()):
                    if await agent.cleanup() is not False:
                        self.active_agents.pop(session_id, None)
                self._session_shutdown_pending.clear()
                self._session_cancel_requested.clear()
                if self.active_agents:
                    logger.error(
                        "AgentManager 延迟关闭后仍有 %d 个子代理 owner 未收敛",
                        len(self.active_agents),
                    )
                    return
                await self._memory.close()
                self._closed = True
        finally:
            self._close_finalizer_task = None
