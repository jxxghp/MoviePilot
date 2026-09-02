from unittest.mock import MagicMock

import pytest

from app.agent.manager import AgentManager
from app.agent.orchestrator import MoviePilotAgent
from app.agent.session import _MessageTask


@pytest.mark.anyio
async def test_custom_moviepilot_agent_factory_receives_runtime_context() -> None:
    """Web/OpenAI Agent 子类必须复用 manager 装配的数据与记忆上下文。"""
    data = MagicMock()
    memory = MagicMock()
    manager = AgentManager(data=data, memory=memory)

    class CustomMoviePilotAgent(MoviePilotAgent):
        async def process(self, message: str, **kwargs: object) -> str:
            return message

    result = await manager._process_message_internal(
        _MessageTask(
            session_id="custom-context",
            user_id="1",
            message="ok",
            agent_factory=CustomMoviePilotAgent,
        )
    )

    agent = manager.active_agents["custom-context"]
    assert result == "ok"
    assert agent._data is data
    assert agent._memory is memory
