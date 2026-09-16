"""真实 Agent 图中的子代理跨轮复用与上下文压缩回归。"""

import json
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.middleware.subagents import (
    SubAgentTaskControlMiddleware,
    _builtin_subagent_profiles,
)
from app.agent.middleware.summarization import ContextPreservingSummarizationMiddleware


@pytest.fixture
def anyio_backend():
    """固定使用运行时支持的 asyncio 后端。"""
    return "asyncio"


class _TaskModel(FakeMessagesListChatModel):
    """提供确定的工具调用响应，避免测试访问真实模型。"""

    def bind_tools(self, tools, **kwargs):
        """允许 LangChain 绑定工具并复用脚本响应。"""
        return self


def _task_call(call_id: str) -> AIMessage:
    """生成需要子代理返回结果的委派调用。"""
    return AIMessage(content="", tool_calls=[{
        "name": "subagent_task",
        "args": {"action": "run", "description": "检查下载器状态", "timeout_ms": 1000},
        "id": call_id,
        "type": "tool_call",
    }])


@pytest.mark.anyio
async def test_cached_graph_can_delegate_on_consecutive_turns(monkeypatch):
    """正常结束仅回收本轮任务，缓存图下一轮仍能实际调用子代理。"""
    model = _TaskModel(responses=[_task_call("one"), AIMessage(content="检查完成"),
                                  _task_call("two"), AIMessage(content="再次完成")])
    middleware = SubAgentTaskControlMiddleware(
        model=model, profiles=_builtin_subagent_profiles(), tools=[],
    )
    run_task = AsyncMock(return_value="下载器在线")
    monkeypatch.setattr(middleware._provider, "run_task", run_task)
    graph = create_agent(model=model, middleware=[middleware])

    for question in ("检查下载器", "再检查一次"):
        result = await graph.ainvoke({"messages": [HumanMessage(content=question)]})
        output = next(message for message in result["messages"] if message.type == "tool")
        assert json.loads(output.content)["tasks"][0]["result"] == "下载器在线"
        assert middleware._tasks == {}

    assert run_task.await_count == 2


@pytest.mark.anyio
async def test_turn_cleanup_never_reopens_sealed_controller():
    """永久关闭的 owner 不会因回合结束被重新开放。"""
    middleware = SubAgentTaskControlMiddleware(
        model=_TaskModel(responses=[AIMessage(content="完成")]),
        profiles=_builtin_subagent_profiles(), tools=[],
    )
    middleware.seal()
    await middleware.aafter_agent({}, None)
    response = json.loads(await middleware._control_task(action="start", description="检查"))
    assert response["success"] is False
    assert "关闭" in response["error"]
    assert await middleware.close() is True


@pytest.mark.anyio
async def test_subagent_compacts_large_input_before_model_call(monkeypatch):
    """独立子图也执行最终请求压缩，大量工具输出不会绕过预算边界。"""
    @tool
    def diagnostic() -> str:
        """读取诊断日志，不访问外部服务。"""
        return "诊断日志中的连接信息。" * 450

    diagnostic.tags = ["read", "system"]
    model = _TaskModel(
        responses=[*[AIMessage(content="", tool_calls=[{
            "name": "diagnostic", "args": {}, "id": f"log-{index}", "type": "tool_call",
        }]) for index in range(3)], AIMessage(content="诊断完成")],
        profile={"max_input_tokens": 4096, "max_output_tokens": 512},
    )
    middleware = SubAgentTaskControlMiddleware(
        model=model, profiles=_builtin_subagent_profiles(), tools=[diagnostic],
    )
    summarize = AsyncMock(return_value="用户要求检查下载器；已收集大量日志，继续分析。")
    monkeypatch.setattr(ContextPreservingSummarizationMiddleware, "acreate_summary", summarize)
    result = await middleware._provider.run_task(
        description="检查下载器。",
        subagent_type="general-purpose",
    )
    assert result == "诊断完成"
    assert summarize.await_count >= 1
