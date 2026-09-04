import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

import app.agent.orchestrator as agent_module
from app.agent.memory import MemoryManager
from app.agent.middleware.config import RuntimeConfigMiddleware
from app.agent.middleware.summarization import (
    ContextPreservingSummarizationMiddleware,
    ContextSummarizationError,
    FinalRequestCompactionMiddleware,
)
from app.agent.middleware.usage import UsageMiddleware


class _FakeLLM:
    _llm_type = "openai-chat"

    def __init__(self, model: str):
        self.model = model
        self.profile = {"max_input_tokens": 64000}

    def with_retry(self):
        """满足新版 LangChain 摘要模型的 Runnable 合同。"""
        return self


class _FailingSummaryLLM(_FakeLLM):
    """模拟摘要模型暂时不可用。"""

    async def ainvoke(self, *_args, **_kwargs):
        """摘要请求始终超时。"""
        raise TimeoutError("summary provider unavailable")

    def invoke(self, *_args, **_kwargs):
        """同步摘要请求始终超时。"""
        raise TimeoutError("summary provider unavailable")


class _SuccessfulSummaryLLM(_FakeLLM):
    """提供稳定摘要结果。"""

    async def ainvoke(self, *_args, **_kwargs):
        """返回异步摘要。"""
        return AIMessage(content="保留旧事实的摘要")

    def invoke(self, *_args, **_kwargs):
        """返回同步摘要。"""
        return AIMessage(content="保留旧事实的摘要")


class _CountingSummaryLLM(_SuccessfulSummaryLLM):
    """记录真实 Agent 图触发的摘要次数。"""

    def __init__(self, model: str):
        super().__init__(model)
        self.calls = 0

    async def ainvoke(self, *_args, **_kwargs):
        """记录异步摘要请求。"""
        self.calls += 1
        return AIMessage(content="保留旧事实的摘要")

    def invoke(self, *_args, **_kwargs):
        """记录同步摘要请求。"""
        self.calls += 1
        return AIMessage(content="保留旧事实的摘要")


class _LongSummaryLLM(_SuccessfulSummaryLLM):
    """返回本身无法装入主模型窗口的摘要。"""

    async def ainvoke(self, *_args, **_kwargs):
        """返回异常超长摘要。"""
        return AIMessage(content="异常冗长摘要 " * 2000)

    def invoke(self, *_args, **_kwargs):
        """返回异常超长摘要。"""
        return AIMessage(content="异常冗长摘要 " * 2000)


class _MetadataRecordingSummaryLLM(_SuccessfulSummaryLLM):
    """记录摘要内部模型调用使用的 metadata。"""

    def __init__(self, model: str):
        super().__init__(model)
        self.configs = []

    async def ainvoke(self, *_args, **kwargs):
        """记录异步摘要调用配置。"""
        self.configs.append(kwargs.get("config"))
        return AIMessage(content="保留旧事实的摘要")

    def invoke(self, *_args, **kwargs):
        """记录同步摘要调用配置。"""
        self.configs.append(kwargs.get("config"))
        return AIMessage(content="保留旧事实的摘要")


class _RecordingChatModel(FakeMessagesListChatModel):
    """记录主模型实际收到的最终请求。"""

    seen_messages: list[list[AnyMessage]] = Field(default_factory=list)

    def bind_tools(self, _tools, **_kwargs):
        """保留测试模型，同时满足工具绑定契约。"""
        return self

    def _generate(self, messages, *args, **kwargs):
        """记录包含 system 的最终消息序列。"""
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


class _FailingMainModel(_RecordingChatModel):
    """模拟最终请求进入主模型后失败。"""

    def _generate(self, messages, *_args, **_kwargs):
        """记录请求后终止模型调用。"""
        self.seen_messages.append(list(messages))
        raise TimeoutError("main provider unavailable")


class _DynamicSystemMiddleware(AgentMiddleware):
    """模拟运行时中间件追加的大型 system prompt。"""

    async def awrap_model_call(self, request, handler):
        """在最终压缩器之前补充动态 system prompt。"""
        return await handler(request.override(system_message=SystemMessage(content="动态系统约束 " * 250)))


