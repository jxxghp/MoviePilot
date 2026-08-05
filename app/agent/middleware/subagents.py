"""MoviePilot 子代理中间件适配。"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal, Optional

from langchain.agents import create_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agent.llm import LLMHelper
from app.agent.middleware.utils import append_to_system_message
from app.agent.runtime import SubAgentDefinition, agent_runtime_manager
from app.agent.tools.tags import ToolTag
from app.log import logger


SUBAGENT_TASK_TOOL_NAME = "task"
SUBAGENT_CONTROL_TOOL_NAME = "subagent_task"
SUBAGENT_STREAM_MARKER_KEY = "ls_agent_type"
SUBAGENT_STREAM_MARKER_VALUE = "subagent"
SUBAGENT_DEFAULT_WAIT_TIMEOUT_MS = 60000
SUBAGENT_MAX_WAIT_TIMEOUT_MS = 300000
SUBAGENT_MAX_ACTIVE_TASKS = 8
SUBAGENT_MAX_CONCURRENT_TASKS = 4
SUBAGENT_RESULT_MAX_CHARS = 12000
SUBAGENT_DESCRIPTION_MAX_CHARS = 500
SUBAGENT_PIPELINE_CONTEXT_MAX_CHARS = 12000

SUBAGENT_PARENT_PROMPT = """<subagents>
You may use subagent tools to delegate independent research, retrieval,
diagnosis, or planning work to built-in subagents.

Delegation modes:
- Use `task` for one blocking subtask when you need the result immediately.
- Use `subagent_task` for two or more independent subtasks. Start them first
  with `action=start` and a `tasks` array, then use `action=status`,
  `action=wait`, or `action=cancel` with the returned task IDs.
- Use `subagent_task` with `action=run` when you want to launch a bounded
  batch and wait for the batch in one tool call.
- Use `subagent_task` with `action=pipeline` when later subtasks must use
  previous subagent results. Pipeline steps run sequentially, and each step's
  result is passed as private context to the next step.

Rules:
- Delegate when a task benefits from focused investigation, such as media identity checks, site/resource search, subscription analysis, download/transfer diagnosis, MoviePilot code/config exploration, or read-only system inspection.
- Subagent output is private context for your decision-making. Do not expose a subagent's process or final report verbatim to the user.
- Subagents must not send messages to the user, ask for interaction, or reveal their internal tool activity.
- Give the user only your synthesized final answer and the minimum necessary next step.
- If a task requires configuration changes, deletion, adding downloads, adding subscriptions, or any high-impact action, the main agent must handle it directly under the confirmation policy.
</subagents>"""

SUBAGENT_TASK_DESCRIPTION = (
    "Delegate an isolated MoviePilot investigation or planning task to a built-in "
    "subagent. The subagent result is private context for the main agent and must "
    "not be forwarded verbatim to the user."
)

SUBAGENT_CONTROL_DESCRIPTION = (
    "Start and manage multiple MoviePilot subagent tasks asynchronously. "
    "Use action=start with tasks=[{description, subagent_type}] to launch a batch "
    "and get task IDs immediately. Use action=status to inspect tasks, action=wait "
    "to wait for all or any task result, action=cancel to stop running tasks, and "
    "action=run to launch a bounded batch and wait in one call. Use action=pipeline "
    "to run tasks sequentially while passing each result as private context to the "
    "next task."
)

SUBAGENT_BASE_PROMPT = """You are a silent subagent working for the MoviePilot main agent.

