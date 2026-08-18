"""Agent 最终模型请求预算测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.agent import MoviePilotAgent
from app.agent.middleware.tool_selection import ToolSelectorMiddleware
from app.agent.middleware.usage import UsageMiddleware
from app.application.orchestration.message import MessageChain


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    """记录最终绑定工具，同时保留固定响应行为。"""

    bound_tool_names: list[str] = []

    def bind_tools(self, tools, **kwargs):
        """记录 LangChain 最终交给模型的工具集合。"""
        self.bound_tool_names = [
            item.get("function", {}).get("name") or item.get("name")
            if isinstance(item, dict)
            else item.name
            for item in tools
        ]
        return self


class _DynamicSystemMiddleware(AgentMiddleware):
    """模拟 MoviePilot 运行时动态追加系统上下文。"""

    async def awrap_model_call(self, request, handler):
        current = request.system_message.content if request.system_message else ""
        return await handler(
            request.override(
                system_message=SystemMessage(
                    content=f"{current}\n{'动态系统上下文 ' * 200}"
                )
            )
        )


def _request(
        *,
        messages=None,
        system_message=None,
        tools=None,
        max_input_tokens=4096,
        max_output_tokens=512,
        model_settings=None,
) -> ModelRequest:
    """构造带模型窗口和最终请求组成的测试请求。"""
    return ModelRequest(
        model=SimpleNamespace(
            model="small-model",
            profile={
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
            },
        ),
        messages=list(messages or []),
        system_message=system_message,
        tools=list(tools or []),
        state={},
        runtime=None,
        model_settings=model_settings,
    )


def test_final_request_can_exceed_window_before_message_fraction_triggers():
    """动态系统提示词和工具定义可能在消息摘要阈值前耗尽输入窗口。"""
    messages = [HumanMessage(content="用户上下文 " * 1000)]
    model = SimpleNamespace(
        _llm_type="test-chat",
        profile={"max_input_tokens": 4096},
    )
    model.with_retry = lambda: model
    summarizer = SummarizationMiddleware(
        model=model,
        trigger=("fraction", 0.85),
    )
    message_tokens = summarizer.token_counter(messages)
    assert message_tokens < 4096 * 0.85
    assert not summarizer._should_summarize(messages, message_tokens)

    request = _request(
        messages=messages,
        system_message=SystemMessage(content="动态系统上下文 " * 1000),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "large_schema_tool",
                    "description": "工具业务说明 " * 800,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "string",
                                "description": "参数约束 " * 400,
                            }
                        },
                    },
                },
            }
        ],
    )

    snapshot = UsageMiddleware.estimate_request(request)

    assert snapshot["has_estimate"]
    assert snapshot["message_tokens"] < 4096 * 0.85
    assert snapshot["system_tokens"] > 0
    assert snapshot["tool_tokens"] > 0
    assert snapshot["estimated_input_tokens"] > 4096
    assert snapshot["estimated_over_input_limit"] is True
    assert snapshot["model_max_output_tokens"] == 512
    assert snapshot["configured_output_limit_tokens"] is None
    assert "output_headroom_tokens" not in snapshot


def test_request_budget_marks_same_request_over_limit_after_switch_to_small_model():
    """相同请求切换到小窗口模型后，应仅改变预算判断而不改变估算输入。"""
    messages = [HumanMessage(content="x" * 12000)]

    large_snapshot = UsageMiddleware.estimate_request(
        _request(messages=messages, max_input_tokens=128000)
    )
    small_snapshot = UsageMiddleware.estimate_request(
        _request(messages=messages, max_input_tokens=2048)
    )

    assert large_snapshot["estimated_input_tokens"] == small_snapshot["estimated_input_tokens"]
    assert large_snapshot["estimated_over_input_limit"] is False
    assert small_snapshot["estimated_over_input_limit"] is True


def test_request_budget_reads_only_explicit_per_call_output_limit():
    """只有最终请求显式配置的输出上限才可视为单次调用限制。"""
    request = _request(
        messages=[HumanMessage(content="hello")],
        max_output_tokens=8192,
        model_settings={"max_completion_tokens": 1024},
    )

    snapshot = UsageMiddleware.estimate_request(request)

    assert snapshot["model_max_output_tokens"] == 8192
    assert snapshot["configured_output_limit_tokens"] == 1024
    assert snapshot["estimated_input_tokens"] < snapshot["context_window_tokens"]


def test_request_budget_rejects_non_integer_token_limits():
    """近似观察不得把 bool、浮点或字符串误报为有效 token 上限。"""
    for invalid_value in (True, False, 1.5, 0, -1, "1024"):
        request = _request(
            messages=[HumanMessage(content="hello")],
            max_input_tokens=invalid_value,
            max_output_tokens=invalid_value,
            model_settings={"max_completion_tokens": invalid_value},
        )

        snapshot = UsageMiddleware.estimate_request(request)

        assert snapshot["context_window_tokens"] is None
        assert snapshot["model_max_output_tokens"] is None
        assert snapshot["configured_output_limit_tokens"] is None
        assert snapshot["estimated_input_ratio"] is None
        assert snapshot["estimated_over_input_limit"] is None


def test_request_budget_uses_next_valid_output_limit_alias():
    """高优先字段无效时，应继续读取同一请求中的有效兼容字段。"""
    request = _request(
        messages=[HumanMessage(content="hello")],
        model_settings={"max_completion_tokens": True, "max_tokens": 1024},
    )

    snapshot = UsageMiddleware.estimate_request(request)

    assert snapshot["configured_output_limit_tokens"] == 1024


def test_request_budget_counts_multimodal_input_without_storing_content():
    """图片按固定成本计入估算，快照不得保留请求正文或工具定义。"""
    secret_marker = "REQUEST_BUDGET_SECRET_MARKER"
    request = _request(
        messages=[
            HumanMessage(
                content=[
                    {"type": "text", "text": secret_marker},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,hidden"},
                    },
                    {"type": "file", "file_id": secret_marker},
                ]
            )
        ],
        system_message=SystemMessage(content=secret_marker),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "secret_tool",
                    "description": secret_marker,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    snapshot = UsageMiddleware.estimate_request(request)

    assert snapshot["image_count"] == 1
    assert snapshot["unknown_multimodal_count"] == 1
    assert snapshot["multimodal_tokens"] == 85
    assert snapshot["estimated_input_tokens"] == (
        snapshot["message_tokens"]
        + snapshot["system_tokens"]
        + snapshot["tool_tokens"]
    )
    assert snapshot["model"] == "small-model"
    assert secret_marker not in repr(snapshot)
    assert all(
        value is None or isinstance(value, (bool, int, float))
        for key, value in snapshot.items()
        if key != "model"
    )


def test_request_budget_counts_each_image_cost_exactly_once():
    """LangChain 的消息估算已包含图片固定成本，汇总预算不得重复相加。"""
    text_only = UsageMiddleware.estimate_request(
        _request(
            messages=[HumanMessage(content=[{"type": "text", "text": "hello"}])]
        )
    )
    with_image = UsageMiddleware.estimate_request(
        _request(
            messages=[
                HumanMessage(
                    content=[
                        {"type": "text", "text": "hello"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,hidden"},
                        },
                    ]
                )
            ]
        )
    )

    assert with_image["multimodal_tokens"] == 85
    assert (
        with_image["estimated_input_tokens"] - text_only["estimated_input_tokens"]
        == 85
    )


def test_request_budget_callback_failure_does_not_block_model_call():
    """预算观察失败不得改变模型请求和响应。"""
    request = _request(messages=[HumanMessage(content="hello")])
    response = ModelResponse(result=[AIMessage(content="ok")])

    def _raise_callback(_snapshot):
        raise RuntimeError("observer unavailable")

    middleware = UsageMiddleware(on_request_budget=_raise_callback)
    handled = []

    async def _handler(received: ModelRequest):
        handled.append(received)
        return response

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert result is response
    assert handled == [request]


def test_request_budget_callback_failure_clears_previous_estimate_state():
    """预算回调失败时，本轮 usage 不得与上一轮估算拼接。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    callback_count = 0

    def _record_then_fail(snapshot):
        nonlocal callback_count
        callback_count += 1
        if callback_count == 2:
            raise RuntimeError("observer unavailable")
        agent._record_request_budget(snapshot)

    middleware = UsageMiddleware(
        on_request_budget=_record_then_fail,
        on_usage=agent._record_usage,
    )
    responses = iter(
        [
            ModelResponse(
                result=[
                    AIMessage(
                        content="first",
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 1,
                            "total_tokens": 11,
                        },
                    )
                ]
            ),
            ModelResponse(
                result=[
                    AIMessage(
                        content="second",
                        usage_metadata={
                            "input_tokens": 50,
                            "output_tokens": 2,
                            "total_tokens": 52,
                        },
                    )
                ]
            ),
        ]
    )

    async def _handler(_request):
        return next(responses)

    request = _request(messages=[HumanMessage(content="hello")])
    asyncio.run(middleware.awrap_model_call(request, _handler))
    assert agent.get_session_status()["last_request_estimate_available"] is True

    asyncio.run(middleware.awrap_model_call(request, _handler))
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 2
    assert status["last_request_estimate_available"] is False
    assert status["last_estimated_input_tokens"] is None
    assert status["last_actual_input_tokens"] is None
    assert status["last_estimate_error_tokens"] is None
    assert status["model"] == "small-model"
    assert status["context_window_tokens"] == 4096
    assert status["last_input_tokens"] == 50