def _final_request(*, messages, system_message=None, tools=None) -> ModelRequest:
    """构造包含最终系统提示词和工具目录的模型请求。"""
    return ModelRequest(
        model=SimpleNamespace(
            model="small-model",
            profile={"max_input_tokens": 2048},
        ),
        messages=list(messages),
        system_message=system_message,
        tools=list(tools or []),
        state={"messages": list(messages)},
        runtime=None,
    )


def _real_compaction_graph(*, model, summarizer, tools=None, checkpointer=None):
    """构造包含真实 LangChain 状态归并路径的最小 Agent 图。"""
    summary_middleware = ContextPreservingSummarizationMiddleware(
        model=summarizer,
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    return create_agent(
        model=model,
        tools=list(tools or []),
        middleware=[
            _DynamicSystemMiddleware(),
            FinalRequestCompactionMiddleware(summarizer=summary_middleware),
        ],
        checkpointer=checkpointer,
    )


def _oversized_final_request_history() -> list[HumanMessage]:
    """生成历史本身未达阈值、叠加动态 system 后超阈值的消息。"""
    return [HumanMessage(content=(f"必须保留的历史事实 {index} " * 80)) for index in range(6)]


class _FailingGraph:
    """模拟上下文压缩阶段失败的 Agent 图。"""

    async def ainvoke(self, _payload, config=None):
        """在提交图状态前终止执行。"""
        raise ContextSummarizationError("会话上下文压缩失败，原有上下文已保留，请稍后重试")


def test_streaming_agent_uses_non_streaming_llm_for_summary():
    """流式 Agent 的摘要中间件应使用非流式 LLM。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    non_streaming_llm = _FakeLLM("non-streaming")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", side_effect=[main_llm, non_streaming_llm]),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=True))

    compaction_middleware = next(
        middleware for middleware in captured["middleware"] if isinstance(middleware, FinalRequestCompactionMiddleware)
    )

    assert captured["model"] is main_llm
    assert compaction_middleware.summarizer.model is non_streaming_llm
    assert not any(
        isinstance(middleware, ContextPreservingSummarizationMiddleware) for middleware in captured["middleware"]
    )


def test_streaming_agent_uses_non_streaming_llm_for_model_middlewares():
    """流式 Agent 的模型型中间件应使用非流式 LLM。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    non_streaming_llm = _FakeLLM("non-streaming")
    captured: dict = {}

    class _FakeToolSelectorMiddleware:
        """记录工具选择中间件初始化参数。"""

        def __init__(
            self,
            model,
            max_tools,
            always_include=None,
            selection_tools=None,
        ):
            """保存测试断言需要的参数。"""
            self.model = model
            self.max_tools = max_tools
            self.always_include = always_include or []
            self.selection_tools = selection_tools or []

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    class _FakeTool:
        """测试用工具占位对象。"""

        def __init__(self, name: str):
            """保存工具名。"""
            self.name = name

    fake_tools = [
        _FakeTool("moviepilot_api"),
        _FakeTool("agent_task"),
        _FakeTool("write_file"),
        _FakeTool("read_file"),
        _FakeTool("edit_file"),
        _FakeTool("execute_command"),
    ]

    with (
        patch.object(agent, "_initialize_llm", side_effect=[main_llm, non_streaming_llm]),
        patch.object(agent, "_initialize_tools", return_value=fake_tools),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(
            agent_module,
            "ToolSelectorMiddleware",
            _FakeToolSelectorMiddleware,
        ),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 3),
    ):
        asyncio.run(agent._create_agent(streaming=True))

    tool_selector_middleware = next(
        middleware for middleware in captured["middleware"] if isinstance(middleware, _FakeToolSelectorMiddleware)
    )

    assert tool_selector_middleware.model is non_streaming_llm
    assert tool_selector_middleware.max_tools == 3
    assert tool_selector_middleware.always_include == [
        "moviepilot_api",
        "write_file",
        "read_file",
        "edit_file",
        "execute_command",
        "agent_task",
        "read_skill",
    ]
    assert tool_selector_middleware.selection_tools[: len(fake_tools)] == fake_tools
    assert [getattr(tool, "name", None) for tool in tool_selector_middleware.selection_tools[len(fake_tools) :]] == [
        "read_skill"
    ]


