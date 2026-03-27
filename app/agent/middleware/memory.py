from collections.abc import Awaitable, Callable
from typing import Annotated, NotRequired, TypedDict, Dict

from anyio import Path as AsyncPath
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,  # noqa
    ResponseT,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.agent.middleware.utils import append_to_system_message
from app.log import logger

# 记忆文件最大限制为 100KB，防止单文件过大导致上下文溢出
MAX_MEMORY_FILE_SIZE = 100 * 1024

# 默认记忆文件名（用户主记忆）
DEFAULT_MEMORY_FILE = "MEMORY.md"


class MemoryState(AgentState):
    """`MemoryMiddleware` 的状态模型。

    属性：
        memory_contents: 将源路径映射到其加载内容的字典。
            标记为私有，因此不包含在最终的代理状态中。
        memory_empty: 记忆文件是否为空或不存在。
            标记为私有，用于判断是否需要触发初始化引导流程。
    """

    memory_contents: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]
    memory_empty: NotRequired[Annotated[bool, PrivateStateAttr]]


class MemoryStateUpdate(TypedDict):
    """`MemoryMiddleware` 的状态更新。"""

    memory_contents: dict[str, str]
    memory_empty: bool


MEMORY_SYSTEM_PROMPT = """<agent_memory>
The following memory files were loaded from your memory directory: `{memory_dir}`
You can create, edit, or organize any `.md` files in this directory to manage your knowledge.

{agent_memory}
</agent_memory>

<memory_guidelines>
    The above <agent_memory> was loaded from `.md` files in your memory directory (`{memory_dir}`). As you learn from your interactions with the user, you can save new knowledge by calling the `edit_file` or `write_file` tool on files in this directory.

    **Memory file organization:**
    - All `.md` files in `{memory_dir}` are automatically loaded as memory.
    - `MEMORY.md` is the default/primary memory file for general user preferences and profile.
    - You may create additional `.md` files to organize knowledge by topic (e.g., `MEDIA_RULES.md`, `DOWNLOAD_PREFERENCES.md`, `SITE_CONFIGS.md`, etc.).
    - Keep each file focused on a specific domain or topic for better organization.
    - Subdirectories are NOT scanned — only `.md` files directly in `{memory_dir}`.

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
    - Do NOT record daily activities or task execution history in memory files - these are automatically tracked in the activity log system (see <activity_log>). Memory files are only for long-term knowledge, preferences, and patterns.

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

MEMORY_ONBOARDING_PROMPT = """<agent_memory>
(No memory loaded — this is a brand new user with no saved preferences.)
Memory directory: {memory_dir}
Default memory file: {memory_file}
</agent_memory>

<memory_onboarding>
    **IMPORTANT — First-time user detected!**

    The memory directory is currently empty. This means this is likely the user's first interaction, or their preferences have been reset.

    **Your MANDATORY first action in this conversation:**
    Before doing ANYTHING else (before answering questions, before calling tools, before performing any task), you MUST proactively greet the user warmly and ask them about their preferences so you can provide personalized service going forward. Specifically, ask about:

    1. **How to address the user** — Ask what name or nickname they'd like you to call them (e.g., a real name, a nickname, or a fun title). This is the top priority for building a personal connection.
    2. **Communication style preference** — Do they prefer a cute/playful tone (with emojis), a formal/professional tone, a concise/minimalist style, or something else?
    3. **Media preferences** — What types of media do they primarily care about? (e.g., movies, TV shows, anime, documentaries, etc.)
    4. **Quality preferences** — Do they have preferred video quality (4K, 1080p), codecs (H.265, H.264), or subtitle language preferences?
    5. **Any other special requests** — Anything else they'd like you to always keep in mind?

    **After the user replies**, you MUST immediately:
    1. Use the `write_file` tool to save ALL their preferences to the memory file at: `{memory_file}`
    2. Format the memory file in clean Markdown with clear sections (e.g., `## User Profile`, `## Communication Style`, `## Media Preferences`, etc.)
    3. The `## User Profile` section MUST include the user's preferred name/nickname at the top
    4. Only AFTER saving the preferences, proceed to help with whatever the user originally asked about (if anything)
    5. From this point on, always address the user by their preferred name/nickname in conversations
    6. You may also create additional `.md` files in the memory directory (`{memory_dir}`) for different topics as needed.

    **If the user skips the preference questions** and directly asks you to do something:
    - Go ahead and help them with their request first
    - But still ask about their preferences naturally at the end of the interaction
    - Save whatever you learn about them (implicit or explicit) to the memory file

    **Example onboarding flow:**
    The greeting should introduce yourself, explain this is the first meeting, and ask the above questions in a numbered list. Adapt the tone to your persona defined in the base system prompt.