Requirements:
- Handle only the delegated subtask from the main agent. Do not converse with the user.
- Do not send messages, request user interaction, or output progress updates.
- Use tool results only for analysis, and return the final result only to the main agent.
- Unless the task explicitly requires it and your tool set permits it, limit yourself to read-only inspection and diagnosis.
- If user confirmation or a high-impact change is needed, explain why the main agent must confirm it instead of executing it yourself.
- Return a concise structured Chinese result with key evidence, judgment, and recommended next step.
"""


@dataclass(frozen=True)
class _SubAgentProfile:
    """子代理运行时定义。"""

    name: str
    description: str
    prompt: str
    include_tags: frozenset[str]
    exclude_tags: frozenset[str]


class _TaskToolInput(BaseModel):
    """子代理任务工具输入。"""

    description: str = Field(..., description="Complete task description for the subagent")
    subagent_type: str = Field(
        default="general-purpose",
        description="Subagent type to invoke, such as general-purpose or media-researcher",
    )


class _SubAgentTaskSpec(BaseModel):
    """异步子代理任务定义。"""

    description: str = Field(..., description="Complete task description for the subagent")
    subagent_type: str = Field(
        default="general-purpose",
        description="Subagent type to invoke, such as general-purpose or media-researcher",
    )


class _SubAgentControlInput(BaseModel):
    """异步子代理管控工具输入。"""

    action: Literal["start", "status", "wait", "cancel", "run", "pipeline"] = Field(
        default="start",
        description="Task action: start, status, wait, cancel, run, or pipeline.",
    )
    description: Optional[str] = Field(
        default=None,
        description="Single task description for action=start or action=run.",
    )
    subagent_type: Optional[str] = Field(
        default="general-purpose",
        description="Single task subagent type for action=start or action=run.",
    )
    tasks: Optional[list[_SubAgentTaskSpec]] = Field(
        default=None,
        description="Batch task specs for action=start or action=run.",
    )
    task_ids: Optional[list[str]] = Field(
        default=None,
        description="Task IDs returned by action=start. Empty means all known tasks.",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Single task ID for status, wait, or cancel.",
    )
    wait_mode: Literal["all", "any"] = Field(
        default="all",
        description="For action=wait or action=run: wait for all selected tasks or any one task.",
    )
    timeout_ms: Optional[int] = Field(
        default=SUBAGENT_DEFAULT_WAIT_TIMEOUT_MS,
        description=(
            "Maximum wait time in milliseconds for action=wait, action=run, "
            "or each action=pipeline step."
        ),
    )


@dataclass
class _SubAgentRuntimeTask:
    """运行中的异步子代理任务记录。"""

    task_id: str
    description: str
    subagent_type: str
    task: asyncio.Task
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


def is_subagent_stream_metadata(metadata: Any) -> bool:
    """判断流式 token 元数据是否来自子代理。"""
    if not isinstance(metadata, dict):
        return False

    if metadata.get(SUBAGENT_STREAM_MARKER_KEY) == SUBAGENT_STREAM_MARKER_VALUE:
        return True

    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, dict) and nested_metadata.get(
        SUBAGENT_STREAM_MARKER_KEY
    ) == SUBAGENT_STREAM_MARKER_VALUE:
        return True

    configurable = metadata.get("configurable")
    if isinstance(configurable, dict) and configurable.get(
        SUBAGENT_STREAM_MARKER_KEY
    ) == SUBAGENT_STREAM_MARKER_VALUE:
        return True

    return bool(
        metadata.get("lc_agent_name")
        in builtin_subagent_names(agent_runtime_manager.current_signature())
    )


def builtin_subagent_names(
    runtime_signature: Optional[tuple[tuple[str, int, int], ...]] = None,
) -> frozenset[str]:
    """返回内置子代理名称集合。"""
    runtime_signature = runtime_signature or agent_runtime_manager.current_signature()
    return _cached_builtin_subagent_names(runtime_signature)


@lru_cache(maxsize=8)
def _cached_builtin_subagent_names(
    runtime_signature: tuple[tuple[str, int, int], ...],
) -> frozenset[str]:
    """按运行时签名缓存内置子代理名称集合。"""
    return frozenset(
        profile.name
        for profile in _builtin_subagent_profiles(runtime_signature)
    )


def _builtin_subagent_profiles(
    runtime_signature: Optional[tuple[tuple[str, int, int], ...]] = None,
) -> tuple[_SubAgentProfile, ...]:
    """从运行时配置目录加载 MoviePilot 子代理定义。"""
    runtime_signature = runtime_signature or agent_runtime_manager.current_signature()
    return _cached_builtin_subagent_profiles(runtime_signature)


@lru_cache(maxsize=8)
def _cached_builtin_subagent_profiles(
    runtime_signature: tuple[tuple[str, int, int], ...],
) -> tuple[_SubAgentProfile, ...]:
    """按运行时签名缓存 MoviePilot 子代理定义。"""
    definitions = agent_runtime_manager.list_subagents()
    profiles = tuple(
        _profile_from_runtime_definition(definition)
        for definition in definitions
    )
    if profiles:
        return profiles
    logger.warning("未加载到任何子代理定义，使用通用兜底子代理。")
    return (
        _SubAgentProfile(
            name="general-purpose",
            description="General read-only investigation subagent for cross-domain MoviePilot analysis and execution recommendations.",
            prompt=(
                f"{SUBAGENT_BASE_PROMPT}\n"
                "You specialize in synthesizing media, site, subscription, download, and system status signals."
            ),
            include_tags=frozenset(tag.value for tag in ToolTag),
            exclude_tags=frozenset(
                {
                    ToolTag.Write.value,
                    ToolTag.Message.value,
                    ToolTag.UserInteraction.value,
                }
            ),
        ),
    )


builtin_subagent_names.cache_clear = _cached_builtin_subagent_names.cache_clear
_builtin_subagent_profiles.cache_clear = _cached_builtin_subagent_profiles.cache_clear


def _profile_from_runtime_definition(
    definition: SubAgentDefinition,
) -> _SubAgentProfile:
    """把运行时子代理定义转换为中间件可用的 profile。"""
    prompt_parts = [SUBAGENT_BASE_PROMPT]
    if definition.text.strip():
        prompt_parts.append(definition.text.strip())
    return _SubAgentProfile(
        name=definition.subagent_id,
        description=definition.description,
        prompt="\n".join(prompt_parts),
        include_tags=frozenset(definition.include_tags),
        exclude_tags=frozenset(definition.exclude_tags),
    )


def _tool_tag_values(tool: BaseTool) -> set[str]:
    """读取工具实例上的标签集合。"""
    tags = getattr(tool, "tags", None) or []
    if isinstance(tags, str):
        return {tags}
    return {str(tag) for tag in tags if tag}


def _select_tools(tools: list[BaseTool], profile: _SubAgentProfile) -> list[BaseTool]:
    """根据工具标签筛选子代理可用工具。"""
    selected_tools = []
    for tool in tools:
        tags = _tool_tag_values(tool)
        if ToolTag.Read.value not in tags:
            continue
        if profile.exclude_tags & tags:
            continue
        if profile.include_tags & tags:
            selected_tools.append(tool)
    return selected_tools


def _format_subagent_catalog(profiles: tuple[_SubAgentProfile, ...]) -> str:
    """渲染子代理目录供任务工具描述使用。"""
    return "\n".join(
        f"- {profile.name}: {profile.description}" for profile in profiles
    )


def _extract_final_text(result: Any) -> str:
    """从子代理执行结果中提取最后一条 AI 文本。"""
    if isinstance(result, dict):
        messages = result.get("messages") or []
    else:
        messages = getattr(result, "messages", []) or []

    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            text = LLMHelper.extract_text_content(message.content).strip()
            if text:
                return text

    return LLMHelper.extract_text_content(result, fallback_to_string=True).strip()


def _clip_text(text: Any, max_chars: int) -> tuple[str, bool]:
    """裁剪过长文本，返回文本和是否被裁剪。"""
    normalized = "" if text is None else str(text)
    if len(normalized) <= max_chars:
        return normalized, False
    return normalized[:max_chars], True


def _format_datetime(value: Optional[datetime]) -> Optional[str]:
    """格式化任务时间。"""
    if not value:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _extract_tool_call_args(request: ToolCallRequest) -> dict[str, Any]:
    """提取工具调用参数，并规整为字典。"""
    tool_call = request.tool_call or {}
    tool_args = tool_call.get("args") or {}
    if not isinstance(tool_args, dict):
        return {}
    return tool_args


def _record_subagent_tool_call(
    *,
    stream_handler: Any,
    tool_name: str,
    tool_args: dict[str, Any],
) -> None:
    """在流式处理器中记录子代理工具调用摘要。"""
    if not stream_handler or not getattr(stream_handler, "is_streaming", False):
        return
    stream_handler.record_tool_call(
        tool_name=tool_name,
        tool_message="Subagent invoked",
        tool_kwargs=tool_args,
    )


class _SubAgentAgentProvider:
    """子代理图懒加载与执行器。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        profiles: tuple[_SubAgentProfile, ...],
        tools: list[BaseTool],
        server_tools: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """初始化子代理执行器。"""
        self._model = model
        self._profiles = {profile.name: profile for profile in profiles}
        self._tools = tools
        self._server_tools = server_tools or []
        self._agents = {}
        self._default_agent_name = "general-purpose"

    def _resolve_profile(self, agent_name: Optional[str]) -> _SubAgentProfile:
        """解析子代理类型，未知类型回退到默认子代理。"""
        return self._profiles.get(agent_name or "") or self._profiles[
            self._default_agent_name
        ]

    def get_agent(self, agent_name: Optional[str]) -> tuple[str, Any]:
        """懒加载指定名称的子代理图。"""
        profile = self._resolve_profile(agent_name)
        cached_agent = self._agents.get(profile.name)
        if cached_agent:
            return profile.name, cached_agent

        subagent_tools = _select_tools(self._tools, profile)
        logger.info(
            f"创建子代理图: subagent_type={profile.name}, tools={len(subagent_tools)}"
        )
        agent = create_agent(
            model=self._model,
            tools=[*subagent_tools, *self._server_tools],
            system_prompt=profile.prompt,
            name=profile.name,
        )
        self._agents[profile.name] = agent
        return profile.name, agent

    async def run_task(
        self,
        *,
        description: str,
        subagent_type: Optional[str],
        task_id: Optional[str] = None,
    ) -> str:
        """调用指定子代理并只返回供主代理读取的结果。"""
        agent_name, agent = self.get_agent(subagent_type)
        thread_suffix = task_id or uuid.uuid4().hex
        log_task_id = task_id or "-"
        logger.info(
            f"开始调用子代理: subagent_type={agent_name}, task_id={log_task_id}"
        )
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=description)]},
                config={
                    "configurable": {
                        "thread_id": f"subagent-{agent_name}-{thread_suffix}",
                        SUBAGENT_STREAM_MARKER_KEY: SUBAGENT_STREAM_MARKER_VALUE,
                    },
                    "metadata": {
                        "lc_agent_name": agent_name,
                        SUBAGENT_STREAM_MARKER_KEY: SUBAGENT_STREAM_MARKER_VALUE,
                    },
                },
            )
        except Exception as err:
            logger.error(
                f"子代理调用失败: subagent_type={agent_name}, "
                f"task_id={log_task_id}, error={err}"
            )
            raise
        final_text = _extract_final_text(result)
        logger.info(
            f"子代理调用完成: subagent_type={agent_name}, "
            f"task_id={log_task_id}, result_chars={len(final_text)}"
        )
        return final_text or "The subagent did not return a usable result."


