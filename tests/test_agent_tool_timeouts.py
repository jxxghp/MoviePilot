import asyncio
import threading
from unittest.mock import patch

import pytest

from app.agent.tools.base import MoviePilotTool, _blocking_executors, shutdown_blocking_executors
from app.agent.tools.manager import MoviePilotToolsManager


class SlowAgentTool(MoviePilotTool):
    """用于验证工具超时保护的慢工具。"""

    name: str = "slow_agent_tool"
    description: str = "Test slow tool."

    async def run(self, **kwargs) -> str:
        """等待足够久以触发测试中的短超时。"""
        await asyncio.sleep(1)
        return "finished"


class BlockingAgentTool(MoviePilotTool):
    """用于验证阻塞调用并发名额释放时机的工具。"""

    name: str = "blocking_agent_tool"
    description: str = "Test blocking tool."

    async def run(self, **kwargs) -> str:
        """本测试不会直接调用该方法。"""
        return "unused"


def test_arun_returns_timeout_message_when_tool_exceeds_limit():
    """LangChain 工具入口应按 LLM_TOOL_TIMEOUT 停止等待慢工具。"""
    tool = SlowAgentTool(session_id="session-1", user_id="10001")

    async def _run_tool():
        with patch("app.agent.tools.base.settings.LLM_TOOL_TIMEOUT", 0.05):
            return await tool._arun()

    result = asyncio.run(_run_tool())

    assert "工具 slow_agent_tool 执行超时" in result
    assert "超过 0.05 秒" in result


def test_http_tool_manager_uses_same_timeout_guard():
    """HTTP/MCP 工具入口绕过 _arun 时也应复用工具超时保护。"""
    manager = MoviePilotToolsManager(is_admin=True)
    manager.tools = [SlowAgentTool(session_id="session-1", user_id="10001")]

    async def _call_tool():
        with patch("app.agent.tools.base.settings.LLM_TOOL_TIMEOUT", 0.05):
            return await manager.call_tool("slow_agent_tool", {})

    result = asyncio.run(_call_tool())

    assert "工具 slow_agent_tool 执行超时" in result


def test_run_blocking_keeps_bucket_slot_until_worker_finishes():
    """被取消的阻塞调用在底层线程结束前不应释放同桶并发名额。"""
    tool = BlockingAgentTool(session_id="session-1", user_id="10001")
    started = asyncio.Event()
    release = threading.Event()

    def _blocking_call() -> str:
        loop.call_soon_threadsafe(started.set)
        release.wait()
        return "done"

    async def _run_scenario():
        nonlocal loop
        loop = asyncio.get_running_loop()
        with patch.dict(
            "app.agent.tools.base._blocking_semaphores",
            {"subscribe": asyncio.Semaphore(1)},
        ):
            task = asyncio.create_task(tool.run_blocking("subscribe", _blocking_call))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            second_task = asyncio.create_task(
                tool.run_blocking("subscribe", lambda: "second")
            )
            await asyncio.sleep(0.05)
            assert not second_task.done()

            release.set()
            assert await asyncio.wait_for(second_task, timeout=1) == "second"

    loop = None
    asyncio.run(_run_scenario())


def test_shutdown_blocking_executors_clears_agent_tool_workers():
    """测试结束清理应关闭 Agent 工具阻塞线程池，避免全量测试退出时等待 worker。"""

    async def _create_worker():
        await MoviePilotTool.run_blocking("web", lambda: "done")

    asyncio.run(_create_worker())
    assert "web" in _blocking_executors

    shutdown_blocking_executors()

    assert _blocking_executors == {}


def test_shutdown_blocking_executors_cancels_queued_workers_and_is_idempotent():
    """收尾清理应取消尚未开始的排队任务，并允许重复调用。"""
    shutdown_blocking_executors(wait=False, cancel_futures=True)
    started = [threading.Event(), threading.Event()]
    release = threading.Event()
    queued_ran = threading.Event()

    def _blocking_call(index: int) -> str:
        started[index].set()
        release.wait()
        return f"done-{index}"

    async def _run_scenario():
        tasks = [
            asyncio.create_task(MoviePilotTool.run_blocking("web", _blocking_call, index))
            for index in range(2)
        ]
        for event in started:
            assert await asyncio.wait_for(asyncio.to_thread(event.wait), timeout=1)
        executor = _blocking_executors["web"]
        queued_future = executor.submit(queued_ran.set)
        shutdown_blocking_executors(wait=False, cancel_futures=True)
        shutdown_blocking_executors(wait=False, cancel_futures=True)
        release.set()

        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=1) == ["done-0", "done-1"]
        return queued_future

    queued_future = asyncio.run(_run_scenario())

    assert _blocking_executors == {}
    assert queued_future.cancelled()
    assert not queued_ran.is_set()


def test_create_agent_config_uses_llm_max_iterations():
    """Agent 执行配置应把 LLM_MAX_ITERATIONS 传给 LangGraph recursion_limit。"""
    from app.agent import MoviePilotAgent
    from langchain_core.messages import AIMessage

    class _FakeGraphState:
        """提供最小 LangGraph 状态替身。"""

        values = {"messages": [AIMessage(content="ok")]}

    class _FakeAgent:
        """记录 ainvoke 收到的 config。"""

        def __init__(self) -> None:
            self.config = None

        async def ainvoke(self, _payload, config=None):
            """保存运行配置供断言。"""
            self.config = config

        def get_state(self, _config):
            """返回最小消息状态。"""
            return _FakeGraphState()

    async def _execute() -> dict:
        agent = MoviePilotAgent(session_id="session-1", user_id="10001")
        fake_agent = _FakeAgent()
        agent._should_stream = lambda: False

        async def _create_agent(streaming=False):
            """返回测试替身 Agent。"""
            return fake_agent

        agent._create_agent = _create_agent
        agent.stream_handler.stop_streaming = lambda: asyncio.sleep(0, result=(False, ""))
        with patch("app.agent.settings.LLM_MAX_ITERATIONS", 7):
            await agent._execute_agent([])
        return fake_agent.config

    config = asyncio.run(_execute())

    assert config["recursion_limit"] == 7