def test_non_streaming_agent_reuses_main_llm_for_summary():
    """非流式 Agent 的摘要中间件应复用主 LLM。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=main_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    compaction_middleware = next(
        middleware for middleware in captured["middleware"] if isinstance(middleware, FinalRequestCompactionMiddleware)
    )

    assert captured["model"] is main_llm
    assert compaction_middleware.summarizer.model is main_llm
    assert not any(
        isinstance(middleware, ContextPreservingSummarizationMiddleware) for middleware in captured["middleware"]
    )


def test_summary_failure_does_not_replace_existing_context():
    """摘要模型失败时应中止压缩，避免错误文本替换既有上下文。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    failing_llm = _FailingSummaryLLM("summary")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=failing_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    compaction_middleware = next(
        middleware for middleware in captured["middleware"] if isinstance(middleware, FinalRequestCompactionMiddleware)
    )
    summary_middleware = compaction_middleware.summarizer
    messages = [HumanMessage(content=f"必须保留的旧上下文 {index} " * 200) for index in range(160)]
    assert summary_middleware.token_counter(messages) >= 64000 * 0.85
    cutoff_index = summary_middleware._determine_cutoff_index(messages)
    assert summary_middleware._trim_messages_for_summary(messages[:cutoff_index])

    with pytest.raises(RuntimeError, match="会话上下文压缩失败"):
        asyncio.run(summary_middleware.abefore_model({"messages": messages}, None))

    with pytest.raises(RuntimeError, match="会话上下文压缩失败"):
        summary_middleware.before_model({"messages": messages}, None)


def test_summary_success_still_replaces_old_context():
    """摘要成功时仍应保留上游的压缩行为。"""
    middleware = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("messages", 25),
    )
    messages = [HumanMessage(content=f"消息 {index}") for index in range(25)]

    update = asyncio.run(middleware.abefore_model({"messages": messages}, None))

    assert update is not None
    assert "保留旧事实的摘要" in update["messages"][1].content
    assert len(update["messages"]) < len(messages)


def test_final_request_compaction_includes_dynamic_system_and_tools():
    """最终 system 和工具预算达到阈值时，应在同轮压缩历史。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("fraction", 0.10),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    messages = [HumanMessage(content=f"必须保留的历史事实 {index} " * 120) for index in range(6)]
    request = _final_request(
        messages=messages,
        system_message=SystemMessage(content="动态系统约束 " * 40),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "工具业务说明 " * 100,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="继续完成")])

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert isinstance(result, ExtendedModelResponse)
    assert len(received) == 1
    assert len(received[0].messages) < len(messages)
    assert "保留旧事实的摘要" in received[0].messages[0].content
    compacted_budget = UsageMiddleware.estimate_request(received[0])
    assert compacted_budget["estimated_input_ratio"] <= 0.85
    assert result.command is not None
    assert "保留旧事实的摘要" in result.command.update["messages"][1].content
    assert result.command.update["messages"][-1].content == "继续完成"


def test_final_request_compaction_preserves_history_when_summary_fails():
    """动态压缩失败时中止本轮，不提交摘要或调用主模型。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_FailingSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("fraction", 0.10),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    messages = [HumanMessage(content=f"必须保留的历史事实 {index} " * 120) for index in range(6)]
    request = _final_request(
        messages=messages,
        system_message=SystemMessage(content="动态系统约束 " * 80),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "工具业务说明 " * 300,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    received = []

    async def _handler(original_request):
        received.append(original_request)
        return ModelResponse(result=[AIMessage(content="不应执行")])

    with pytest.raises(ContextSummarizationError, match="会话上下文压缩失败"):
        asyncio.run(middleware.awrap_model_call(request, _handler))

    assert received == []


@pytest.mark.parametrize("failure", ["fixed-overhead", "long-summary"])
def test_final_request_compaction_rejects_known_overflow_before_main_model(failure):
    """压缩后仍已知超窗时不得把请求发送给主模型。"""
    summary_model = _LongSummaryLLM("summary") if failure == "long-summary" else _SuccessfulSummaryLLM("summary")
    summarizer = ContextPreservingSummarizationMiddleware(
        model=summary_model,
        trigger=("fraction", 0.85),
        keep=("fraction", 0.10),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    request = _final_request(
        messages=_oversized_final_request_history(),
        system_message=SystemMessage(
            content=("不可缩减的系统约束 " * 1500 if failure == "fixed-overhead" else "动态系统约束 " * 250)
        ),
    )
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="不应执行")])

    with pytest.raises(ContextSummarizationError, match="仍超出上下文窗口"):
        asyncio.run(middleware.awrap_model_call(request, _handler))

    assert received == []