class MoviePilotSubAgentMiddleware(AgentMiddleware):
    """MoviePilot 本地子代理中间件兜底实现。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        profiles: tuple[_SubAgentProfile, ...],
        tools: list[BaseTool],
        server_tools: Optional[list[dict[str, Any]]] = None,
        system_prompt: str = SUBAGENT_PARENT_PROMPT,
        task_description: str = SUBAGENT_TASK_DESCRIPTION,
        stream_handler: Any = None,
    ) -> None:
        """初始化同步子代理中间件。"""
        self.system_prompt = system_prompt
        self.stream_handler = stream_handler
        self._provider = _SubAgentAgentProvider(
            model=model,
            profiles=profiles,
            tools=tools,
            server_tools=server_tools,
        )
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._run_task,
                name=SUBAGENT_TASK_TOOL_NAME,
                description=(
                    f"{task_description}\n\nAvailable subagents:\n"
                    f"{_format_subagent_catalog(profiles)}"
                ),
                args_schema=_TaskToolInput,
            )
        ]

    def _get_agent(self, agent_name: str) -> Any:
        """懒加载指定名称的子代理图。"""
        return self._provider.get_agent(agent_name)[1]

    async def _run_task(self, description: str, subagent_type: str) -> str:
        """调用指定子代理并只返回供主代理读取的结果。"""
        return await self._provider.run_task(
            description=description,
            subagent_type=subagent_type,
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """在主代理模型调用前注入子代理使用说明。"""
        new_system_message = append_to_system_message(
            request.system_message,
            self.system_prompt,
        )
        return await handler(request.override(system_message=new_system_message))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在 task 子代理工具执行时记录聚合摘要。"""
        tool = request.tool
        tool_name = getattr(tool, "name", None)
        if tool_name != SUBAGENT_TASK_TOOL_NAME:
            return await handler(request)

        tool_args = _extract_tool_call_args(request)
        logger.info(
            f"开始执行子代理工具: tool_name={tool_name}, "
            f"subagent_type={tool_args.get('subagent_type') or '-'}"
        )
        _record_subagent_tool_call(
            stream_handler=self.stream_handler,
            tool_name=SUBAGENT_TASK_TOOL_NAME,
            tool_args=tool_args,
        )
        try:
            result = await handler(request)
        except Exception as err:
            logger.error(f"子代理工具执行失败: tool_name={tool_name}, error={err}")
            raise
        logger.info(f"子代理工具执行完成: tool_name={tool_name}")
        return result