</memory_onboarding>

<memory_guidelines>
    Your memory directory is at: {memory_dir}. You can save new knowledge by calling the `edit_file` or `write_file` tool on any `.md` file in this directory.

    **Memory file organization:**
    - `MEMORY.md` is the default/primary memory file for general user preferences and profile.
    - You may create additional `.md` files to organize knowledge by topic.
    - All `.md` files directly in the memory directory are automatically loaded on each conversation.

    **Learning from feedback:**
    - One of your MAIN PRIORITIES is to learn from your interactions with the user. These learnings can be implicit or explicit. This means that in the future, you will remember this important information.
    - When you need to remember something, updating memory must be your FIRST, IMMEDIATE action - before responding to the user, before calling other tools, before doing anything else. Just update memory immediately.
    - When user says something is better/worse, capture WHY and encode it as a pattern.
    - Each correction is a chance to improve permanently - don't just fix the immediate issue, update your instructions.
    - The user might not explicitly ask you to remember something, but if they provide information that is useful for future use, you should update your memories immediately.

    **When to update memories:**
    - When the user explicitly asks you to remember something
    - When the user describes your role or how you should behave
    - When the user gives feedback on your work
    - When the user provides information required for tool use
    - When you discover new patterns or preferences

    **When to NOT update memories:**
    - Temporary/transient information
    - One-time task requests
    - Simple questions, acknowledgments, or small talk
    - Never store API keys, access tokens, passwords, or credentials
    - Do NOT record daily activities in memory files — those go to the activity log