def test_uncompactable_request_below_window_still_calls_main_model():
    """主动压缩线不是硬拒绝线，窗口内请求应保持可用。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("fraction", 0.10),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    request = _final_request(
        messages=[HumanMessage(content="最新问题")],
        system_message=SystemMessage(content="不可缩减的系统约束 " * 750),
    )
    budget = UsageMiddleware.estimate_request(request)
    assert 0.85 <= budget["estimated_input_ratio"] <= 1
    received = []

    async def _handler(original_request):
        received.append(original_request)
        return ModelResponse(result=[AIMessage(content="继续完成")])

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert isinstance(result, ModelResponse)
    assert received == [request]


def test_overflow_with_small_history_compacts_to_hard_window():
    """历史低于常规保留量时，超窗请求仍应尝试压缩而不是直接拒绝。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    messages = [HumanMessage(content=f"近期历史 {index} " * 5) for index in range(4)]
    request = _final_request(
        messages=messages,
        system_message=SystemMessage(content="固定系统约束 " * 1150),
    )
    original_budget = UsageMiddleware.estimate_request(request)
    fixed_budget = UsageMiddleware.estimate_request(request.override(messages=[]))
    assert fixed_budget["estimated_input_ratio"] < 1
    assert summarizer.token_counter(messages) < 2048 * 0.10
    assert original_budget["estimated_input_ratio"] > 1
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="继续完成")])

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert isinstance(result, ExtendedModelResponse)
    assert len(received) == 1
    compacted_budget = UsageMiddleware.estimate_request(received[0])
    assert 0.85 < compacted_budget["estimated_input_ratio"] <= 1


def test_compaction_uses_hard_window_when_soft_target_is_unreachable():
    """固定开销超过软线时，应缩小近期历史直到完整窗口可承载。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    messages = [HumanMessage(content=f"需要择量保留的历史 {index} " * 20) for index in range(24)]
    request = _final_request(
        messages=messages,
        system_message=SystemMessage(content="固定系统约束 " * 1100),
    )
    fixed_budget = UsageMiddleware.estimate_request(request.override(messages=[]))
    assert 0.85 < fixed_budget["estimated_input_ratio"] < 1
    assert UsageMiddleware.estimate_request(request)["estimated_input_ratio"] > 1
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="继续完成")])

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert isinstance(result, ExtendedModelResponse)
    assert len(received) == 1
    compacted_budget = UsageMiddleware.estimate_request(received[0])
    assert 0.85 < compacted_budget["estimated_input_ratio"] <= 1


def test_forced_compaction_advances_beyond_existing_summary():
    """超窗时应继续压缩旧事实，不能因首条已有摘要而误判无进展。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    messages = [
        AIMessage(
            content="已有摘要 " * 570,
            additional_kwargs={"lc_source": "summarization"},
        ),
        HumanMessage(content="仍可继续压缩的旧事实 " * 92),
        HumanMessage(content="最新问题"),
    ]
    request = _final_request(
        messages=messages,
        system_message=SystemMessage(content="固定系统约束 " * 700),
    )
    fixed_budget = UsageMiddleware.estimate_request(request.override(messages=[]))
    assert fixed_budget["estimated_input_ratio"] < 1
    assert UsageMiddleware.estimate_request(request)["estimated_input_ratio"] > 1
    partition = summarizer.partition_for_token_limit(
        messages,
        int(2048 * 0.10),
        force=True,
    )
    assert partition is not None
    assert len(partition[0]) == 2
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="继续完成")])

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert isinstance(result, ExtendedModelResponse)
    assert len(received) == 1
    assert received[0].messages[-1].id == messages[-1].id
    assert UsageMiddleware.estimate_request(received[0])["estimated_input_ratio"] <= 1


