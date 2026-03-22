from collections.abc import Awaitable, Callable
from typing import Annotated, NotRequired, TypedDict, Dict

from aiopathlib import AsyncPath
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,  # noqa
    ResponseT,
)
from langchain_core.messages import SystemMessage, ContentBlock
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.log import logger


class MemoryState(AgentState):
    """`MemoryMiddleware` 的状态模型。

    属性：
        memory_contents: 将源路径映射到其加载内容的字典。
            标记为私有，因此不包含在最终的代理状态中。
    """

    memory_contents: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]


class MemoryStateUpdate(TypedDict):
    """`MemoryMiddleware` 的状态更新。"""

    memory_contents: dict[str, str]


MEMORY_SYSTEM_PROMPT = """<agent_memory>
{agent_memory}
</agent_memory>

<memory_guidelines>
    The above <agent_memory> was loaded in from files in your filesystem. As you learn from your interactions with the user, you can save new knowledge by calling the `edit_file` or `write_file` tool.

    **Learning from feedback:**
    - One of your MAIN PRIORITIES is to learn from your interactions with the user. These learnings can be implicit or explicit. This means that in the future, you will remember this important information.
    - When you need to remember something, updating memory must be your FIRST, IMMEDIATE action - before responding to the user, before calling other tools, before doing anything else. Just update memory immediately.
    - When user says something is better/worse, capture WHY and encode it as a pattern.
    - Each correction is a chance to improve permanently - don't just fix the immediate issue, update your instructions.
    - A great opportunity to update your memories is when the user interrupts a tool call and provides feedback. You should update your memories immediately before revising the tool call.
    - Look for the underlying principle behind corrections, not just the specific mistake.
    - The user might not explicitly ask you to remember something, but if they provide information that is useful for future use, you should update your memories immediately.

    **Asking for information:**
    - If you lack context to perform an action (e.g. send a Slack DM, requires a user ID/email) you should explicitly ask the user for this information.
    - It is preferred for you to ask for information, don't assume anything that you do not know!
    - When the user provides information that is useful for future use, you should update your memories immediately.

    **When to update memories:**
    - When the user explicitly asks you to remember something (e.g., "remember my email", "save this preference")
    - When the user describes your role or how you should behave (e.g., "you are a web researcher", "always do X")
    - When the user gives feedback on your work - capture what was wrong and how to improve
    - When the user provides information required for tool use (e.g., slack channel ID, email addresses)
    - When the user provides context useful for future tasks, such as how to use tools, or which actions to take in a particular situation
    - When you discover new patterns or preferences (coding styles, conventions, workflows)

    **When to NOT update memories:**
    - When the information is temporary or transient (e.g., "I'm running late", "I'm on my phone right now")
    - When the information is a one-time task request (e.g., "Find me a recipe", "What's 25 * 4?")
    - When the information is a simple question that doesn't reveal lasting preferences (e.g., "What day is it?", "Can you explain X?")
    - When the information is an acknowledgment or small talk (e.g., "Sounds good!", "Hello", "Thanks for that")
    - When the information is stale or irrelevant in future conversations
    - Never store API keys, access tokens, passwords, or any other credentials in any file, memory, or system prompt.
    - If the user asks where to put API keys or provides an API key, do NOT echo or save it.

    **Examples:**
    Example 1 (remembering user information):
    User: Can you connect to my google account?
    Agent: Sure, I'll connect to your google account, what's your google account email?
    User: john@example.com
    Agent: Let me save this to my memory.
    Tool Call: edit_file(...) -> remembers that the user's google account email is john@example.com

    Example 2 (remembering implicit user preferences):
    User: Can you write me an example for creating a deep agent in LangChain?
    Agent: Sure, I'll write you an example for creating a deep agent in LangChain <example code in Python>
    User: Can you do this in JavaScript
    Agent: Let me save this to my memory.
    Tool Call: edit_file(...) -> remembers that the user prefers to get LangChain code examples in JavaScript
    Agent: Sure, here is the JavaScript example<example code in JavaScript>

    Example 3 (do not remember transient information):
    User: I'm going to play basketball tonight so I will be offline for a few hours.
    Agent: Okay I'll add a block to your calendar.
    Tool Call: create_calendar_event(...) -> just calls a tool, does not commit anything to memory, as it is transient information
</memory_guidelines>
"""