def test_request_budget_estimator_failure_reports_empty_snapshot_and_calls_model():
    """估算器异常应清除旧观测状态，并继续原模型调用。"""
    budgets = []
    middleware = UsageMiddleware(on_request_budget=budgets.append)
    request = _request(messages=[HumanMessage(content="hello")])
    response = ModelResponse(result=[AIMessage(content="ok")])

    async def _handler(_request):
        return response

    with patch.object(
        UsageMiddleware,
        "estimate_request",
        side_effect=RuntimeError("request content must not reach the snapshot"),
    ):
        result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert result is response
    assert budgets == [
        {
            "request_sequence": 1,
            "has_estimate": False,
            "model": "small-model",
            "context_window_tokens": 4096,
        }
    ]


def test_request_budget_metadata_failure_still_calls_model():
    """模型元数据属性异常不得让预算观察器阻断真实模型调用。"""

    class _BrokenMetadataModel:
        @property
        def model(self):
            raise RuntimeError("model metadata unavailable")

        @property
        def model_name(self):
            raise RuntimeError("model metadata unavailable")

        @property
        def model_id(self):
            raise RuntimeError("model metadata unavailable")

        @property
        def profile(self):
            raise RuntimeError("profile metadata unavailable")

    budgets = []
    middleware = UsageMiddleware(on_request_budget=budgets.append)
    request = ModelRequest(
        model=_BrokenMetadataModel(),
        messages=[HumanMessage(content="hello")],
        tools=[],
        state={},
        runtime=None,
    )
    response = ModelResponse(result=[AIMessage(content="ok")])
    handled = []

    async def _handler(received):
        handled.append(received)
        return response

    with patch.object(
        UsageMiddleware,
        "estimate_request",
        side_effect=RuntimeError("estimate unavailable"),
    ):
        result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert result is response
    assert handled == [request]
    assert budgets == [
        {
            "request_sequence": 1,
            "has_estimate": False,
            "model": None,
            "context_window_tokens": None,
        }
    ]