def test_forced_compaction_keeps_only_current_message_instead_of_summarizing_it():
    """唯一当前消息不可被强制摘要，无法装入窗口时应保留历史并拒绝。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    latest_message = HumanMessage(content="唯一且必须保留的当前问题 " * 40)
    request = _final_request(
        messages=[latest_message],
        system_message=SystemMessage(content="固定系统约束 " * 1100),
    )
    fixed_budget = UsageMiddleware.estimate_request(request.override(messages=[]))
    assert fixed_budget["estimated_input_ratio"] < 1
    assert UsageMiddleware.estimate_request(request)["estimated_input_ratio"] > 1
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="不应执行")])

    with pytest.raises(ContextSummarizationError, match="仍超出上下文窗口"):
        asyncio.run(middleware.awrap_model_call(request, _handler))

    assert received == []
    assert request.messages == [latest_message]


def test_forced_compaction_rejects_single_long_current_message():
    """当前消息超过保留预算时也不可被整体摘要。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    latest_message = HumanMessage(content="当前用户唯一问题 " * 830)
    request = _final_request(
        messages=[latest_message],
        system_message=SystemMessage(content="固定系统约束 " * 100),
    )
    assert UsageMiddleware.estimate_request(request)["estimated_input_ratio"] > 1
    assert (
        summarizer.partition_for_token_limit(
            [latest_message],
            int(2048 * 0.10),
            force=True,
        )
        is None
    )
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="不应执行")])

    with pytest.raises(ContextSummarizationError, match="仍超出上下文窗口"):
        asyncio.run(middleware.awrap_model_call(request, _handler))

    assert received == []
    assert request.messages == [latest_message]


def test_repartition_advances_past_complete_old_tool_transaction():
    """二次分区应整体摘要旧工具事务，而不是停在相同安全边界。"""
    summarizer = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    middleware = FinalRequestCompactionMiddleware(summarizer=summarizer)
    latest_message = HumanMessage(content="新的用户问题")
    messages = [
        HumanMessage(content="更早历史 " * 30),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "old_tool",
                    "args": {"text": "工具参数 " * 500},
                    "id": "old-call",
                }
            ],
        ),
        ToolMessage(content="旧工具结果", tool_call_id="old-call"),
        latest_message,
    ]
    request = _final_request(
        messages=messages,
        system_message=SystemMessage(content="固定系统约束 " * 780),
    )
    assert UsageMiddleware.estimate_request(request)["estimated_input_ratio"] > 1
    received = []

    async def _handler(compacted_request):
        received.append(compacted_request)
        return ModelResponse(result=[AIMessage(content="继续完成")])

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert isinstance(result, ExtendedModelResponse)
    assert len(received) == 1
    assert received[0].messages[-1].id == latest_message.id
    assert not any(isinstance(message, ToolMessage) for message in received[0].messages)
    assert UsageMiddleware.estimate_request(received[0])["estimated_input_ratio"] <= 1


def test_summary_calls_include_version_compatible_internal_metadata():
    """摘要调用应合并当前 LangChain 提供的内部流式过滤标记。"""
    summary_model = _MetadataRecordingSummaryLLM("summary")
    middleware = ContextPreservingSummarizationMiddleware(
        model=summary_model,
        trigger=("messages", 2),
    )
    messages = [HumanMessage(content="旧消息"), HumanMessage(content="新消息")]

    with patch(
        "app.agent.middleware.summarization._internal_call_metadata",
        return_value={"lc_internal_call": "process-marker"},
    ):
        middleware.create_summary(messages)
        asyncio.run(middleware.acreate_summary(messages))

    assert [config["metadata"] for config in summary_model.configs] == [
        {"lc_source": "summarization", "lc_internal_call": "process-marker"},
        {"lc_source": "summarization", "lc_internal_call": "process-marker"},
    ]


