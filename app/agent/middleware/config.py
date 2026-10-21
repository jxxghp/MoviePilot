"""
动态注入 Agent 根层运行时配置的中间件。
"""

import asyncio
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

from app.agent.middleware.utils import append_to_system_message
from app.agent.runtime import agent_runtime_manager
from app.runtime.settings import get_runtime_setting


class RuntimeConfigMiddleware(AgentMiddleware[AgentState, ContextT, ResponseT]):  # noqa
    """
    在每次模型调用前动态加载运行时配置。

    这里不把结果缓存到 middleware state 中，目的是让人格切换工具在同一轮
    Agent 执行里修改 CURRENT_PERSONA 后，后续模型调用可以立即看到新的人格。
    """

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:  # noqa
        """
        即时加载人格及附加上下文，并返回追加系统消息后的请求副本。
        """
        runtime_config = agent_runtime_manager.load_runtime_config()
        runtime_sections = runtime_config.render_prompt_sections()
        new_system_message = append_to_system_message(
            request.system_message, runtime_sections
        )
        return request.override(system_message=new_system_message)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """
        在异步模型调用前注入当前运行配置，并返回模型执行结果。
        """
        timeout = float(get_runtime_setting("AGENT_LLM_TIMEOUT", 180))
        async with asyncio.timeout(timeout if timeout > 0 else None):
            return await handler(self.modify_request(request))


__all__ = ["RuntimeConfigMiddleware"]
