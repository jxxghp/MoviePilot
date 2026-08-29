"""动态注入 Agent 根层运行时配置的中间件。"""

from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

from app.agent.middleware.utils import append_to_system_message
from app.agent.runtime import agent_runtime_manager


class RuntimeConfigMiddleware(AgentMiddleware[dict, ContextT, ResponseT]):  # noqa
    """在每次模型调用前动态加载运行时配置。

    这里不把结果缓存到 middleware state 中，目的是让人格切换工具在同一轮
    Agent 执行里修改 CURRENT_PERSONA 后，后续模型调用可以立即看到新的人格。
    """

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:  # noqa
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
        return await handler(self.modify_request(request))


__all__ = ["RuntimeConfigMiddleware"]