@pytest.mark.parametrize("execution", ["ainvoke", "astream"])
def test_real_agent_commits_final_request_compaction(execution):
    """真实图在普通和流式执行中应提交相同的压缩后最终状态。"""
    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[AIMessage(content="继续完成")],
        profile={"max_input_tokens": 2048},
    )
    checkpointer = InMemorySaver()
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        checkpointer=checkpointer,
    )
    messages = _oversized_final_request_history()
    config = {"configurable": {"thread_id": f"compaction-{execution}"}}

    async def _run():
        if execution == "ainvoke":
            result = await graph.ainvoke({"messages": messages}, config=config)
            return result, (await graph.aget_state(config)).values
        final_state = None
        async for state in graph.astream({"messages": messages}, config=config, stream_mode="values"):
            final_state = state
        return final_state, (await graph.aget_state(config)).values

    result, persisted = asyncio.run(_run())

    assert result is not None
    assert [message.content for message in result["messages"]] == [message.content for message in persisted["messages"]]
    assert summarizer.calls == 1
    assert len(model.seen_messages) == 1
    assert isinstance(model.seen_messages[0][0], SystemMessage)
    assert "保留旧事实的摘要" in model.seen_messages[0][1].content
    assert "保留旧事实的摘要" in result["messages"][0].content
    assert result["messages"][-1].content == "继续完成"
    assert len(result["messages"]) < len(messages)


def test_real_agent_does_not_compact_request_below_threshold():
    """最终请求低于主模型阈值时不得调用摘要模型。"""
    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[AIMessage(content="直接完成")],
        profile={"max_input_tokens": 8192},
    )
    graph = _real_compaction_graph(model=model, summarizer=summarizer)
    messages = _oversized_final_request_history()

    result = asyncio.run(graph.ainvoke({"messages": messages}))

    assert summarizer.calls == 0
    assert len(model.seen_messages[0]) == len(messages) + 1
    assert [message.content for message in result["messages"][:-1]] == [message.content for message in messages]


def test_real_agent_executes_compacted_tool_call_once():
    """压缩不得重试主模型或重复执行工具事务。"""
    calls = []

    @tool
    def record_value(value: str) -> str:
        """记录工具调用次数。"""
        calls.append(value)
        return value

    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "record_value", "args": {"value": "once"}, "id": "call-1"}],
            ),
            AIMessage(content="工具完成"),
        ],
        profile={"max_input_tokens": 2048},
    )
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        tools=[record_value],
    )

    result = asyncio.run(graph.ainvoke({"messages": _oversized_final_request_history()}))

    assert calls == ["once"]
    assert summarizer.calls == 1
    assert len(model.seen_messages) == 2
    assert result["messages"][-1].content == "工具完成"


def test_real_agent_does_not_recompact_small_tool_result_during_same_loop():
    """小工具结果不会让同一轮请求重新压缩。"""
    calls = []

    @tool
    def record_value(value: str) -> str:
        """记录工具调用次数。"""
        calls.append(value)
        return value

    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "record_value", "args": {"value": "once"}, "id": "call-1"}],
            ),
            AIMessage(content="工具完成"),
        ],
        profile={"max_input_tokens": 2048},
    )
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        tools=[record_value],
    )

    result = asyncio.run(graph.ainvoke({"messages": _oversized_final_request_history()}))

    assert calls == ["once"]
    assert summarizer.calls == 1
    assert len(model.seen_messages) == 2
    assert result["messages"][-1].content == "工具完成"


def test_large_tool_result_allows_same_turn_recompaction():
    """新工具结果使请求超窗时，同轮 anchor 不得阻止再次压缩。"""
    calls = []

    @tool
    def large_result() -> str:
        """返回足以再次耗尽主模型窗口的工具结果。"""
        calls.append("once")
        return "超长工具结果 " * 800

    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "large_result", "args": {}, "id": "large-call"}],
            ),
            AIMessage(content="工具完成"),
        ],
        profile={"max_input_tokens": 2048},
    )
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        tools=[large_result],
    )

    result = asyncio.run(graph.ainvoke({"messages": _oversized_final_request_history()}))

    assert calls == ["once"]
    assert summarizer.calls == 2
    assert len(model.seen_messages) == 2
    second_request = model.seen_messages[1]
    assert isinstance(second_request[-2], AIMessage)
    assert isinstance(second_request[-1], ToolMessage)
    assert second_request[-1].tool_call_id == second_request[-2].tool_calls[0]["id"]
    assert result["messages"][-1].content == "工具完成"