def test_request_sequence_callback_failure_still_calls_model():
    """会话序号分配异常时应放弃最近快照竞争，并继续真实模型调用。"""
    budgets = []

    def _raise_sequence():
        raise RuntimeError("sequence unavailable")

    middleware = UsageMiddleware(
        on_request_budget=budgets.append,
        next_request_sequence=_raise_sequence,
    )
    request = _request(messages=[HumanMessage(content="hello")])
    response = ModelResponse(result=[AIMessage(content="ok")])
    handled = []

    async def _handler(received):
        handled.append(received)
        return response

    result = asyncio.run(middleware.awrap_model_call(request, _handler))

    assert result is response
    assert handled == [request]
    assert budgets[0]["request_sequence"] is None


def test_request_budget_and_actual_usage_share_one_request_sequence():
    """真实 usage 只能校准同一次成功模型调用产生的估算。"""
    budgets = []
    usages = []
    middleware = UsageMiddleware(
        on_request_budget=budgets.append,
        on_usage=usages.append,
    )
    request = _request(messages=[HumanMessage(content="hello")])
    response = ModelResponse(
        result=[
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 23,
                    "output_tokens": 4,
                    "total_tokens": 27,
                },
            )
        ]
    )

    async def _handler(_request):
        return response

    asyncio.run(middleware.awrap_model_call(request, _handler))

    assert budgets[0]["request_sequence"] == 1
    assert usages[0]["request_sequence"] == 1
    assert usages[0]["request_budget_recorded"] is True
    assert usages[0]["input_usage_available"] is True
    assert usages[0]["estimated_input_tokens"] == budgets[0]["estimated_input_tokens"]