</memory_guidelines>
"""


class MemoryMiddleware(AgentMiddleware[MemoryState, ContextT, ResponseT]):  # noqa
    """从代理记忆目录加载所有 MD 文件作为记忆的中间件。

    自动扫描指定目录下的所有 `.md` 文件，加载其内容并注入到系统提示词中。
    支持多文件记忆组织：用户可以创建多个 `.md` 文件来按主题组织知识。

    参数：
        memory_dir: 记忆文件目录路径。
    """

    state_schema = MemoryState

    def __init__(
        self,
        *,
        memory_dir: str,
    ) -> None:
        """初始化记忆中间件。

        参数：
            memory_dir: 记忆文件目录路径（例如，`"/config/agent"`）。
                        该目录下所有 `.md` 文件都会被自动加载为记忆。
        """
        self.memory_dir = memory_dir
        self.default_memory_file = str(AsyncPath(memory_dir) / DEFAULT_MEMORY_FILE)

    @staticmethod
    def _is_memory_empty(contents: dict[str, str]) -> bool:
        """判断记忆内容是否为空。

        检查所有源文件的内容，如果全部为空或仅包含空白字符则返回 True。

        参数：
            contents: 将源路径映射到内容的字典。

        返回：
            如果记忆为空则返回 True，否则返回 False。
        """
        if not contents:
            return True
        return all(not content.strip() for content in contents.values())

    def _format_agent_memory(
        self, contents: dict[str, str], memory_empty: bool = False
    ) -> str:
        """格式化记忆，将位置和内容成对组合。

        当记忆为空时，返回初始化引导提示词，引导智能体主动询问用户偏好。
        当记忆非空时，返回标准记忆系统提示词，包含所有加载的文件内容。

        参数：
            contents: 将源路径映射到内容的字典。
            memory_empty: 记忆是否为空的标志位。

        返回：
            在 <agent_memory> 标签中包装了位置+内容对的格式化字符串。
        """
        # 记忆为空时返回初始化引导提示词
        if memory_empty or self._is_memory_empty(contents):
            return MEMORY_ONBOARDING_PROMPT.format(
                memory_dir=self.memory_dir,
                memory_file=self.default_memory_file,
            )

        # 按文件名排序，确保 MEMORY.md 排在最前面
        sorted_paths = sorted(
            [p for p in contents if contents[p].strip()],
            key=lambda p: (0 if AsyncPath(p).name == DEFAULT_MEMORY_FILE else 1, p),
        )

        if not sorted_paths:
            return MEMORY_ONBOARDING_PROMPT.format(
                memory_dir=self.memory_dir,
                memory_file=self.default_memory_file,
            )

        sections = []
        for path in sorted_paths:
            file_name = AsyncPath(path).name
            sections.append(f"### {file_name}\n**Path:** `{path}`\n\n{contents[path]}")

        memory_body = "\n\n---\n\n".join(sections)
        return MEMORY_SYSTEM_PROMPT.format(
            agent_memory=memory_body,
            memory_dir=self.memory_dir,
        )

    async def _scan_memory_files(self) -> list[str]:
        """扫描记忆目录下的所有 .md 文件。

        仅扫描目录下直接存在的 `.md` 文件（不递归子目录）。
        文件大小超过限制的将被跳过。

        返回：
            发现的 .md 文件路径列表。
        """
        dir_path = AsyncPath(self.memory_dir)
        if not await dir_path.exists():
            return []

        md_files: list[str] = []
        async for entry in dir_path.iterdir():
            if await entry.is_file() and entry.name.lower().endswith(".md"):
                md_files.append(str(entry))

        return md_files

    async def abefore_agent(
        self,
        state: MemoryState,
        runtime: Runtime,  # noqa
        config: RunnableConfig,
    ) -> MemoryStateUpdate | None:
        """在代理执行前扫描记忆目录并加载所有 .md 文件的内容。

        自动发现目录下所有 `.md` 文件并加载其内容到状态中。
        如果状态中尚未存在则进行加载。
        同时检测记忆文件是否为空，设置 memory_empty 标志位，
        以便在系统提示词中触发初始化引导流程。

        参数：
            state: 当前代理状态。
            runtime: 运行时上下文。
            config: Runnable 配置。

        返回：
            填充了 memory_contents 和 memory_empty 的状态更新。
        """
        # 如果已经加载则跳过
        if "memory_contents" in state:
            return None

        # 扫描目录下所有 .md 文件
        md_files = await self._scan_memory_files()

        contents: Dict[str, str] = {}
        for path in md_files:
            file_path = AsyncPath(path)
            try:
                # 检查文件大小
                stat = await file_path.stat()
                if stat.st_size > MAX_MEMORY_FILE_SIZE:
                    logger.warning(
                        "Skipping memory file %s: too large (%d bytes, max %d)",
                        path,
                        stat.st_size,
                        MAX_MEMORY_FILE_SIZE,
                    )
                    continue
                contents[path] = await file_path.read_text(encoding="utf-8")
                logger.debug("Loaded memory from: %s", path)
            except Exception as e:
                logger.warning("Failed to read memory file %s: %s", path, e)

        if contents:
            logger.info(
                "Loaded %d memory file(s) from %s: %s",
                len(contents),
                self.memory_dir,
                [AsyncPath(p).name for p in contents],
            )

        # 检测记忆是否为空（文件不存在、文件内容为空白）
        is_empty = self._is_memory_empty(contents)
        if is_empty:
            logger.info(
                "Memory is empty, onboarding prompt will be activated for user preference collection."
            )

        return MemoryStateUpdate(memory_contents=contents, memory_empty=is_empty)

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """将记忆内容注入系统消息。

        参数：
            request: 要修改的模型请求。

        返回：
            将记忆注入系统消息后的修改后请求。
        """
        contents = request.state.get("memory_contents", {})  # noqa
        memory_empty = request.state.get("memory_empty", False)  # noqa
        agent_memory = self._format_agent_memory(contents, memory_empty=memory_empty)

        new_system_message = append_to_system_message(
            request.system_message, agent_memory
        )

        return request.override(system_message=new_system_message)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
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