def test_real_agent_can_compact_again_after_new_user_message():
    """同轮保护不得阻止后续用户轮次继续滚动压缩。"""
    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[AIMessage(content="第一轮完成"), AIMessage(content="第二轮完成")],
        profile={"max_input_tokens": 1024},
    )
    checkpointer = InMemorySaver()
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "compaction-next-turn"}}

    async def _run():
        await graph.ainvoke(
            {"messages": _oversized_final_request_history()},
            config=config,
        )
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="新的用户问题 " * 300)]},
            config=config,
        )

    result = asyncio.run(_run())

    assert summarizer.calls == 2
    assert len(model.seen_messages) == 2
    assert result["messages"][-1].content == "第二轮完成"


def test_real_agent_does_not_resummarize_existing_summary_only():
    """可移除历史只有既有摘要时，不应反复摘要同一内容。"""
    summarizer = _CountingSummaryLLM("summary")
    model = _RecordingChatModel(
        responses=[AIMessage(content="继续完成")],
        profile={"max_input_tokens": 2048},
    )
    graph = _real_compaction_graph(model=model, summarizer=summarizer)
    messages = [
        HumanMessage(
            content="已有摘要 " * 400,
            additional_kwargs={"lc_source": "summarization"},
        ),
        HumanMessage(content="最新问题"),
    ]

    result = asyncio.run(graph.ainvoke({"messages": messages}))

    assert summarizer.calls == 0
    assert "已有摘要" in model.seen_messages[0][1].content
    assert result["messages"][-1].content == "继续完成"


@pytest.mark.parametrize("failure", ["summary", "main"])
def test_real_agent_failure_does_not_commit_compacted_history(failure):
    """摘要或主模型失败时，checkpoint 只保留原始历史。"""
    checkpointer = InMemorySaver()
    summarizer = _FailingSummaryLLM("summary") if failure == "summary" else _CountingSummaryLLM("summary")
    model = (
        _FailingMainModel(
            responses=[AIMessage(content="不会返回")],
            profile={"max_input_tokens": 2048},
        )
        if failure == "main"
        else _RecordingChatModel(
            responses=[AIMessage(content="不会调用")],
            profile={"max_input_tokens": 2048},
        )
    )
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        checkpointer=checkpointer,
    )
    messages = _oversized_final_request_history()
    config = {"configurable": {"thread_id": f"compaction-{failure}"}}

    async def _run():
        with pytest.raises((ContextSummarizationError, TimeoutError)):
            await graph.ainvoke({"messages": messages}, config=config)
        return await graph.aget_state(config)

    snapshot = asyncio.run(_run())

    assert [message.content for message in snapshot.values["messages"]] == [message.content for message in messages]
    if failure == "summary":
        assert model.seen_messages == []
    else:
        assert summarizer.calls == 1
        assert len(model.seen_messages) == 1


def test_history_triggered_compaction_waits_for_main_model_success():
    """历史本身触发压缩时，主模型失败也不得提前提交摘要状态。"""
    checkpointer = InMemorySaver()
    summarizer = _CountingSummaryLLM("summary")
    summarizer.profile = {"max_input_tokens": 2048}
    model = _FailingMainModel(
        responses=[AIMessage(content="不会返回")],
        profile={"max_input_tokens": 2048},
    )
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        checkpointer=checkpointer,
    )
    messages = [HumanMessage(content=f"历史直接触发压缩 {index} " * 40) for index in range(30)]
    summary_middleware = ContextPreservingSummarizationMiddleware(
        model=summarizer,
        trigger=("fraction", 0.85),
        keep=("messages", 20),
    )
    assert summary_middleware.token_counter(messages) >= 2048 * 0.85
    config = {"configurable": {"thread_id": "history-trigger-main-failure"}}

    async def _run():
        with pytest.raises(TimeoutError):
            await graph.ainvoke({"messages": messages}, config=config)
        return await graph.aget_state(config)

    snapshot = asyncio.run(_run())

    assert [message.content for message in snapshot.values["messages"]] == [message.content for message in messages]
    assert summarizer.calls >= 1
    assert len(model.seen_messages) == 1