def test_partial_usage_without_input_does_not_calibrate_request_estimate():
    """仅有输出 usage 时，不得把缺失的真实输入误报为零。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    middleware = UsageMiddleware(
        on_request_budget=agent._record_request_budget,
        on_usage=agent._record_usage,
    )
    request = _request(messages=[HumanMessage(content="hello")])
    response = ModelResponse(
        result=[
            AIMessage(
                content="ok",
                response_metadata={
                    "token_usage": {
                        "completion_tokens": 7,
                        "total_tokens": 7,
                    }
                },
            )
        ]
    )

    async def _handler(_request):
        return response

    asyncio.run(middleware.awrap_model_call(request, _handler))
    status = agent.get_session_status()

    assert status["last_request_estimate_available"] is True
    assert status["last_input_usage_available"] is False
    assert status["last_input_tokens"] is None
    assert status["last_output_tokens"] == 7
    assert status["last_actual_input_tokens"] is None
    assert status["last_estimate_error_tokens"] is None
    assert status["last_estimate_error_ratio"] is None
    assert status["last_context_usage_ratio"] is None
    status_text = MessageChain._format_session_status_text(status)
    assert "最近一次上下文占用: 未知 / 4,096" in status_text
    assert "最近一次 tokens: 输入 未知 / 输出 7 / 总计 7" in status_text


def test_missing_usage_clears_previous_last_call_values():
    """本轮没有 usage 时，最近一次状态不得保留上一轮实际值。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    agent._record_request_budget(
        {
            "request_sequence": 1,
            "has_estimate": True,
            "estimated_input_tokens": 10,
            "context_window_tokens": 1000,
        }
    )
    agent._record_usage(
        {
            "request_sequence": 1,
            "request_budget_recorded": True,
            "has_usage": True,
            "input_usage_available": True,
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "cache_usage_available": True,
            "cache_read_input_tokens": 4,
            "cache_write_input_tokens": 0,
            "uncached_input_tokens": 8,
        }
    )
    agent._record_request_budget(
        {
            "request_sequence": 2,
            "has_estimate": True,
            "estimated_input_tokens": 20,
            "context_window_tokens": 1000,
        }
    )
    agent._record_usage(
        {
            "request_sequence": 2,
            "request_budget_recorded": True,
            "has_usage": False,
            "input_usage_available": False,
        }
    )

    status = agent.get_session_status()

    assert status["last_request_sequence"] == 2
    assert status["last_request_estimate_available"] is True
    assert status["last_input_usage_available"] is False
    assert status["last_input_tokens"] is None
    assert status["last_output_tokens"] is None
    assert status["last_total_tokens"] is None
    assert status["last_context_usage_ratio"] is None
    assert status["last_cache_usage_available"] is False
    assert status["total_input_tokens"] == 12
    assert status["total_output_tokens"] == 3
    assert status["total_tokens"] == 15
    status_text = MessageChain._format_session_status_text(status)
    assert "最近一次 tokens: 输入 未知 / 输出 未知 / 总计 未知" in status_text
    assert "最近一次缓存:" not in status_text
    assert "当前会话累计缓存: 命中 4 / 写入 0 / 未命中 8" in status_text