class SubAgentTaskControlMiddleware(AgentMiddleware):
    """提供异步子代理任务调度工具的中间件。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        profiles: tuple[_SubAgentProfile, ...],
        tools: list[BaseTool],
        server_tools: Optional[list[dict[str, Any]]] = None,
        task_description: str = SUBAGENT_CONTROL_DESCRIPTION,
        stream_handler: Any = None,
    ) -> None:
        """初始化异步子代理调度中间件。"""
        self.stream_handler = stream_handler
        self._provider = _SubAgentAgentProvider(
            model=model,
            profiles=profiles,
            tools=tools,
            server_tools=server_tools,
        )
        self._semaphore = asyncio.Semaphore(SUBAGENT_MAX_CONCURRENT_TASKS)
        self._tasks: dict[str, _SubAgentRuntimeTask] = {}
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._control_task,
                name=SUBAGENT_CONTROL_TOOL_NAME,
                description=(
                    f"{task_description}\n\nAvailable subagents:\n"
                    f"{_format_subagent_catalog(profiles)}"
                ),
                args_schema=_SubAgentControlInput,
            )
        ]

    @staticmethod
    def _json_response(payload: dict[str, Any]) -> str:
        """将工具响应序列化为稳定 JSON。"""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _normalize_timeout_ms(timeout_ms: Optional[int]) -> int:
        """规范化等待超时时间。"""
        if timeout_ms is None:
            return SUBAGENT_DEFAULT_WAIT_TIMEOUT_MS
        return max(0, min(int(timeout_ms), SUBAGENT_MAX_WAIT_TIMEOUT_MS))

    @staticmethod
    def _task_status(record: _SubAgentRuntimeTask) -> str:
        """读取任务当前状态。"""
        task = record.task
        if task.cancelled():
            return "cancelled"
        if not task.done():
            return "running" if record.started_at else "pending"
        if task.exception():
            return "failed"
        return "completed"

    @staticmethod
    def _task_output(record: _SubAgentRuntimeTask) -> dict[str, Any]:
        """格式化单个任务状态和结果。"""
        description, description_truncated = _clip_text(
            record.description,
            SUBAGENT_DESCRIPTION_MAX_CHARS,
        )
        payload: dict[str, Any] = {
            "task_id": record.task_id,
            "subagent_type": record.subagent_type,
            "status": SubAgentTaskControlMiddleware._task_status(record),
            "description": description,
            "description_truncated": description_truncated,
            "created_at": _format_datetime(record.created_at),
            "started_at": _format_datetime(record.started_at),
            "finished_at": _format_datetime(record.finished_at),
        }
        if not record.task.done():
            return payload
        if record.task.cancelled():
            return payload

        error = record.task.exception()
        if error:
            payload["error"] = str(error)
            return payload

        result, result_truncated = _clip_text(
            record.task.result(),
            SUBAGENT_RESULT_MAX_CHARS,
        )
        payload["result"] = result
        payload["result_truncated"] = result_truncated
        return payload

    def _selected_records(
        self,
        *,
        task_ids: Optional[list[str]] = None,
        task_id: Optional[str] = None,
        active_only: bool = False,
    ) -> tuple[list[_SubAgentRuntimeTask], list[str]]:
        """根据任务 ID 选择记录。"""
        selected_ids = []
        if task_id:
            selected_ids.append(task_id)
        selected_ids.extend(task_ids or [])
        if not selected_ids:
            records = list(self._tasks.values())
            if active_only:
                records = [record for record in records if not record.task.done()]
            return records, []

        records = []
        missing_ids = []
        seen_ids = set()
        for selected_id in selected_ids:
            if selected_id in seen_ids:
                continue
            seen_ids.add(selected_id)
            record = self._tasks.get(selected_id)
            if record:
                records.append(record)
            else:
                missing_ids.append(selected_id)
        return records, missing_ids

    def _normalize_specs(
        self,
        *,
        description: Optional[str],
        subagent_type: Optional[str],
        tasks: Optional[list[_SubAgentTaskSpec]],
    ) -> tuple[list[_SubAgentTaskSpec], Optional[str]]:
        """规范化单任务和批量任务输入。"""
        specs = []
        for task in tasks or []:
            if isinstance(task, dict):
                task = _SubAgentTaskSpec(**task)
            if task.description.strip():
                specs.append(task)
        if not specs and description and description.strip():
            specs.append(
                _SubAgentTaskSpec(
                    description=description,
                    subagent_type=subagent_type or "general-purpose",
                )
            )
        if not specs:
            return [], "缺少可执行的子代理任务描述。"
        if len(specs) > SUBAGENT_MAX_ACTIVE_TASKS:
            return [], f"单次最多可提交 {SUBAGENT_MAX_ACTIVE_TASKS} 个子代理任务。"

        active_count = sum(
            1 for record in self._tasks.values() if not record.task.done()
        )
        if active_count + len(specs) > SUBAGENT_MAX_ACTIVE_TASKS:
            return [], (
                f"当前仍有 {active_count} 个子代理任务未完成，"
                f"总并发上限为 {SUBAGENT_MAX_ACTIVE_TASKS}。"
            )
        return specs, None

    async def _execute_managed_task(self, record: _SubAgentRuntimeTask) -> str:
        """执行受调度器管理的子代理任务。"""
        async with self._semaphore:
            record.started_at = datetime.now()
            logger.info(
                f"异步子代理任务开始执行: task_id={record.task_id}, "
                f"subagent_type={record.subagent_type}"
            )
            try:
                result = await self._provider.run_task(
                    description=record.description,
                    subagent_type=record.subagent_type,
                    task_id=record.task_id,
                )
                logger.info(
                    f"异步子代理任务执行完成: task_id={record.task_id}, "
                    f"subagent_type={record.subagent_type}, result_chars={len(result)}"
                )
                return result
            except asyncio.CancelledError:
                logger.info(
                    f"异步子代理任务已取消: task_id={record.task_id}, "
                    f"subagent_type={record.subagent_type}"
                )
                raise
            except Exception as err:
                logger.error(f"子代理任务执行失败: task_id={record.task_id}, error={err}")
                raise

    def _mark_task_finished(self, task_id: str, task: asyncio.Task) -> None:
        """记录任务完成时间并取出异常避免未读取告警。"""
        record = self._tasks.get(task_id)
        if record:
            record.finished_at = datetime.now()
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            return

    def _start_tasks(self, specs: list[_SubAgentTaskSpec]) -> list[_SubAgentRuntimeTask]:
        """启动一批异步子代理任务。"""
        records = []
        for spec in specs:
            task_id = f"subagent-{uuid.uuid4().hex[:12]}"
            record = _SubAgentRuntimeTask(
                task_id=task_id,
                description=spec.description.strip(),
                subagent_type=spec.subagent_type or "general-purpose",
                task=None,
                created_at=datetime.now(),
            )
            task = asyncio.create_task(
                self._execute_managed_task(record),
                name=task_id,
            )
            record.task = task
            task.add_done_callback(
                lambda finished_task, finished_task_id=task_id: self._mark_task_finished(
                    finished_task_id,
                    finished_task,
                )
            )
            self._tasks[task_id] = record
            records.append(record)
            logger.info(
                f"已启动子代理任务: task_id={task_id}, "
                f"subagent_type={record.subagent_type}"
            )
        return records

    async def _wait_records(
        self,
        *,
        records: list[_SubAgentRuntimeTask],
        wait_mode: str,
        timeout_ms: Optional[int],
    ) -> None:
        """按等待模式等待一组任务完成。"""
        pending_tasks = [record.task for record in records if not record.task.done()]
        if not pending_tasks:
            if records:
                logger.info(f"子代理任务无需等待: tasks={len(records)}")
            return

        normalized_timeout_ms = self._normalize_timeout_ms(timeout_ms)
        timeout = normalized_timeout_ms / 1000
        if timeout <= 0:
            logger.info(
                f"子代理任务等待超时时间为 0，跳过等待: tasks={len(pending_tasks)}"
            )
            return

        return_when = (
            asyncio.FIRST_COMPLETED if wait_mode == "any" else asyncio.ALL_COMPLETED
        )
        logger.info(
            f"开始等待子代理任务: tasks={len(pending_tasks)}, "
            f"wait_mode={wait_mode}, timeout_ms={normalized_timeout_ms}"
        )
        await asyncio.wait(
            pending_tasks,
            timeout=timeout,
            return_when=return_when,
        )
        finished_count = sum(1 for task in pending_tasks if task.done())
        logger.info(
            f"子代理任务等待结束: finished={finished_count}, "
            f"pending={len(pending_tasks) - finished_count}"
        )

    @staticmethod
    async def _cancel_records(records: list[_SubAgentRuntimeTask]) -> None:
        """取消一组尚未完成的任务。"""
        cancellable_tasks = [
            record.task for record in records if not record.task.done()
        ]
        if cancellable_tasks:
            logger.info(f"开始取消子代理任务: tasks={len(cancellable_tasks)}")
        for task in cancellable_tasks:
            task.cancel()
        if cancellable_tasks:
            await asyncio.gather(*cancellable_tasks, return_exceptions=True)
            logger.info(f"子代理任务取消完成: tasks={len(cancellable_tasks)}")

    @staticmethod
    def _pipeline_description(
        *,
        description: str,
        previous_results: list[tuple[_SubAgentRuntimeTask, str]],
    ) -> str:
        """追加上游子代理结果，生成当前管道步骤的任务描述。"""
        normalized_description = description.strip()
        if not previous_results:
            return normalized_description

        context_parts = []
        for step_index, (record, result) in enumerate(previous_results, start=1):
            clipped_result, result_truncated = _clip_text(
                result,
                SUBAGENT_RESULT_MAX_CHARS,
            )
            truncated_note = "\n[Result truncated]" if result_truncated else ""
            context_parts.append(
                f"Step {step_index} ({record.subagent_type}) result:\n"
                f"{clipped_result}{truncated_note}"
            )
        context_text, context_truncated = _clip_text(
            "\n\n".join(context_parts),
            SUBAGENT_PIPELINE_CONTEXT_MAX_CHARS,
        )
        truncated_note = "\n[Pipeline context truncated]" if context_truncated else ""
        return (
            f"{normalized_description}\n\n"
            "<pipeline_context>\n"
            "Previous subagent results are private context for this delegated "
            "subtask. Use them to complete the current task, but do not expose "
            "the prior reports verbatim.\n\n"
            f"{context_text}{truncated_note}\n"
            "</pipeline_context>"
        )

    async def _execute_pipeline_task(
        self,
        *,
        record: _SubAgentRuntimeTask,
        description: str,
    ) -> str:
        """执行单个管道步骤，保留原始步骤描述用于状态展示。"""
        async with self._semaphore:
            record.started_at = datetime.now()
            logger.info(
                f"管道子代理任务开始执行: task_id={record.task_id}, "
                f"subagent_type={record.subagent_type}"
            )
            try:
                result = await self._provider.run_task(
                    description=description,
                    subagent_type=record.subagent_type,
                    task_id=record.task_id,
                )
                logger.info(
                    f"管道子代理任务执行完成: task_id={record.task_id}, "
                    f"subagent_type={record.subagent_type}, result_chars={len(result)}"
                )
                return result
            except asyncio.CancelledError:
                logger.info(
                    f"管道子代理任务已取消: task_id={record.task_id}, "
                    f"subagent_type={record.subagent_type}"
                )
                raise
            except Exception as err:
                logger.error(f"管道子代理任务执行失败: task_id={record.task_id}, error={err}")
                raise

    @staticmethod
    def _create_pipeline_record(
            spec: _SubAgentTaskSpec,
    ) -> _SubAgentRuntimeTask:
        """创建一个管道步骤记录。"""
        task_id = f"subagent-{uuid.uuid4().hex[:12]}"
        return _SubAgentRuntimeTask(
            task_id=task_id,
            description=spec.description.strip(),
            subagent_type=spec.subagent_type or "general-purpose",
            task=None,
            created_at=datetime.now(),
        )

    def _track_pipeline_task(
        self,
        record: _SubAgentRuntimeTask,
        task: asyncio.Task,
    ) -> None:
        """登记管道步骤任务，复用统一的状态和异常收口逻辑。"""
        record.task = task
        task.add_done_callback(
            lambda finished_task, finished_task_id=record.task_id: self._mark_task_finished(
                finished_task_id,
                finished_task,
            )
        )
        self._tasks[record.task_id] = record

    async def _run_pipeline(
        self,
        specs: list[_SubAgentTaskSpec],
        timeout_ms: Optional[int],
    ) -> tuple[list[_SubAgentRuntimeTask], Optional[str]]:
        """按顺序执行管道任务，并把每一步结果传给下一步。"""
        normalized_timeout_ms = self._normalize_timeout_ms(timeout_ms)
        if normalized_timeout_ms <= 0:
            return [], "管道任务需要大于 0 的等待时间。"

        records: list[_SubAgentRuntimeTask] = []
        previous_results: list[tuple[_SubAgentRuntimeTask, str]] = []
        timeout = normalized_timeout_ms / 1000
        for step_index, spec in enumerate(specs, start=1):
            record = self._create_pipeline_record(spec)
            records.append(record)
            pipeline_description = self._pipeline_description(
                description=record.description,
                previous_results=previous_results,
            )
            task = asyncio.create_task(
                self._execute_pipeline_task(
                    record=record,
                    description=pipeline_description,
                ),
                name=record.task_id,
            )
            self._track_pipeline_task(record, task)
            logger.info(
                f"已启动管道子代理任务: step={step_index}, task_id={record.task_id}, "
                f"subagent_type={record.subagent_type}"
            )

            try:
                result = await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                error = f"第 {step_index} 个管道子代理任务等待超时。"
                logger.info(
                    f"{error} task_id={record.task_id}, timeout_ms={normalized_timeout_ms}"
                )
                return records, error
            except Exception as err:
                error = f"第 {step_index} 个管道子代理任务执行失败: {err}"
                logger.info(f"{error} task_id={record.task_id}")
                return records, error

            previous_results.append((record, result))

        return records, None

    async def _control_task(
        self,
        action: str = "start",
        description: Optional[str] = None,
        subagent_type: Optional[str] = "general-purpose",
        tasks: Optional[list[_SubAgentTaskSpec]] = None,
        task_ids: Optional[list[str]] = None,
        task_id: Optional[str] = None,
        wait_mode: str = "all",
        timeout_ms: Optional[int] = SUBAGENT_DEFAULT_WAIT_TIMEOUT_MS,
    ) -> str:
        """管理异步子代理任务。"""
        logger.info(f"收到子代理管控操作: action={action}")
        if action in {"start", "run", "pipeline"}:
            specs, error = self._normalize_specs(
                description=description,
                subagent_type=subagent_type,
                tasks=tasks,
            )
            if error:
                logger.info(f"子代理管控操作未启动任务: action={action}, error={error}")
                return self._json_response({"success": False, "error": error})

            logger.info(f"准备启动子代理任务: action={action}, tasks={len(specs)}")
            if action == "pipeline":
                records, pipeline_error = await self._run_pipeline(
                    specs=specs,
                    timeout_ms=timeout_ms,
                )
                return self._json_response(
                    {
                        "success": pipeline_error is None,
                        "action": action,
                        "error": pipeline_error,
                        "tasks": [self._task_output(record) for record in records],
                    }
                )

            records = self._start_tasks(specs)
            if action == "run":
                await self._wait_records(
                    records=records,
                    wait_mode=wait_mode,
                    timeout_ms=timeout_ms,
                )

            return self._json_response(
                {
                    "success": True,
                    "action": action,
                    "wait_mode": wait_mode if action == "run" else None,
                    "tasks": [self._task_output(record) for record in records],
                }
            )

        records, missing_ids = self._selected_records(
            task_ids=task_ids,
            task_id=task_id,
            active_only=action in {"wait", "cancel"} and not task_ids and not task_id,
        )

        if action == "wait":
            logger.info(
                f"准备等待子代理任务: selected={len(records)}, missing={len(missing_ids)}"
            )
            await self._wait_records(
                records=records,
                wait_mode=wait_mode,
                timeout_ms=timeout_ms,
            )
        elif action == "cancel":
            logger.info(
                f"准备取消子代理任务: selected={len(records)}, missing={len(missing_ids)}"
            )
            await self._cancel_records(records)
        elif action == "status":
            logger.info(
                f"查询子代理任务状态: selected={len(records)}, missing={len(missing_ids)}"
            )

        return self._json_response(
            {
                "success": True,
                "action": action,
                "wait_mode": wait_mode if action == "wait" else None,
                "missing_task_ids": missing_ids,
                "tasks": [self._task_output(record) for record in records],
            }
        )

    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        """Agent 结束时取消未完成的子代理任务，避免后台泄漏。"""
        unfinished_records = [
            record for record in self._tasks.values() if not record.task.done()
        ]
        if unfinished_records:
            logger.info(f"Agent 结束，取消未完成子代理任务: tasks={len(unfinished_records)}")
        await self._cancel_records(unfinished_records)
        self._tasks.clear()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在 subagent_task 子代理工具执行时记录聚合摘要。"""
        tool = request.tool
        tool_name = getattr(tool, "name", None)
        if tool_name != SUBAGENT_CONTROL_TOOL_NAME:
            return await handler(request)

        tool_args = _extract_tool_call_args(request)
        logger.info(
            f"开始执行子代理工具: tool_name={tool_name}, "
            f"action={tool_args.get('action') or '-'}, "
            f"subagent_type={tool_args.get('subagent_type') or '-'}"
        )
        _record_subagent_tool_call(
            stream_handler=self.stream_handler,
            tool_name=SUBAGENT_CONTROL_TOOL_NAME,
            tool_args=tool_args,
        )
        try:
            result = await handler(request)
        except Exception as err:
            logger.error(f"子代理工具执行失败: tool_name={tool_name}, error={err}")
            raise
        logger.info(f"子代理工具执行完成: tool_name={tool_name}")
        return result