def test_history_triggered_compaction_commits_after_main_model_success():
    """历史本身触发压缩时，摘要与模型结果应在成功后一次性提交。"""
    checkpointer = InMemorySaver()
    summarizer = _CountingSummaryLLM("summary")
    summarizer.profile = {"max_input_tokens": 2048}
    model = _RecordingChatModel(
        responses=[AIMessage(content="继续完成")],
        profile={"max_input_tokens": 2048},
    )
    graph = _real_compaction_graph(
        model=model,
        summarizer=summarizer,
        checkpointer=checkpointer,
    )
    messages = [HumanMessage(content=f"历史直接触发压缩 {index} " * 40) for index in range(30)]
    config = {"configurable": {"thread_id": "history-trigger-main-success"}}

    async def _run():
        result = await graph.ainvoke({"messages": messages}, config=config)
        return result, await graph.aget_state(config)

    result, snapshot = asyncio.run(_run())

    assert [message.content for message in result["messages"]] == [
        message.content for message in snapshot.values["messages"]
    ]
    assert "保留旧事实的摘要" in result["messages"][0].content
    assert result["messages"][-1].content == "继续完成"
    assert len(result["messages"]) < len(messages)
    assert summarizer.calls >= 1
    assert len(model.seen_messages) == 1


def test_unsummarizable_message_requires_new_context_instead_of_retry():
    """确定性不可裁剪的消息应给出可前进路径，而非建议无效重试。"""
    middleware = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("messages", 21),
        keep=("messages", 20),
        trim_tokens_to_summarize=1000,
    )
    messages = [
        HumanMessage(content="无法裁剪的单条超长消息" * 4000),
        *[HumanMessage(content=f"后续消息 {index}") for index in range(20)],
    ]
    assert not middleware._trim_messages_for_summary(messages[:1])

    errors = []
    for _ in range(2):
        with pytest.raises(ContextSummarizationError) as error:
            middleware.before_model({"messages": messages}, None)
        errors.append(str(error.value))

    assert errors == ["会话历史中存在无法压缩的超长内容，原有上下文已保留，请新建或清空会话后继续"] * 2
    assert all("稍后重试" not in error for error in errors)


def test_summary_failure_preserves_database_history():
    """上下文压缩失败时不得覆盖数据库中的上一轮消息。"""
    session_id = f"summary-failure-{uuid.uuid4().hex}"
    user_id = "10001"
    isolated_memory = MemoryManager()
    isolated_memory.save_agent_messages(
        session_id=session_id,
        user_id=user_id,
        messages=[HumanMessage(content="数据库中的旧事实")],
    )
    isolated_memory.clear_memory(session_id, user_id)
    restored_messages = isolated_memory.get_agent_messages(session_id, user_id)
    agent = agent_module.MoviePilotAgent(
        session_id=session_id,
        user_id=user_id,
        memory=isolated_memory,
    )
    agent._compiled_agent_bundle = object()
    agent._should_stream = lambda: False
    agent._create_agent = AsyncMock(return_value=_FailingGraph())
    agent.stream_handler = SimpleNamespace(stop_streaming=AsyncMock(return_value=(False, "")))
    agent.send_agent_message = AsyncMock()

    with (
        patch("app.agent.orchestrator.eventmanager.send_event") as send_usage_event,
    ):
        result, _ = asyncio.run(agent._execute_agent([*restored_messages, HumanMessage(content="继续原来的任务")]))

    isolated_memory.clear_memory(session_id, user_id)
    recovered_messages = isolated_memory.get_agent_messages(session_id, user_id)
    assert result == "智能助手执行失败，请稍后重试"
    assert agent._compiled_agent_bundle is None
    assert [message.content for message in recovered_messages] == ["数据库中的旧事实"]
    send_usage_event.assert_called_once()
    assert not send_usage_event.call_args.args[1].success


def test_agent_uses_runtime_config_middleware_instead_of_hooks():
    """Agent 应使用运行时配置中间件而不是旧 hooks。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=main_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    assert any(isinstance(middleware, RuntimeConfigMiddleware) for middleware in captured["middleware"])
    assert not any(type(middleware).__name__ == "AgentHooksMiddleware" for middleware in captured["middleware"])