def test_out_of_order_responses_keep_latest_request_snapshot():
    """较早请求晚返回时，只累计 usage，不得覆盖较新请求的最近状态。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    middleware = UsageMiddleware(
        on_request_budget=agent._record_request_budget,
        on_usage=agent._record_usage,
    )
    first_request = _request(
        messages=[HumanMessage(content="first")],
        max_input_tokens=1000,
    )
    first_request.model.model = "first-model"
    second_request = _request(
        messages=[HumanMessage(content="second")],
        max_input_tokens=2000,
    )
    second_request.model.model = "second-model"
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _handler(request):
        if request.model.model == "first-model":
            first_started.set()
            await release_first.wait()
            return ModelResponse(
                result=[
                    AIMessage(
                        content="first",
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 1,
                            "total_tokens": 11,
                        },
                    )
                ]
            )
        return ModelResponse(
            result=[
                AIMessage(
                    content="second",
                    usage_metadata={
                        "input_tokens": 50,
                        "output_tokens": 2,
                        "total_tokens": 52,
                    },
                )
            ]
        )

    async def _run_out_of_order():
        first_task = asyncio.create_task(
            middleware.awrap_model_call(first_request, _handler)
        )
        await first_started.wait()
        await middleware.awrap_model_call(second_request, _handler)
        release_first.set()
        await first_task

    asyncio.run(_run_out_of_order())
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 2
    assert status["model"] == "second-model"
    assert status["context_window_tokens"] == 2000
    assert status["last_input_tokens"] == 50
    assert status["last_output_tokens"] == 2
    assert status["last_total_tokens"] == 52
    assert status["last_actual_input_tokens"] == 50
    assert status["total_input_tokens"] == 60
    assert status["total_output_tokens"] == 3
    assert status["total_tokens"] == 63
    assert status["model_call_count"] == 2


def test_sequence_failure_cannot_overwrite_next_request_snapshot():
    """无序号请求晚返回时只能累计 usage，不能覆盖后续有序请求。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    allocation_count = 0

    def _next_sequence():
        nonlocal allocation_count
        allocation_count += 1
        if allocation_count == 1:
            raise RuntimeError("sequence unavailable")
        return agent._next_request_sequence()

    middleware = UsageMiddleware(
        on_request_budget=agent._record_request_budget,
        on_usage=agent._record_usage,
        next_request_sequence=_next_sequence,
    )
    first_request = _request(
        messages=[HumanMessage(content="first")],
        max_input_tokens=1000,
    )
    first_request.model.model = "first-model"
    second_request = _request(
        messages=[HumanMessage(content="second")],
        max_input_tokens=2000,
    )
    second_request.model.model = "second-model"
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _handler(request):
        if request.model.model == "first-model":
            first_started.set()
            await release_first.wait()
            input_tokens = 10
        else:
            input_tokens = 50
        return ModelResponse(
            result=[
                AIMessage(
                    content=request.model.model,
                    usage_metadata={
                        "input_tokens": input_tokens,
                        "output_tokens": 1,
                        "total_tokens": input_tokens + 1,
                    },
                )
            ]
        )

    async def _run_out_of_order():
        first_task = asyncio.create_task(
            middleware.awrap_model_call(first_request, _handler)
        )
        await first_started.wait()
        await middleware.awrap_model_call(second_request, _handler)
        release_first.set()
        await first_task

    asyncio.run(_run_out_of_order())
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 1
    assert status["model"] == "second-model"
    assert status["context_window_tokens"] == 2000
    assert status["last_input_tokens"] == 50
    assert status["last_actual_input_tokens"] == 50
    assert status["total_input_tokens"] == 60
    assert status["model_call_count"] == 2


