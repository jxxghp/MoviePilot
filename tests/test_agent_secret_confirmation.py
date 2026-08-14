"""Agent 敏感系统设置读取的宿主确认测试。"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import MoviePilotAgent, ReplyMode, agent_manager
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool
from app.schemas.types import MessageChannel


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """允许 LangChain 为固定响应假模型绑定本地工具。"""

    def bind_tools(self, tools, **kwargs):
        """保留固定响应行为，仅声明测试模型支持工具绑定。"""
        return self


def _policy_context(agent_context: dict) -> ToolPolicyContext:
    """构造可注入确认处理器的交互式策略上下文。"""
    return ToolPolicyContext(
        session_id="session-secret",
        user_id="user-secret",
        origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.CHANNEL,
        agent_context=agent_context,
        channel=MessageChannel.Telegram.value,
        source="telegram-test",
    )


def test_after_model_pauses_secret_setting_read_before_tool_node() -> None:
    """首次敏感读取必须结束当前图执行，并闭合整批 tool call。"""
    tool = QuerySystemSettingsTool(
        session_id="session-secret",
        user_id="user-secret",
    )
    confirmation_handler = AsyncMock(return_value="请回复“确认”继续，回复“取消”放弃。")
    middleware = AgentPolicyMiddleware(
        context=_policy_context(
            {"secret_confirmation_handler": confirmation_handler}
        ),
        tools=[tool],
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool.name,
                        "args": {
                            "setting_key": "TMDB_API_KEY",
                            "show_secrets": True,
                        },
                        "id": "secret-call",
                    },
                    {
                        "name": "query_schedulers",
                        "args": {},
                        "id": "ordinary-call",
                    },
                ],
            )
        ]
    }

    result = asyncio.run(middleware.aafter_model(state, runtime=None))

    assert result["jump_to"] == "end"
    assert isinstance(result["messages"][-1], AIMessage)
    assert "确认" in result["messages"][-1].content
    confirmation_handler.assert_awaited_once()
    assert confirmation_handler.await_args.args[0] is tool
    assert confirmation_handler.await_args.args[1]["show_secrets"] is True


def test_after_model_keeps_redacted_setting_read_on_normal_tool_path() -> None:
    """普通设置读取不得增加确认步骤。"""
    tool = QuerySystemSettingsTool(
        session_id="session-secret",
        user_id="user-secret",
    )
    confirmation_handler = AsyncMock()
    middleware = AgentPolicyMiddleware(
        context=_policy_context(
            {"secret_confirmation_handler": confirmation_handler}
        ),
        tools=[tool],
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool.name,
                        "args": {
                            "setting_key": "TMDB_API_KEY",
                            "show_secrets": False,
                        },
                        "id": "redacted-call",
                    }
                ],
            )
        ]
    }

    assert asyncio.run(middleware.aafter_model(state, runtime=None)) is None
    confirmation_handler.assert_not_awaited()


def test_real_agent_graph_stops_before_secret_tool_execution() -> None:
    """真实 Agent 图必须在 ToolNode 前结束，不得执行敏感读取。"""
    tool = QuerySystemSettingsTool(
        session_id="session-secret",
        user_id="user-secret",
    )
    confirmation_handler = AsyncMock(return_value="请回复“确认”继续。")
    context = _policy_context(
        {"secret_confirmation_handler": confirmation_handler}
    )
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool.name,
                        "args": {
                            "setting_key": "TMDB_API_KEY",
                            "show_secrets": True,
                        },
                        "id": "secret-call",
                    }
                ],
            )
        ]
    )
    graph = create_agent(
        model=model,
        tools=[tool],
        middleware=[AgentPolicyMiddleware(context=context, tools=[tool])],
    )

    with patch.object(
        QuerySystemSettingsTool,
        "_load_setting_value",
        side_effect=AssertionError("敏感工具不应执行"),
    ) as load_value:
        result = asyncio.run(
            graph.ainvoke({"messages": [HumanMessage(content="读取密钥")]})
        )

    load_value.assert_not_called()
    confirmation_handler.assert_awaited_once()
    assert isinstance(result["messages"][-1], AIMessage)
    assert "确认" in result["messages"][-1].content


def test_confirm_executes_once_without_model_or_history() -> None:
    """确认应直接执行冻结参数，结果不经过模型和 Agent 历史。"""
    secret_marker = "confirmed-secret-marker"
    protected_output = []
    ordinary_output = []
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        output_callback=ordinary_output.append,
        protected_output_callback=protected_output.append,
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_message_attr(
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
    )
    tool.set_agent_context(agent._tool_context)

    async def scenario() -> tuple[str, str]:
        prompt = await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        result = await agent.process("确认")
        return prompt, result

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(agent, "_execute_agent", new=AsyncMock()) as execute_agent,
        patch.object(agent, "_save_display_history_messages") as save_display,
        patch("app.agent.orchestrator.memory_manager.save_agent_messages") as save_messages,
        patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value=secret_marker,
        ) as load_value,
    ):
        prompt, result = asyncio.run(scenario())

    assert "确认" in prompt
    assert ordinary_output == [prompt]
    assert result == "敏感设置确认已处理。"
    assert len(protected_output) == 1
    assert secret_marker in protected_output[0]
    load_value.assert_called_once()
    execute_agent.assert_not_awaited()
    save_display.assert_not_called()
    save_messages.assert_not_called()
    assert agent.has_pending_secret_confirmation() is False


def test_cancel_clears_pending_without_executing_tool() -> None:
    """取消只消费当前 pending，不读取任何设置值。"""
    protected_output = []
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        protected_output_callback=protected_output.append,
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("取消")

    with patch.object(
        agent,
        "_is_system_admin_context",
        new=AsyncMock(return_value=True),
    ), patch.object(
        QuerySystemSettingsTool,
        "_load_setting_value",
    ) as load_value:
        result = asyncio.run(scenario())

    assert result == "已取消敏感设置读取。"
    assert protected_output == ["已取消敏感设置读取。"]
    load_value.assert_not_called()
    assert agent.has_pending_secret_confirmation() is False


def test_expired_confirmation_reaches_agent_expiry_receipt() -> None:
    """入口不得提前清除过期 pending，否则确认文本会被误送给模型。"""
    protected_output = []
    agent = MoviePilotAgent(
        session_id="session-expired-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        protected_output_callback=protected_output.append,
    )
    tool = QuerySystemSettingsTool(session_id=agent.session_id, user_id="1")

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        agent._pending_secret_confirmation.created_at = (
            datetime.now() - timedelta(minutes=6)
        )
        agent_manager.active_agents[agent.session_id] = agent
        try:
            assert agent_manager.matches_secret_confirmation(
                agent.session_id,
                "1",
                channel=MessageChannel.WebAgent.value,
                source="web-agent",
            )
            return await agent.process("确认")
        finally:
            agent_manager.active_agents.pop(agent.session_id, None)

    with patch.object(
        agent,
        "_is_system_admin_context",
        new=AsyncMock(return_value=True),
    ), patch.object(agent, "_execute_agent", new=AsyncMock()) as execute_agent:
        result = asyncio.run(scenario())

    assert result == "敏感设置读取确认已过期，请重新发起。"
    assert protected_output == [result]
    execute_agent.assert_not_awaited()


def test_background_agent_refuses_secret_read_without_pending() -> None:
    """后台 Agent 没有用户确认通道时必须直接拒绝明文读取。"""
    agent = MoviePilotAgent(session_id="background-secret", user_id="system")
    tool = QuerySystemSettingsTool(session_id="background-secret", user_id="system")

    async def scenario() -> str:
        context = await agent._build_tool_context(should_dispatch_reply=False)
        tool.set_agent_context(context)
        return await tool.run(setting_key="TMDB_API_KEY", show_secrets=True)

    with patch.object(QuerySystemSettingsTool, "_load_setting_value") as load_value:
        result = asyncio.run(scenario())

    assert "确认" in result
    load_value.assert_not_called()


def test_message_channel_receives_confirmation_prompt_once() -> None:
    """TG/飞书应先向用户私聊发送提示，再登记待确认操作。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="admin",
        original_chat_id="chat-1",
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")

    async def scenario() -> str:
        return await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )

    with patch.object(
        agent,
        "_is_system_admin_context",
        new=AsyncMock(return_value=True),
    ), patch.object(
        agent,
        "_deliver_private_channel_message",
        new=AsyncMock(return_value=True),
    ) as send_message:
        prompt = asyncio.run(scenario())

    send_message.assert_awaited_once_with(prompt)
    assert agent._tool_context["user_reply_sent"] is True
    assert agent.has_pending_secret_confirmation() is True


