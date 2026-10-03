"""与前台相同模型、提示词和工具 schema 的隔离复盘，只在派发侧收紧可执行能力。"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agent.learning.prompts import COMBINED_REVIEW_PROMPT, MEMORY_REVIEW_PROMPT, SKILL_REVIEW_PROMPT
from app.agent.learning.tools import LearningTools
from app.agent.learning.usage import ReviewUsage
from app.agent.middleware.summarization import (
    ContextPreservingSummarizationMiddleware,
    FinalRequestCompactionMiddleware,
)
from app.agent.middleware.usage import UsageMiddleware


@dataclass
class ReviewSnapshot:
    """仅复制可序列化请求与消息，复用模型配置而不持有父图、持久化器或工具实例。"""

    model: Any
    system: SystemMessage | None
    messages: list[BaseMessage]
    schemas: list[dict[str, Any]]
    settings: dict[str, Any]
    tool_choice: Any

    @classmethod
    def capture(cls, request: ModelRequest, result: Any) -> 'ReviewSnapshot | None':
        """只接受客户端函数工具，供应商原生工具不能由本地白名单约束时不启动复盘。"""
        schemas = [deepcopy(convert_to_openai_tool(tool)) for tool in request.tools]
        if any(schema.get('type') != 'function' for schema in schemas) or request.response_format is not None:
            return None
        response = result.model_response if isinstance(result, ExtendedModelResponse) else result
        model = request.model.model_copy(update={'streaming': False, 'callbacks': None})
        return cls(model, deepcopy(request.system_message), deepcopy([*request.messages, *response.result]),
                   schemas, deepcopy(request.model_settings or {}), deepcopy(request.tool_choice))


class ReviewLoop:
    """16 次主模型迭代及累计输入预算，首次保留完整缓存前缀，后续压缩状态完全独立。"""

    def __init__(self, snapshot: ReviewSnapshot, tools: LearningTools, *, review_memory: bool,
                 review_skills: bool, cancelled: Callable[[], bool], on_usage: Callable[[dict[str, Any]], None]) -> None:
        """模型输出只进入复盘消息列表，绝不复用前台 output callback 或归档中间件。"""
        self.snapshot, self.tools = snapshot, tools
        self.review_memory, self.cancelled, self.on_usage = review_memory, cancelled, on_usage
        prompt = COMBINED_REVIEW_PROMPT if review_memory and review_skills else MEMORY_REVIEW_PROMPT if review_memory else SKILL_REVIEW_PROMPT
        self.messages = [*deepcopy(snapshot.messages), HumanMessage(content=prompt)]
        window = UsageMiddleware._extract_context_window_tokens(snapshot.model)
        self.budget = min(600_000, max(1, int(window * .75))) if window else 120_000
        self.usage = ReviewUsage(on_usage)
        self.model = snapshot.model.model_copy(update={'callbacks': [self.usage]})
        self.actions: list[dict[str, Any]] = []
        self.compaction = FinalRequestCompactionMiddleware(summarizer=ContextPreservingSummarizationMiddleware(
            model=self.model, trigger=('tokens', int((window or 160_000) * .85)), keep=('messages', 20),
        ))

    @property
    def consumed(self) -> int:
        """主循环和压缩统一使用累计输入预算。"""
        return self.usage.consumed

    async def _invoke(self, request: ModelRequest) -> ModelResponse:
        """使用原 schema 和请求设置绑定模型；真实 usage 归属父会话，未返回 usage 时保守估算。"""
        if self.cancelled() or self.consumed >= self.budget:
            raise RuntimeError('后台复盘已取消')
        model = request.model.bind_tools(self.snapshot.schemas, tool_choice=self.snapshot.tool_choice, **self.snapshot.settings)
        messages = [request.system_message, *request.messages] if request.system_message else list(request.messages)
        response = await model.ainvoke(messages, config={'callbacks': [], 'tags': ['background_review']})
        if not isinstance(response, AIMessage):
            raise ValueError('复盘模型未返回 AIMessage')
        return ModelResponse(result=[response])

    async def _dispatch(self, message: AIMessage) -> None:
        """逐调用校验和记录实际新回执，绝不把父对话中的旧学习工具算作本次成果。"""
        import json  # pylint: disable=import-outside-toplevel
        for call in message.tool_calls:
            if self.cancelled():
                return
            try:
                result = await self.tools.dispatch(call['name'], call['args'], review_memory=self.review_memory)
            except (ValueError, TypeError) as error:
                result = json.dumps(dict(success=False, error=str(error)), ensure_ascii=False)
            self.messages.append(ToolMessage(content=result, tool_call_id=call['id'], name=call['name']))
            payload = json.loads(result)
            if (call['name'] in {'memory', 'skill_manage'} and call['args'].get('action') != 'pending'
                    and payload.get('success') and payload.get('changed', True)):
                self.actions.append(dict(tool=call['name'], kind='proposal' if payload.get('staged') else 'mutation',
                                         arguments=deepcopy(call['args']), result=payload))

    async def run(self) -> list[dict[str, Any]]:
        """累计预算在下一次请求前阻止继续；复盘错误由 owner 收口，不污染已交付主回复。"""
        for iteration in range(16):
            if self.cancelled() or self.consumed >= self.budget:
                break
            request = ModelRequest(model=self.model, system_message=self.snapshot.system,
                                   messages=self.messages, tools=self.snapshot.schemas,
                                   model_settings=self.snapshot.settings, tool_choice=self.snapshot.tool_choice,
                                   state={'messages': self.messages}, runtime=None)
            response = await self._invoke(request) if iteration == 0 else await self.compaction.awrap_model_call(request, self._invoke)
            if isinstance(response, ExtendedModelResponse):
                self.messages = response.command.update['messages'][1:]
                message = response.model_response.result[-1]
            else:
                self.messages.extend(response.result)
                message = response.result[-1]
            if not isinstance(message, AIMessage) or not message.tool_calls:
                break
            await self._dispatch(message)
        return self.actions