def test_failed_model_switch_keeps_model_and_window_from_same_request():
    """新模型请求即使失败，状态也不得混合旧模型名称与新窗口。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    agent._sync_model_profile(
        SimpleNamespace(model="large-model", profile={"max_input_tokens": 128000})
    )
    middleware = UsageMiddleware(
        on_request_budget=agent._record_request_budget,
        on_usage=agent._record_usage,
    )
    request = _request(
        messages=[HumanMessage(content="small")],
        max_input_tokens=2048,
    )
    request.model.model = "small-model"

    async def _failing_handler(_request):
        raise RuntimeError("model unavailable")

    async def _run_failure():
        try:
            await middleware.awrap_model_call(request, _failing_handler)
        except RuntimeError:
            pass

    asyncio.run(_run_failure())
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 1
    assert status["model"] == "small-model"
    assert status["context_window_tokens"] == 2048


def test_unknown_model_name_is_not_replaced_after_request_snapshot():
    """请求已开始后，未知模型名称不得与配置默认值拼成虚假快照。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    agent._record_request_budget(
        {
            "has_estimate": True,
            "request_sequence": 1,
            "model": None,
            "estimated_input_tokens": 100,
            "context_window_tokens": 2048,
        }
    )

    status = agent.get_session_status()

    assert status["model"] is None
    assert status["context_window_tokens"] == 2048


def test_request_sequence_remains_monotonic_after_agent_graph_rebuild():
    """重建 Agent 图后，新观察器也必须延续当前会话的请求顺序。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    first_graph = UsageMiddleware(
        on_request_budget=agent._record_request_budget,
        on_usage=agent._record_usage,
        next_request_sequence=agent._next_request_sequence,
    )
    rebuilt_graph = UsageMiddleware(
        on_request_budget=agent._record_request_budget,
        on_usage=agent._record_usage,
        next_request_sequence=agent._next_request_sequence,
    )

    async def _handler(request):
        return ModelResponse(
            result=[
                AIMessage(
                    content=request.model.model,
                    usage_metadata={
                        "input_tokens": request.model.profile["max_input_tokens"] // 10,
                        "output_tokens": 1,
                        "total_tokens": request.model.profile["max_input_tokens"] // 10 + 1,
                    },
                )
            ]
        )

    first = _request(
        messages=[HumanMessage(content="first")],
        max_input_tokens=1000,
    )
    first.model.model = "first-model"
    second = _request(
        messages=[HumanMessage(content="second")],
        max_input_tokens=2000,
    )
    second.model.model = "second-model"

    asyncio.run(first_graph.awrap_model_call(first, _handler))
    asyncio.run(rebuilt_graph.awrap_model_call(second, _handler))
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 2
    assert status["model"] == "second-model"
    assert status["context_window_tokens"] == 2000
    assert status["last_input_tokens"] == 200


def test_new_request_estimate_clears_stale_calibration_until_success():
    """新请求失败时不得沿用上一轮估算误差。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    first = {
        "has_estimate": True,
        "request_sequence": 1,
        "estimated_input_tokens": 100,
        "context_window_tokens": 1000,
    }
    second = {
        "has_estimate": True,
        "request_sequence": 2,
        "estimated_input_tokens": 200,
        "context_window_tokens": 1000,
    }

    agent._record_request_budget(first)
    agent._record_usage(
        {
            "request_sequence": 1,
            "request_budget_recorded": True,
            "input_usage_available": True,
            "estimated_input_tokens": 100,
            "has_usage": True,
            "input_tokens": 120,
            "output_tokens": 1,
            "total_tokens": 121,
        }
    )
    assert agent.get_session_status()["last_estimate_error_tokens"] == 20

    agent._record_request_budget(second)
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 2
    assert status["last_actual_input_tokens"] is None
    assert status["last_estimate_error_tokens"] is None
    assert status["last_estimate_error_ratio"] is None


