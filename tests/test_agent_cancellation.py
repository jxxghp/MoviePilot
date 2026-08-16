import asyncio
from unittest.mock import AsyncMock

import pytest

from app.agent import AgentManager, MoviePilotAgent


def test_execute_agent_propagates_task_cancellation():
    """取消 Agent 执行时应终止调用方任务，不能转换成普通完成结果。"""
    started = asyncio.Event()

    class _BlockingAgent:
        """等待取消的最小 LangGraph 替身。"""

        async def ainvoke(self, _payload, config=None):  # noqa: ARG002
            """阻塞到外层任务取消。"""
            started.set()
            await asyncio.Event().wait()

    async def _run_scenario():
        agent = MoviePilotAgent(session_id="session-1", user_id="10001")
        agent._should_stream = lambda: False
        agent._create_agent = AsyncMock(return_value=_BlockingAgent())
        agent.stream_handler.stop_streaming = AsyncMock(return_value=(False, ""))

        execution = asyncio.create_task(agent._execute_agent([]))
        await asyncio.wait_for(started.wait(), timeout=1)
        execution.cancel()

        with pytest.raises(asyncio.CancelledError):
            await execution
        agent.stream_handler.stop_streaming.assert_awaited_once()

    asyncio.run(_run_scenario())


def test_stop_current_task_cancels_waiters_and_allows_next_message():
    """停止会话应结束当前及排队请求，并允许同一会话继续处理消息。"""

    async def _run_scenario():
        manager = AgentManager()
        await manager.initialize()
        started = asyncio.Event()

        async def _block_current_task(_task):
            started.set()
            await asyncio.Event().wait()

        manager._process_message_internal = _block_current_task
        first_waiter = asyncio.create_task(
            manager.process_message(
                session_id="session-1",
                user_id="10001",
                message="first",
                wait_for_completion=True,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        second_waiter = asyncio.create_task(
            manager.process_message(
                session_id="session-1",
                user_id="10001",
                message="second",
                wait_for_completion=True,
            )
        )
        await asyncio.sleep(0)

        try:
            assert await asyncio.wait_for(
                manager.stop_current_task("session-1"), timeout=1
            ) is True
            with pytest.raises(asyncio.CancelledError):
                await first_waiter
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(second_waiter, timeout=1)

            manager._process_message_internal = AsyncMock(return_value="resumed")
            result = await asyncio.wait_for(
                manager.process_message(
                    session_id="session-1",
                    user_id="10001",
                    message="next",
                    wait_for_completion=True,
                ),
                timeout=1,
            )
            assert result == "resumed"
        finally:
            await manager.stop_current_task("session-1")
            for waiter in (first_waiter, second_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                first_waiter,
                second_waiter,
                return_exceptions=True,
            )
            await manager.close()

    asyncio.run(_run_scenario())


def test_stop_queues_new_message_until_cancellation_cleanup_finishes():
    """旧 worker 清理期间到达的新消息应保留，并在清理完成后执行。"""

    async def _run_scenario():
        manager = AgentManager()
        await manager.initialize()
        current_started = asyncio.Event()
        cancellation_cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def _process(task):
            if task.message == "current":
                current_started.set()
                await asyncio.Event().wait()
            return "next-completed"

        async def _finish_status(_task):
            cancellation_cleanup_started.set()
            await release_cleanup.wait()

        manager._process_message_internal = _process
        manager._finish_task_processing_status = _finish_status
        current_waiter = asyncio.create_task(
            manager.process_message(
                session_id="session-1",
                user_id="10001",
                message="current",
                wait_for_completion=True,
            )
        )
        await asyncio.wait_for(current_started.wait(), timeout=1)
        stop_task = asyncio.create_task(manager.stop_current_task("session-1"))
        await asyncio.wait_for(cancellation_cleanup_started.wait(), timeout=1)

        next_waiter = asyncio.create_task(
            manager.process_message(
                session_id="session-1",
                user_id="10001",
                message="next",
                wait_for_completion=True,
            )
        )
        await asyncio.sleep(0)
        assert not next_waiter.done()

        release_cleanup.set()
        assert await asyncio.wait_for(stop_task, timeout=1) is True
        assert await asyncio.wait_for(next_waiter, timeout=1) == "next-completed"
        with pytest.raises(asyncio.CancelledError):
            await current_waiter
        await manager.close()

    asyncio.run(_run_scenario())