def test_message_channel_does_not_register_pending_when_private_delivery_fails() -> None:
    """无法建立私聊时不得等待确认，更不能回退群聊投递结果。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Feishu.value,
        source="feishu-main",
        username="admin",
        original_chat_id="group-1",
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(
            agent,
            "_deliver_private_channel_message",
            new=AsyncMock(return_value=False),
        ) as deliver,
    ):
        result = asyncio.run(
            agent._register_secret_confirmation(
                tool,
                {"setting_key": "TMDB_API_KEY", "show_secrets": True},
            )
        )

    assert result == "无法向当前用户建立私聊，未执行敏感设置读取。"
    deliver.assert_awaited_once()
    assert agent.has_pending_secret_confirmation() is False


def test_private_delivery_requests_literal_plain_text() -> None:
    """敏感提示与结果均须请求渠道按纯文本私聊投递。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="admin",
        original_chat_id="group-1",
    )
    response = SimpleNamespace(success=True)

    with patch(
        "app.agent.orchestrator.AgentChain.send_direct_message",
        return_value=response,
    ) as send_direct:
        delivered = asyncio.run(
            agent._deliver_private_channel_message(
                "G2A1_PROTECTED_MARKER_20260812\n**literal markdown**\n<img src=x>"
            )
        )

    assert delivered is True
    notification = send_direct.call_args.args[0]
    assert notification.private_delivery is True
    assert notification.parse_mode == "plain"
    assert notification.original_chat_id is None