def test_failed_estimate_clears_previous_request_components():
    """估算失败后状态不得继续展示上一轮的请求组成。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    agent._record_request_budget(
        {
            "has_estimate": True,
            "request_sequence": 1,
            "estimated_input_tokens": 100,
            "message_tokens": 70,
            "system_tokens": 20,
            "tool_tokens": 10,
            "message_count": 3,
            "tool_count": 2,
        }
    )

    agent._record_request_budget(
        {"has_estimate": False, "request_sequence": 2}
    )
    status = agent.get_session_status()

    assert status["last_request_sequence"] == 2
    assert status["last_request_estimate_available"] is False
    assert status["last_estimated_input_tokens"] is None
    assert status["last_estimated_message_tokens"] is None
    assert status["last_estimated_system_tokens"] is None
    assert status["last_estimated_tool_tokens"] is None
    assert status["last_message_count"] == 0
    assert status["last_tool_count"] == 0


def test_new_request_with_unknown_window_clears_previous_model_window():
    """切换到窗口未知的模型后，不得继续展示上一模型的窗口。"""
    agent = MoviePilotAgent(session_id="request-budget", user_id="user-1")
    agent._sync_model_profile(
        SimpleNamespace(model="large-model", profile={"max_input_tokens": 128000})
    )
    agent._record_request_budget(
        {
            "has_estimate": True,
            "request_sequence": 1,
            "estimated_input_tokens": 100,
            "context_window_tokens": 128000,
        }
    )

    agent._sync_model_profile(
        SimpleNamespace(model="unknown-window-model", profile={"max_input_tokens": 0})
    )
    agent._record_request_budget(
        {
            "has_estimate": True,
            "request_sequence": 2,
            "estimated_input_tokens": 100,
            "context_window_tokens": None,
        }
    )
    status = agent.get_session_status()

    assert status["model"] == "unknown-window-model"
    assert status["context_window_tokens"] is None
    assert status["last_estimated_input_ratio"] is None
    assert status["last_estimated_over_input_limit"] is None


def test_real_agent_observer_sees_dynamic_system_and_selected_tools():
    """末尾观察器必须看到动态 system 和 ToolSelector 最终保留的工具。"""

    @tool
    def kept_tool(value: str) -> str:
        """应保留的测试工具。"""
        return value

    @tool
    def removed_tool(value: str) -> str:
        """应被筛除的测试工具。"""
        return value

    selection_model = _ToolBindingFakeModel(
        responses=[AIMessage(content='{"tools": []}')]
    )
    main_model = _ToolBindingFakeModel(responses=[AIMessage(content="done")])
    budgets = []
    graph = create_agent(
        model=main_model,
        tools=[kept_tool, removed_tool],
        system_prompt="base",
        middleware=[
            _DynamicSystemMiddleware(),
            ToolSelectorMiddleware(
                model=selection_model,
                selection_tools=[kept_tool, removed_tool],
                max_tools=1,
                always_include=["kept_tool"],
            ),
            UsageMiddleware(on_request_budget=budgets.append),
        ],
    )

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="hello")]}))

    assert len(budgets) == 1
    assert budgets[0]["tool_count"] == 1
    assert budgets[0]["system_tokens"] > 100
    assert main_model.bound_tool_names == ["kept_tool"]
