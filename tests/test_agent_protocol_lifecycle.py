"""兼容协议请求的 AgentManager ownership 与关闭竞态合同。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.security import HTTPAuthorizationCredentials

from app import schemas
from app.api.endpoints import anthropic, openai

_API_TOKEN = "test-agent-protocol-token"


class _ManagerClosedError(RuntimeError):
    """模拟 enqueue 时 manager 已关闭的 acceptance gate 错误。"""

    code = "agent_manager_unavailable"


class _ClosingManager:
    """拒绝新任务并记录请求级清理的 manager 替身。"""

    def __init__(self) -> None:
        self.process_calls = []
        self.clear_calls = []

    async def process_message(self, **kwargs):
        self.process_calls.append(kwargs)
        raise _ManagerClosedError("AgentManager 已关闭")

    async def clear_session(self, **kwargs):
        self.clear_calls.append(kwargs)

    async def stop_current_task(self, _session_id):
        return False


async def _collect(response) -> str:
    """收集 StreamingResponse 的全部文本块。"""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_streaming_protocols_reject_config_disable_before_manager_lookup() -> None:
    """配置关闭后流式请求保持 503，且不得接触运行态 manager。"""
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=_API_TOKEN,
    )
    openai_payload = schemas.OpenAIChatCompletionsRequest(
        messages=[schemas.OpenAIChatMessage(role="user", content="hello")],
        stream=True,
    )
    anthropic_payload = schemas.AnthropicMessagesRequest(
        messages=[schemas.AnthropicMessage(role="user", content="hello")],
        stream=True,
    )

    async def scenario():
        return (
            await openai.chat_completions(
                openai_payload,
                SimpleNamespace(headers={}),
                credentials,
            ),
            await anthropic.messages(
                anthropic_payload,
                x_api_key=_API_TOKEN,
            ),
        )

    runtime_config = SimpleNamespace(
        ai_agent_enable=False,
        api_token=_API_TOKEN,
    )
    with patch.object(
        openai,
        "get_api_runtime_config_snapshot",
        return_value=runtime_config,
    ), patch.object(
        anthropic,
        "get_api_runtime_config_snapshot",
        return_value=runtime_config,
    ), patch.object(
        openai,
        "get_running_agent_manager",
    ) as openai_manager, patch.object(
        anthropic,
        "get_running_agent_manager",
    ) as anthropic_manager:
        responses = asyncio.run(scenario())

    assert [response.status_code for response in responses] == [503, 503]
    openai_manager.assert_not_called()
    anthropic_manager.assert_not_called()


def test_openai_stream_rejects_shutdown_race_and_cleans_request_session() -> None:
    """随机 OpenAI 流在 enqueue 竞态失败时返回协议错误并清理临时会话。"""
    manager = _ClosingManager()
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=_API_TOKEN,
    )
    payload = schemas.OpenAIChatCompletionsRequest(
        messages=[schemas.OpenAIChatMessage(role="user", content="hello")],
        stream=True,
    )

    async def scenario() -> str:
        response = await openai.chat_completions(
            payload,
            SimpleNamespace(headers={}),
            credentials,
        )
        return await _collect(response)

    with patch.object(
        openai,
        "get_api_runtime_config_snapshot",
        return_value=SimpleNamespace(
            ai_agent_enable=True,
            api_token=_API_TOKEN,
        ),
    ), patch.object(
        openai,
        "get_running_agent_manager",
        return_value=manager,
    ):
        body = asyncio.run(scenario())

    assert '"type": "server_error"' in body
    assert "data: [DONE]" in body
    assert len(manager.process_calls) == 1
    assert manager.process_calls[0]["wait_for_completion"] is True
    assert callable(manager.process_calls[0]["agent_setup"])
    assert len(manager.clear_calls) == 1


def test_anthropic_stream_rejects_shutdown_race_and_cleans_request_session() -> None:
    """Anthropic 流在 enqueue 竞态失败时返回 error 终态并清理临时会话。"""
    manager = _ClosingManager()
    payload = schemas.AnthropicMessagesRequest(
        messages=[schemas.AnthropicMessage(role="user", content="hello")],
        stream=True,
    )

    async def scenario() -> str:
        response = await anthropic.messages(
            payload,
                x_api_key=_API_TOKEN,
        )
        return await _collect(response)

    with patch.object(
        anthropic,
        "get_api_runtime_config_snapshot",
        return_value=SimpleNamespace(
            ai_agent_enable=True,
            api_token=_API_TOKEN,
        ),
    ), patch.object(
        anthropic,
        "get_running_agent_manager",
        return_value=manager,
    ):
        body = asyncio.run(scenario())

    assert "event: error" in body
    assert "event: message_stop" in body
    assert len(manager.process_calls) == 1
    assert manager.process_calls[0]["wait_for_completion"] is True
    assert callable(manager.process_calls[0]["agent_setup"])
    assert len(manager.clear_calls) == 1


def test_managed_protocol_request_releases_its_stream_queue() -> None:
    """协议请求完成后不应由持久会话 Agent 继续强引用请求队列。"""
    event_queue = asyncio.Queue()
    created_agents = []

    class ProtocolAgent:
        """记录请求绑定与释放的最小协议 Agent。"""

        def __init__(self, **_kwargs):
            self.collected_messages = ["done"]
            self.bound_queue = None
            created_agents.append(self)

        def configure_protocol_request(self, *, stream_mode, event_queue):
            assert stream_mode is True
            self.bound_queue = event_queue

        def release_protocol_request(self, queue):
            if self.bound_queue is queue:
                self.bound_queue = None

    class RunningManager:
        """在 worker 边界执行 agent_setup 的 manager 替身。"""

        async def process_message(self, **kwargs):
            agent = kwargs["agent_factory"]()
            kwargs["agent_setup"](agent)
            return "done"

    async def scenario():
        with patch.object(
            openai,
            "_get_collecting_agent_type",
            return_value=ProtocolAgent,
        ):
            return await openai._run_managed_agent(
                manager=RunningManager(),
                session_id="persistent",
                user_id="1",
                username="api",
                source="openai",
                prompt="hello",
                images=[],
                stream_mode=True,
                event_queue=event_queue,
            )

    assert asyncio.run(scenario()) == ("done", ["done"])
    assert len(created_agents) == 1
    assert created_agents[0].bound_queue is None
