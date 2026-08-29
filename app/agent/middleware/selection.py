"""MoviePilot 自定义工具筛选中间件。"""

from dataclasses import dataclass, replace
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.agents.middleware.types import (
    PrivateStateAttr,  # noqa
)
from langchain.agents.middleware.tool_selection import (
    DEFAULT_SYSTEM_PROMPT,
    LLMToolSelectorMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from typing_extensions import TypedDict  # noqa

from app.agent.llm.helper import LLMHelper
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger

MIN_SELECTED_TOOL_COUNT = 4
RECENT_SELECTION_CONTEXT_MESSAGE_LIMIT = 6
RECENT_SELECTION_CONTEXT_MAX_CHARS = 6000
RECENT_SELECTION_CONTEXT_TRUNCATION_PREFIX = "..."
TOOL_GROUP_EXCLUDED_TAGS = frozenset(
    {
        ToolTag.AgentTool.value,
        ToolTag.Read.value,
        ToolTag.Write.value,
        ToolTag.Admin.value,
        ToolTag.Message.value,
        ToolTag.UserInteraction.value,
        ToolTag.TerminalResponse.value,
    }
)

MOVIEPILOT_TOOL_SELECTION_HINT = """

MoviePilot tool-chain hints:
- Tools with the same capability tag belong to the same functional group.
- For multi-step MoviePilot tasks, keep same-tag tools together when relevant.
- Prefer selecting likely next-step tools in the same capability group instead of selecting only the first tool.
"""


class ToolSelectionState(AgentState):
    """工具筛选中间件私有状态。"""

    selected_tool_names: NotRequired[Annotated[list[str] | None, PrivateStateAttr]]
    """当前这条用户请求首轮筛选得到的工具名列表。"""


class ToolSelectionStateUpdate(TypedDict):
    """工具筛选中间件状态更新项。"""

    selected_tool_names: list[str] | None


@dataclass(frozen=True)
class _ToolSelectionAttempt:
    """工具筛选尝试结果，用于统一记录最终日志。"""

    request: ModelRequest
    selected_tool_names: list[str]
    status: str
    detail: str = ""


class ToolSelectorMiddleware(LLMToolSelectorMiddleware):
    """
    使用 provider-neutral JSON 提示执行工具筛选。

    LangChain 默认会通过 `with_structured_output()` 走 provider-specific 的
    结构化输出能力，不同 OpenAI/Anthropic 兼容端点对 `response_format`、
    JSON schema 和工具绑定的支持并不一致。工具筛选只是 Agent 执行前的
    辅助优化，失败时也会恢复使用全部工具，因此这里统一使用文本提示约束
    模型返回 `{"tools": [...]}` 并手动解析，避免在筛选阶段引入额外兼容分支。

    另外，LangChain 原生工具筛选挂在 `wrap_model_call` 上，会在同一条用户请求
    的每次“模型回合”前都重新筛选一次工具。对于会多轮调用工具的复杂任务，
    这会重复消耗一次额外的 LLM 调用。这里改成：
    - `abefore_agent()`：在本轮 Agent 执行开始时筛选一次；
    - `awrap_model_call()`：从 `request.state` 读取首轮筛选结果并复用。
    """

    state_schema = ToolSelectionState

    def __init__(
            self,
            model: BaseChatModel | str | None = None,
            system_prompt: str = DEFAULT_SYSTEM_PROMPT,
            selection_tools: list[Any] | None = None,
            max_tools: int | None = None,
            always_include: list[str] | None = None,
    ) -> None:
        super().__init__(
            model=model,
            system_prompt=self._append_tool_selection_hint(system_prompt),
            max_tools=max_tools,
            always_include=always_include,
        )
        self.selection_tools = selection_tools or []

    @classmethod
    def _render_recent_conversation_context(
            cls,
            messages: list[Any],
    ) -> tuple[str, int]:
        """渲染最近对话上下文，供工具筛选模型理解多轮追问。"""
        rendered_messages = []
        for message in messages:
            if isinstance(message, HumanMessage):
                role = "User"
            elif isinstance(message, AIMessage):
                role = "Assistant"
            else:
                continue

            content = LLMHelper.extract_text_content(message.content).strip()
            if not content:
                continue
            rendered_messages.append(f"{role}: {content}")

        recent_messages = rendered_messages[-RECENT_SELECTION_CONTEXT_MESSAGE_LIMIT:]
        context = "\n\n".join(recent_messages)
        if len(context) > RECENT_SELECTION_CONTEXT_MAX_CHARS:
            context = (
                f"{RECENT_SELECTION_CONTEXT_TRUNCATION_PREFIX}"
                f"{context[-RECENT_SELECTION_CONTEXT_MAX_CHARS:]}"
            )
        return context, len(recent_messages)

    @classmethod
    def _build_contextual_user_message(
            cls,
            messages: list[Any],
            last_user_message: HumanMessage,
    ) -> HumanMessage:
        """根据最近对话构造工具筛选专用用户消息。"""
        context, message_count = cls._render_recent_conversation_context(messages)
        if message_count <= 1:
            return last_user_message

        return HumanMessage(
            content=(
                "Recent conversation context for tool selection:\n"
                f"{context}\n\n"
                "Select tools for the latest user instruction. Use prior assistant "
                "messages and earlier user requests when the latest user message "
                "depends on previous context."
            )
        )

    def _prepare_selection_request(
            self,
            request: ModelRequest[ContextT],
    ) -> Any | None:
        """准备带最近对话上下文的工具筛选请求。"""
        selection_request = super()._prepare_selection_request(request)
        if selection_request is None:
            return None

        contextual_user_message = self._build_contextual_user_message(
            messages=request.messages,
            last_user_message=selection_request.last_user_message,
        )
        if contextual_user_message is selection_request.last_user_message:
            return selection_request
        return replace(selection_request, last_user_message=contextual_user_message)

    @staticmethod
    def _append_tool_selection_hint(system_prompt: str) -> str:
        """追加 MoviePilot 工具组选择提示，避免复杂链路只选中首个工具。"""
        if "MoviePilot tool-chain hints:" in system_prompt:
            return system_prompt
        return f"{system_prompt.rstrip()}{MOVIEPILOT_TOOL_SELECTION_HINT}"

    def _get_tool_selection_limit(self, valid_tool_names: list[str]) -> int:
        """计算补齐筛选结果时允许使用的工具数量上限。"""
        if self.max_tools:
            return min(self.max_tools, len(valid_tool_names))
        return len(valid_tool_names)

    @staticmethod
    def _normalize_tool_tags(tool: BaseTool) -> list[str]:
        """读取工具的业务标签，过滤掉无法表达工具组的通用标签。"""
        tags = getattr(tool, "tags", None) or []
        if isinstance(tags, str):
            tags = [tags]

        normalized_tags = []
        for tag in tags:
            tag_value = getattr(tag, "value", tag)
            if not tag_value:
                continue
            tag_name = str(tag_value)
            if tag_name in TOOL_GROUP_EXCLUDED_TAGS or tag_name in normalized_tags:
                continue
            normalized_tags.append(tag_name)
        return normalized_tags

    @classmethod
    def _build_tool_groups(
            cls,
            available_tools: list[BaseTool],
            valid_tool_names: list[str],
    ) -> list[tuple[str, list[str]]]:
        """根据工具标签构造能力组，保留当前工具列表中的稳定顺序。"""
        valid_tool_set = set(valid_tool_names)
        tool_groups: dict[str, list[str]] = {}
        for tool in available_tools:
            tool_name = getattr(tool, "name", None)
            if not tool_name or tool_name not in valid_tool_set:
                continue
            for tag in cls._normalize_tool_tags(tool):
                group_tool_names = tool_groups.setdefault(tag, [])
                if tool_name not in group_tool_names:
                    group_tool_names.append(tool_name)

        return [
            (tag, tool_names)
            for tag, tool_names in tool_groups.items()
            if len(tool_names) > 1
        ]

    @classmethod
    def _get_matched_tool_groups(
            cls,
            selected_names: list[str],
            available_tools: list[BaseTool],
            valid_tool_names: list[str],
    ) -> list[tuple[str, list[str]]]:
        """返回已选工具命中的标签能力组。"""
        groups_by_tag = {
            tag: tool_names
            for tag, tool_names in cls._build_tool_groups(
                available_tools=available_tools,
                valid_tool_names=valid_tool_names,
            )
        }
        tools_by_name = {
            tool.name: tool
            for tool in available_tools
            if getattr(tool, "name", None)
        }
        matched_groups: list[tuple[str, list[str]]] = []
        seen_tags = set()
        for tool_name in selected_names:
            tool = tools_by_name.get(tool_name)
            if not tool:
                continue
            for tag in cls._normalize_tool_tags(tool):
                if tag in seen_tags or tag not in groups_by_tag:
                    continue
                matched_groups.append((tag, groups_by_tag[tag]))
                seen_tags.add(tag)
        return matched_groups

    def _complete_low_count_selection(
            self,
            selected_tool_names: list[str],
            valid_tool_names: list[str],
            available_tools: list[BaseTool],
    ) -> list[str]:
        """
        当模型只选出极少工具时，按工具标签补齐同组工具。

        工具标签是工具自身声明的能力归属。这里只补齐已经命中的标签组，
        不会把所有工具组都展开。
        """
        limit = self._get_tool_selection_limit(valid_tool_names)
        selected_names = [
            tool_name
            for tool_name in selected_tool_names
            if tool_name in valid_tool_names
        ]
        selected_set = set(selected_names)
        valid_tool_set = set(valid_tool_names)
        completed_names = list(selected_names)
        matched_groups = self._get_matched_tool_groups(
            selected_names=selected_names,
            available_tools=available_tools,
            valid_tool_names=valid_tool_names,
        )
        if not matched_groups:
            return completed_names[:limit]

        matched_group_tool_names = {
            tool_name
            for _, group_tool_names in matched_groups
            for tool_name in group_tool_names
        }
        target_count = min(
            max(MIN_SELECTED_TOOL_COUNT, len(matched_group_tool_names)),
            limit,
        )
        if len(selected_names) >= target_count:
            return selected_names[:limit]

        for _, group_tool_names in matched_groups:
            for tool_name in group_tool_names:
                if tool_name in selected_set or tool_name not in valid_tool_set:
                    continue
                completed_names.append(tool_name)
                selected_set.add(tool_name)
                if len(completed_names) >= target_count:
                    return completed_names[:limit]

        return completed_names[:limit]

    def _process_selection_response(
            self,
            response: dict[str, Any],
            available_tools: list[BaseTool],
            valid_tool_names: list[str],
            request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """
        处理工具筛选响应，并在正常空结果时禁用可筛选工具。
        """
        if response.get("tools") == []:
            always_included_tools: list[BaseTool] = [
                tool
                for tool in request.tools
                if not isinstance(tool, dict) and tool.name in self.always_include
            ]
            provider_tools = [tool for tool in request.tools if isinstance(tool, dict)]
            return request.override(tools=[*always_included_tools, *provider_tools])

        response["tools"] = self._complete_low_count_selection(
            selected_tool_names=[
                tool_name
                for tool_name in response.get("tools", [])
                if isinstance(tool_name, str)
            ],
            valid_tool_names=valid_tool_names,
            available_tools=available_tools,
        )
        modified_request = super()._process_selection_response(
            response,
            available_tools,
            valid_tool_names,
            request,
        )
        return modified_request

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        """
        解析模型返回的 JSON。

        不同模型可能偶发输出 Markdown 围栏或前后说明文本，因此这里从
        响应中提取第一个 JSON 对象作为兜底。
        """
        stripped_text = text.strip()
        if not stripped_text:
            raise ValueError("工具筛选返回了空响应")

        try:
            payload = json.loads(stripped_text)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

        start = stripped_text.find("{")
        end = stripped_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"工具筛选返回的内容不是合法 JSON: {stripped_text}")

        payload = json.loads(stripped_text[start: end + 1])
        if not isinstance(payload, dict):
            raise ValueError("工具筛选 JSON 顶层必须是对象")
        return payload

    @classmethod
    def _render_tool_list(cls, available_tools: list[Any]) -> str:
        """把工具名和描述渲染成稳定的文本列表。"""
        lines = []
        for tool in available_tools:
            tags = cls._normalize_tool_tags(tool)
            tag_text = f" [group tags: {', '.join(tags)}]" if tags else ""
            lines.append(f"- {tool.name}{tag_text}: {tool.description}")
        return "\n".join(lines)

    @classmethod
    def _render_tool_groups(cls, available_tools: list[BaseTool]) -> str:
        """把当前可用工具按标签渲染成能力组提示。"""
        valid_tool_names = [
            tool.name
            for tool in available_tools
            if getattr(tool, "name", None)
        ]
        groups = cls._build_tool_groups(
            available_tools=available_tools,
            valid_tool_names=valid_tool_names,
        )
        if not groups:
            return ""
        rendered_groups = "\n".join(
            f"- {tag}: {', '.join(tool_names)}"
            for tag, tool_names in groups
        )
        return f"Capability groups from tool tags:\n{rendered_groups}\n\n"

    def _build_json_selection_prompt(self, selection_request: Any) -> str:
        """
        生成显式 JSON 输出提示。

        使用纯提示约束可覆盖更多兼容端点，避免在工具筛选阶段依赖某个
        provider 专属的 `response_format` 或 schema 能力。
        """
        limit_instruction = ""
        if self.max_tools:
            limit_instruction = f"- Select up to {self.max_tools} tools. Return an empty array if no tools are relevant."

        return (
            f"{selection_request.system_message}\n\n"
            "Return the answer in JSON only.\n"
            'Use exactly this shape: {"tools": ["tool_name_1", "tool_name_2"]}\n'
            "Rules:\n"
            "- The `tools` field must be a JSON array of strings.\n"
            "- Only use tool names from the allowed list below.\n"
            "- Order tools by relevance, with the most relevant first.\n"
            "- Tools sharing the same capability tag are in the same group; include same-group tools together when relevant.\n"
            f"{limit_instruction}\n"
            "- Do not add explanations, markdown, or extra keys.\n\n"
            f"{self._render_tool_groups(selection_request.available_tools)}"
            "Allowed tools:\n"
            f"{self._render_tool_list(selection_request.available_tools)}"
        )

    def _normalize_selection_response(self, response: Any) -> dict[str, list[str]]:
        """
        解析并标准化显式 JSON 模式的工具筛选结果。
        """
        content = getattr(response, "content", response)
        text = LLMHelper.extract_text_content(content)
        logger.debug(f"工具筛选原始响应: {text}")
        payload = self._parse_json_object(text)

        tools = payload.get("tools")
        if not isinstance(tools, list):
            raise ValueError(f"工具筛选 JSON 缺少 `tools` 数组: {payload}")

        normalized_tools = [
            tool_name for tool_name in tools if isinstance(tool_name, str)
        ]
        logger.debug(f"工具筛选标准化结果: {normalized_tools}")
        return {"tools": normalized_tools}

    async def _aselect_tools_with_json_prompt(
            self, selection_request: Any
    ) -> dict[str, list[str]]:
        """
        使用 JSON 提示执行异步工具筛选。

        :param selection_request: LangChain 工具筛选请求
        :return: 标准化后的工具名列表
        """
        logger.debug("工具筛选走 JSON 提示分支")
        response = await selection_request.model.ainvoke(
            [
                SystemMessage(
                    content=self._build_json_selection_prompt(selection_request)
                ),
                selection_request.last_user_message,
            ]
        )
        return self._normalize_selection_response(response)

    @staticmethod
    def _extract_selected_tool_names(request: ModelRequest) -> list[str]:
        """从已筛选后的请求中提取最终工具名，保留原有顺序。"""
        return [tool.name for tool in request.tools if not isinstance(tool, dict)]

    @staticmethod
    def _count_request_tools(request: ModelRequest) -> int:
        """统计当前请求中的 LangChain 工具数量，不包含 provider 原生工具字典。"""
        return len([tool for tool in request.tools if not isinstance(tool, dict)])

    @classmethod
    def _log_selection_attempt(cls, attempt: _ToolSelectionAttempt) -> None:
        """按工具筛选最终状态记录稳定日志。"""
        tool_count = cls._count_request_tools(attempt.request)
        if attempt.status == "selected":
            selected_text = ", ".join(attempt.selected_tool_names) or "无有效工具"
            logger.info(f"工具筛选结果: {selected_text}")
            return
        if attempt.status == "failed_fallback":
            logger.warning(
                f"工具筛选失败，将恢复使用所有工具（共 {tool_count} 个）: {attempt.detail}"
            )
            return
        if attempt.status == "skipped":
            logger.info(f"工具筛选跳过: {attempt.detail}。")
            return
        if attempt.status == "reused":
            selected_text = ", ".join(attempt.selected_tool_names) or "无有效工具"
            logger.info(f"工具筛选复用已有结果: {selected_text}")

    @staticmethod
    def _apply_selected_tools(
            request: ModelRequest[ContextT],
            selected_tool_names: list[str],
    ) -> ModelRequest[ContextT]:
        """
        将已筛选出的工具集应用到当前模型请求。

        这里只复用首次筛选出的客户端工具名；provider-specific 的 dict 工具仍然
        原样保留，避免破坏 LangChain/provider 自身的工具绑定约定。
        """
        current_tools_by_name = {
            tool.name: tool for tool in request.tools if not isinstance(tool, dict)
        }
        selected_tools = [
            current_tools_by_name[tool_name]
            for tool_name in selected_tool_names
            if tool_name in current_tools_by_name
        ]
        provider_tools = [tool for tool in request.tools if isinstance(tool, dict)]
        return request.override(tools=[*selected_tools, *provider_tools])

    async def _aselect_request_once(
            self, request: ModelRequest[ContextT]
    ) -> ModelRequest[ContextT]:
        """
        执行一次真实工具筛选，并返回筛选后的请求对象。

        这里单独抽成 helper，便于首次筛选后缓存结果，也便于测试覆盖
        “首轮筛选，后续复用”的行为。
        """
        return (await self._aselect_request_once_with_status(request)).request

    async def _aselect_request_once_with_status(
            self, request: ModelRequest[ContextT]
    ) -> _ToolSelectionAttempt:
        """
        执行一次真实工具筛选，并携带最终状态供调用方统一记录日志。
        """
        selection_request = self._prepare_selection_request(request)
        if selection_request is None:
            return _ToolSelectionAttempt(
                request=request,
                selected_tool_names=self._extract_selected_tool_names(request),
                status="skipped",
                detail="没有需要筛选的工具",
            )

        try:
            response = await self._aselect_tools_with_json_prompt(selection_request)
            modified_request = self._process_selection_response(
                response,
                selection_request.available_tools,
                selection_request.valid_tool_names,
                request,
            )
            return _ToolSelectionAttempt(
                request=modified_request,
                selected_tool_names=self._extract_selected_tool_names(modified_request),
                status="selected",
            )
        except Exception as err:
            return _ToolSelectionAttempt(
                request=request,
                selected_tool_names=self._extract_selected_tool_names(request),
                status="failed_fallback",
                detail=str(err),
            )

    async def abefore_agent(  # noqa
            self,
            state: ToolSelectionState,
            runtime: Runtime,  # noqa
            config: RunnableConfig,
    ) -> ToolSelectionStateUpdate | None:  # ty: ignore[invalid-method-override]
        """
        在本轮 Agent 执行开始前完成一次真实工具筛选。

        这样后续多轮 `model -> tools -> model` 循环都只复用这一次结果，
        不会为每次模型回合重复追加一笔 selector LLM 开销。
        """
        if not self.selection_tools or self.model is None:
            detail = "没有可筛选工具" if not self.selection_tools else "未配置筛选模型"
            self._log_selection_attempt(
                _ToolSelectionAttempt(
                    request=ModelRequest(
                        model=self.model,
                        tools=list(self.selection_tools),
                        messages=state["messages"],
                        state=state,
                        runtime=runtime,
                    ),
                    selected_tool_names=[],
                    status="skipped",
                    detail=detail,
                )
            )
            return ToolSelectionStateUpdate(selected_tool_names=None)

        selection_request = ModelRequest(
            model=self.model,
            tools=list(self.selection_tools),
            messages=state["messages"],
            state=state,
            runtime=runtime,
        )
        attempt = await self._aselect_request_once_with_status(selection_request)
        self._log_selection_attempt(attempt)
        selected_tool_names = attempt.selected_tool_names
        return ToolSelectionStateUpdate(selected_tool_names=selected_tool_names)

    async def awrap_model_call(
            self,
            request: ModelRequest[ContextT],
            handler: Callable[
                [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
            ],
    ) -> ModelResponse[ResponseT]:
        """
        从 state 中读取首次筛选结果，并应用到每次模型回合。
        """
        selected_tool_names = request.state.get("selected_tool_names")  # noqa

        # 正常路径下，`abefore_agent()` 已经提前写入状态；这里只保留一层兜底，
        # 兼容直接单测或未来某些绕过 before_agent 的调用场景。
        if (
                selected_tool_names is None
                and self.selection_tools
                and self.model is not None
        ):
            attempt = await self._aselect_request_once_with_status(request)
            self._log_selection_attempt(attempt)
            request = attempt.request
            selected_tool_names = attempt.selected_tool_names
            request.state["selected_tool_names"] = selected_tool_names  # noqa

        if selected_tool_names is not None:
            request = self._apply_selected_tools(request, selected_tool_names)

        return await handler(request)