def append_to_system_message(
        system_message: SystemMessage | None,
        text: str,
) -> SystemMessage:
    """将文本追加到系统消息。

    参数：
        system_message: 现有的系统消息或 None。
        text: 要添加到系统消息的文本。

    返回：
        追加了文本的新 SystemMessage。
    """
    new_content: list[ContentBlock] = list(system_message.content_blocks) if system_message else []  # noqa
    if new_content:
        text = f"\n\n{text}"
    new_content.append({"type": "text", "text": text})
    return SystemMessage(content_blocks=new_content)


class MemoryMiddleware(AgentMiddleware[MemoryState, ContextT, ResponseT]):  # noqa
    """从 `AGENTS.md` 文件加载代理记忆的中间件。

    从配置的源加载记忆内容并注入到系统提示词中。

    支持对多个源进行合并。

    参数：
        sources: 包含指定路径和名称的 `MemorySource` 配置列表。
    """

    state_schema = MemoryState

    def __init__(
            self,
            *,
            sources: list[str],
    ) -> None:
        """初始化记忆中间件。

        参数：
            sources: 要加载的记忆文件路径列表（例如，`["~/.deepagents/AGENTS.md",
                     "./.deepagents/AGENTS.md"]`）。

                     显示名称自动从路径中派生。

                     按顺序加载源。
        """
        self.sources = sources

    def _format_agent_memory(self, contents: dict[str, str]) -> str:
        """格式化记忆，将位置和内容成对组合。

        参数：
            contents: 将源路径映射到内容的字典。

        返回：
            在 <agent_memory> 标签中包装了位置+内容对的格式化字符串。
        """
        if not contents:
            return MEMORY_SYSTEM_PROMPT.format(
                agent_memory=f"(No memory loaded), but you can add some by calling the `write_file` tool to the file: {self.sources[0]}.")

        sections = [f"{path}\n{contents[path]}" for path in self.sources if contents.get(path)]

        if not sections:
            return MEMORY_SYSTEM_PROMPT.format(agent_memory="(No memory loaded)")

        memory_body = "\n\n".join(sections)
        return MEMORY_SYSTEM_PROMPT.format(agent_memory=memory_body)

    async def abefore_agent(self, state: MemoryState, runtime: Runtime,  # noqa
                            config: RunnableConfig) -> MemoryStateUpdate | None:
        """在代理执行前加载记忆内容。

        从所有配置的源加载记忆并存储在状态中。
        如果状态中尚未存在则进行加载。

        参数：
            state: 当前代理状态。
            runtime: 运行时上下文。
            config: Runnable 配置。

        返回：
            填充了 memory_contents 的状态更新。
        """
        # 如果已经加载则跳过
        if "memory_contents" in state:
            return None

        contents: Dict[str, str] = {}
        for path in self.sources:
            file_path = AsyncPath(path)
            if await file_path.exists():
                contents[path] = await file_path.read_text()
                logger.debug("Loaded memory from: %s", path)

        return MemoryStateUpdate(memory_contents=contents)

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """将记忆内容注入系统消息。

        参数：
            request: 要修改的模型请求。

        返回：
            将记忆注入系统消息后的修改后请求。
        """
        contents = request.state.get("memory_contents", {})  # noqa
        agent_memory = self._format_agent_memory(contents)

        new_system_message = append_to_system_message(request.system_message, agent_memory)

        return request.override(system_message=new_system_message)

    async def awrap_model_call(
            self,
            request: ModelRequest[ContextT],
            handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """异步包装模型调用，将记忆注入系统提示词。

        参数：
            request: 正在处理的模型请求。
            handler: 使用修改后的请求进行调用的异步处理函数。

        返回：
            来自处理函数的模型响应。
        """
        modified_request = self.modify_request(request)
        return await handler(modified_request)
