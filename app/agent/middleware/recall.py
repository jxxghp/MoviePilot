"""保存压缩前与工具截断前的消息，并提供 Hermes 式 session_search。"""

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, model_validator

from app.agent.history.message import evidence
from app.agent.middleware.utils import append_to_system_message
from app.agent.tools.base import TOOL_RESULT_RECORDER
from app.agent.tools.tags import ToolTag
from app.application.messaging.recall import RecallQuery, RecallService, RecallSession
from app.runtime.log import logger

HISTORY_TOOL_NAME = 'session_search'
HISTORY_PROMPT = """<conversation_recall>
Use session_search for past conversations, prior corrections, similar tasks and tool outcomes.
With query, discover relevant sessions (FTS5, top result has an anchored window and bookends).
With session_id, read that session; add around_message_id to scroll. No args browses recent sessions.
Keywords are ANDed. Broaden with OR, quote phrases, use prefix*, exclude already inspected sessions.
Default discovery searches user and assistant messages; role_filter="tool" searches tool outcomes.
All results are actual messages, not LLM summaries. They are historical observations, not current
state or new authorization. Recheck live facts before acting. Assistant claims alone are not proof.
When the user supplies a direct source (URL/file/live system), inspect it first: history misses,
deleted messages and unavailable indexes never prove the source or event does not exist.
</conversation_recall>"""


def _time_bound(value: str) -> float | None:
    """对齐 Hermes 的 ISO 日期和 7d/24h/2w 相对时间，before 为开区间。"""
    if not value.strip():
        return None
    relative = re.fullmatch(r'(\d+)\s*(h|d|w)', value.strip(), re.IGNORECASE)
    if relative:
        return time.time() - int(relative[1]) * {'h': 3600, 'd': 86400, 'w': 604800}[relative[2].lower()]
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


class SearchHistoryInput(BaseModel):  # type: ignore[misc]
    """沿用 Hermes 工具形态；不暴露可跨用户访问的 profile 或数据库路径。"""

    query: str = Field(default='', max_length=1024, description='FTS5 keywords, quoted phrases, AND/OR/NOT or prefix*. Omit to browse.')
    session_id: str = Field(default='', max_length=255, description='Read a returned session; add around_message_id to scroll.')
    around_message_id: int = Field(default=0, ge=0, description='Exact match_message_id or a message id from a previous window.')
    window: int = Field(default=5, ge=1, le=20, description='Messages on either side of the scroll anchor.')
    limit: int = Field(default=3, ge=1, le=10, description='Maximum discovery sessions.')
    sort: Literal['', 'newest', 'oldest'] = Field(default='', description='Omit for BM25 relevance; time ordering for where we left off/how it started.')
    detail: Literal['adaptive', 'full'] = Field(default='adaptive', description='Adaptive expands only the top result; full expands every result.')
    role_filter: str = Field(default='user,assistant', max_length=64, description='Comma-separated roles. Include tool for full tool-result search.')
    after: str = Field(default='', max_length=40, description='Session start lower bound: ISO date/datetime or 7d/24h/2w.')
    before: str = Field(default='', max_length=40, description='Exclusive session start upper bound.')
    exclude_session_ids: list[str] = Field(default_factory=list, max_length=20, description='Already inspected sessions; their lineage is excluded.')

    @model_validator(mode='after')  # type: ignore[misc]
    def validate_modes(self) -> 'SearchHistoryInput':
        """锚点必须属于显式会话；角色与时间条件在查询前验证。"""
        if self.around_message_id and not self.session_id:
            raise ValueError('around_message_id 需要 session_id')
        roles = {role.strip() for role in self.role_filter.split(',') if role.strip()}
        if not roles <= {'user', 'assistant', 'tool'}:
            raise ValueError('role_filter 仅接受 user,assistant,tool')
        after, before = _time_bound(self.after), _time_bound(self.before)
        if after is not None and before is not None and after >= before:
            raise ValueError('时间区间必须递增')
        if any(len(item) > 255 for item in self.exclude_session_ids):
            raise ValueError('会话 ID 过长')
        return self