def create_subagent_middlewares(
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    server_tools: Optional[list[dict[str, Any]]] = None,
    stream_handler: Any = None,
) -> tuple[list[AgentMiddleware], list[BaseTool]]:
    """创建子代理中间件列表和任务工具列表。"""
    runtime_signature = agent_runtime_manager.current_signature()
    profiles = _builtin_subagent_profiles(runtime_signature)
    subagent_middleware = MoviePilotSubAgentMiddleware(
        model=model,
        profiles=profiles,
        tools=tools,
        server_tools=server_tools or [],
        stream_handler=stream_handler,
    )
    control_middleware = SubAgentTaskControlMiddleware(
        model=model,
        profiles=profiles,
        tools=tools,
        server_tools=server_tools or [],
        stream_handler=stream_handler,
    )

    task_tools = [
        *list(getattr(subagent_middleware, "tools", []) or []),
        *list(getattr(control_middleware, "tools", []) or []),
    ]
    return [
        subagent_middleware,
        control_middleware,
    ], task_tools


__all__ = [
    "SUBAGENT_CONTROL_TOOL_NAME",
    "SUBAGENT_TASK_TOOL_NAME",
    "SubAgentTaskControlMiddleware",
    "create_subagent_middlewares",
    "is_subagent_stream_metadata",
]