def test_pending_secret_read_keeps_actor_and_action_across_chat_targets() -> None:
    """新请求不得覆盖 pending，同一用户可从私聊消费群聊发起的确认。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="admin",
        original_chat_id="chat-1",
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)
    delivered = []

    async def scenario() -> tuple[str, str, str]:
        first = await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        second = await agent._register_secret_confirmation(
            tool,
            {"setting_key": "API_TOKEN", "show_secrets": True},
        )
        agent.original_chat_id = "chat-2"
        result = await agent.process("确认")
        return first, second, result

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(agent, "_execute_agent", new=AsyncMock(return_value="普通回复")),
        patch.object(
            agent,
            "_deliver_private_channel_message",
            new=AsyncMock(side_effect=lambda content: delivered.append(content) or True),
        ),
        patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value="secret-marker",
        ) as load_value,
    ):
        first, second, result = asyncio.run(scenario())

    assert "TMDB_API_KEY" in first
    assert "已有待确认" in second
    assert result == "敏感设置确认已处理。"
    load_value.assert_called_once()
    assert "secret-marker" in delivered[-1]
    assert agent.has_pending_secret_confirmation() is False


def test_confirm_reports_result_delivery_failure_without_secret() -> None:
    """工具已读取但受保护结果未送达时，只能通过普通渠道报告非敏感失败。"""
    secret_marker = "confirmed-secret-marker"
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="admin",
        original_chat_id="group-1",
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("确认")

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(
            agent,
            "_deliver_private_channel_message",
            new=AsyncMock(side_effect=[True, False]),
        ) as deliver,
        patch.object(agent, "send_agent_message", new=AsyncMock()) as send_notice,
        patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value=secret_marker,
        ),
    ):
        result = asyncio.run(scenario())

    assert result == "敏感设置读取已完成，但结果投递失败，请重新发起。"
    assert deliver.await_count == 2
    assert secret_marker in deliver.await_args_list[-1].args[0]
    send_notice.assert_awaited_once_with(result)
    assert secret_marker not in send_notice.await_args.args[0]


def test_web_protected_callback_failure_returns_ordinary_safe_notice() -> None:
    """Web 受保护回调失败时，普通流只能收到不含敏感结果的提示。"""
    secret_marker = "confirmed-secret-marker"
    ordinary_output = []

    def broken_protected_callback(_content: str) -> None:
        raise RuntimeError("delivery unavailable")

    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        output_callback=ordinary_output.append,
        protected_output_callback=broken_protected_callback,
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        ordinary_output.clear()
        return await agent.process("确认")

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value=secret_marker,
        ),
    ):
        result = asyncio.run(scenario())

    assert result == "敏感设置读取已完成，但结果投递失败，请重新发起。"
    assert ordinary_output == [result]
    assert secret_marker not in ordinary_output[0]


def test_confirm_reuses_policy_lifecycle() -> None:
    """确认后的冻结调用必须生成与 ToolNode 相同的 start/finish 生命周期。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        protected_output_callback=lambda _content: None,
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("确认")

    from app.agent.policy import DEFAULT_TOOL_POLICY_ORCHESTRATOR

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(QuerySystemSettingsTool, "_load_setting_value", return_value="secret"),
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "start",
            wraps=DEFAULT_TOOL_POLICY_ORCHESTRATOR.start,
        ) as start,
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "finish",
            wraps=DEFAULT_TOOL_POLICY_ORCHESTRATOR.finish,
        ) as finish,
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "fail",
            wraps=DEFAULT_TOOL_POLICY_ORCHESTRATOR.fail,
        ) as fail,
    ):
        result = asyncio.run(scenario())

    assert result == "敏感设置确认已处理。"
    start.assert_called_once()
    finish.assert_called_once()
    fail.assert_not_called()