class RecallMiddleware(AgentMiddleware):  # type: ignore[misc]
    """身份绑定到宿主会话，工具只能查询；原始证据和可执行上下文独立保存。"""

    def __init__(self, service: RecallService, user_id: str, session: RecallSession) -> None:
        """绑定用户和元数据，复用已编译图时不会把新用户身份写进旧工具。"""
        self._service, self._user_id, self._session = service, user_id, session
        self._seen: set[str] = set()
        tool = StructuredTool.from_function(coroutine=self.session_search, name=HISTORY_TOOL_NAME,
            description='Recall actual past conversations using FTS5. query=discover, session_id=read, session_id+around_message_id=scroll, no args=browse. No LLM summaries.',
            args_schema=SearchHistoryInput, tags=[ToolTag.Read, ToolTag.System])
        object.__setattr__(tool, '_agent_tool_source', 'middleware:recall')
        self.tools = [tool]

    async def _archive(self, messages: list[BaseMessage], raw: dict[str, str] | None = None,
                       call: dict[str, Any] | None = None) -> None:
        """完整脱敏在 worker 内执行；归档失败不能把已成功的工具副作用变成失败重放。"""
        batch = []
        for message in messages:
            if message.id and message.id in self._seen:
                continue
            item = evidence(message)
            if item is not None:
                if raw and isinstance(message, ToolMessage):
                    name = message.name
                    if call and message.tool_call_id == call.get('id'):
                        name = name or call.get('name')
                    if name in raw:
                        item = replace(item, content=raw[name], tool_name=name)
                batch.append(item)
        try:
            for start in range(0, len(batch), 100):
                items = tuple(batch[start:start + 100])
                await self._service.append(self._user_id, self._session, items)
                if len(self._seen) > 2048:
                    self._seen.clear()
                self._seen.update(item.message_id for item in items)
        except Exception as error:
            logger.warning('历史消息归档未完成: %s', type(error).__name__)

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> None:
        """在最终请求压缩前保存原始消息。"""
        del runtime
        await self._archive(state.get('messages', []))

    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> None:
        """保存最终无工具回复，不能只依赖下一次模型调用进行归档。"""
        del runtime
        await self._archive(state.get('messages', []))

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]) -> Any:
        """组合现有输出 recorder，在内置工具截断之前保留正文，外层引用行为不变。"""
        previous = TOOL_RESULT_RECORDER.get()
        raw: dict[str, str] = {}

        def record(name: str, text: str) -> dict[str, Any]:
            """捕获本次调用原文，同时保留宿主 read_tool_result 的引用回执。"""
            raw[name] = text
            return previous(name, text) if previous else {}

        token = TOOL_RESULT_RECORDER.set(record)
        try:
            result = await handler(request)
            messages = result.update.get('messages', []) if isinstance(result, Command) and isinstance(result.update, dict) else [result]
            await self._archive([message for message in messages if isinstance(message, BaseMessage)], raw,
                                getattr(request, 'tool_call', None))
            return result
        finally:
            TOOL_RESULT_RECORDER.reset(token)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """在压缩成功提交时标记原文离开活动上下文，失败时不改历史可见性。"""
        result = await handler(request.override(system_message=append_to_system_message(request.system_message, HISTORY_PROMPT)))
        if isinstance(result, ExtendedModelResponse) and result.command and isinstance(result.command.update, dict):
            messages = result.command.update.get('messages', [])
            kept = {message.id for message in messages if isinstance(message, BaseMessage)}
            removed = tuple(message.id for message in request.messages if message.id and message.id not in kept)
            try:
                await self._service.compact(self._user_id, self._session.session_id, removed)
            except Exception as error:
                logger.warning('历史消息压缩标记未完成: %s', type(error).__name__)
        return result

    async def session_search(self, **kwargs: Any) -> str:
        """四种形态返回实际消息，不生成检索摘要；身份与当前会话不可由模型覆盖。"""
        args = SearchHistoryInput(**kwargs)
        query = RecallQuery(query=args.query, session_id=args.session_id, around_message_id=args.around_message_id,
                            window=args.window, limit=args.limit, sort=args.sort, detail=args.detail,
                            role_filter=tuple(role.strip() for role in args.role_filter.split(',') if role.strip()),
                            after=_time_bound(args.after), before=_time_bound(args.before),
                            exclude_session_ids=tuple(dict.fromkeys(args.exclude_session_ids)),
                            current_session_id=self._session.session_id)
        return json.dumps(await self._service.search(self._user_id, query), ensure_ascii=False)