def test_confirm_respects_policy_denial_without_running_tool() -> None:
    """确认不能覆盖宿主策略的拒绝决定。"""
    protected_output = []
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        protected_output_callback=protected_output.append,
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)
    denied = SimpleNamespace(decision=SimpleNamespace(allowed=False))

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("确认")

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch("app.agent.middleware.policy.DEFAULT_TOOL_POLICY_ORCHESTRATOR.start", return_value=denied),
        patch.object(QuerySystemSettingsTool, "_run_confirmed", new=AsyncMock()) as run_tool,
    ):
        result = asyncio.run(scenario())

    assert result == "敏感设置确认已处理。"
    assert protected_output[-1] == "当前宿主策略不允许执行该工具。"
    run_tool.assert_not_awaited()


def test_policy_denial_delivery_failure_reports_not_executed() -> None:
    """策略拒绝无法私聊送达时，普通提示不得把未执行操作报告为完成。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="admin",
        original_chat_id="group-1",
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)
    denied = SimpleNamespace(decision=SimpleNamespace(allowed=False))

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("确认")

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(
            agent,
            "_deliver_private_channel_message",
            new=AsyncMock(side_effect=[True, False]),
        ),
        patch.object(agent, "send_agent_message", new=AsyncMock()) as send_notice,
        patch(
            "app.agent.middleware.policy.DEFAULT_TOOL_POLICY_ORCHESTRATOR.start",
            return_value=denied,
        ),
        patch.object(QuerySystemSettingsTool, "_run_confirmed", new=AsyncMock()) as run_tool,
    ):
        result = asyncio.run(scenario())

    assert result == "当前宿主策略不允许执行该工具。"
    send_notice.assert_awaited_once_with(result)
    run_tool.assert_not_awaited()


def test_confirm_records_policy_failure_and_returns_protected_error() -> None:
    """确认执行异常必须闭合 fail 生命周期，且不把异常交给普通对话。"""
    protected_output = []
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
        username="admin",
        replay_mode=ReplyMode.CAPTURE_ONLY,
        protected_output_callback=protected_output.append,
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)
    orchestrator = MagicMock()
    orchestrator.start.return_value = SimpleNamespace(
        decision=SimpleNamespace(allowed=True)
    )

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("确认")

    from app.agent.policy import DEFAULT_TOOL_POLICY_ORCHESTRATOR

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "start",
            return_value=orchestrator.start.return_value,
        ),
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "fail",
            side_effect=orchestrator.fail,
        ) as fail,
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "finish",
            side_effect=orchestrator.finish,
        ) as finish,
        patch.object(
            QuerySystemSettingsTool,
            "_run_confirmed",
            new=AsyncMock(side_effect=RuntimeError("secret-bearing-error")),
        ),
    ):
        result = asyncio.run(scenario())

    assert result == "敏感设置读取失败，请稍后重试。"
    assert protected_output[-1] == result
    fail.assert_called_once()
    finish.assert_not_called()


def test_execution_failure_delivery_failure_reports_safe_notice() -> None:
    """执行和私聊投递同时失败时，普通渠道仍须收到不含敏感信息的提示。"""
    agent = MoviePilotAgent(
        session_id="session-secret",
        user_id="1",
        channel=MessageChannel.Telegram.value,
        source="telegram-main",
        username="admin",
        original_chat_id="group-1",
    )
    tool = QuerySystemSettingsTool(session_id="session-secret", user_id="1")
    tool.set_agent_context(agent._tool_context)

    async def scenario() -> str:
        await agent._register_secret_confirmation(
            tool,
            {"setting_key": "TMDB_API_KEY", "show_secrets": True},
        )
        return await agent.process("确认")

    with (
        patch.object(agent, "_is_system_admin_context", new=AsyncMock(return_value=True)),
        patch.object(
            agent,
            "_deliver_private_channel_message",
            new=AsyncMock(side_effect=[True, False]),
        ),
        patch.object(agent, "send_agent_message", new=AsyncMock()) as send_notice,
        patch.object(
            QuerySystemSettingsTool,
            "_run_confirmed",
            new=AsyncMock(side_effect=RuntimeError("secret-bearing-error")),
        ),
    ):
        result = asyncio.run(scenario())

    assert result == "敏感设置读取失败，请稍后重试。"
    send_notice.assert_awaited_once_with(result)
    assert "secret-bearing-error" not in send_notice.await_args.args[0]
