import json
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, List, NotRequired, Optional, TypedDict

import yaml  # noqa
from anyio import Path as AsyncPath
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,  # noqa
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from app.agent.middleware.utils import append_to_system_message
from app.agent.policy.sanitizer import sanitize_for_host, summarize_error
from app.agent.skills.metadata import MAX_SKILL_CONTENT_BYTES, SkillMetadata, parse_skill_metadata
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger

SKILL_CONTENT_TRUNCATION_MESSAGE = (
    "SKILL.md exceeds 512 KiB; content contains only the first 512 KiB. "
    "Do not use read_file to bypass this limit."
)


class SkillsState(AgentState):
    """skills 中间件状态。"""

    skills_metadata: NotRequired[Annotated[list[SkillMetadata], PrivateStateAttr]]
    """已加载的 skill 元数据列表，不传播给父 agent。"""


class SkillsStateUpdate(TypedDict):
    """skills 中间件状态更新项。"""

    skills_metadata: list[SkillMetadata]
    """待合并的 skill 元数据列表。"""


class SkillToolInput(BaseModel):
    """Skill 加载工具的输入参数模型。"""

    name: str = Field(
        ...,
        description="Skill name or id from the available skills list.",
    )


def _format_skill_annotations(skill: SkillMetadata) -> str:
    """构建许可证和兼容性说明字符串。"""
    parts: list[str] = []
    if skill.get("license"):
        parts.append(f"License: {skill['license']}")
    if skill.get("compatibility"):
        parts.append(f"Compatibility: {skill['compatibility']}")
    return ", ".join(parts)


async def _alist_skills(source_path: AsyncPath) -> list[SkillMetadata]:
    """异步列出指定路径下的所有技能。

    扫描包含 SKILL.md 的目录并解析其元数据。
    """
    skills: list[SkillMetadata] = []

    # 查找所有技能目录 (包含 SKILL.md 的目录)
    skill_dirs: List[AsyncPath] = []
    async for path in source_path.iterdir():
        if await path.is_dir() and await (path / "SKILL.md").is_file():
            skill_dirs.append(path)

    if not skill_dirs:
        return []

    # 显式按目录名排序，避免文件系统返回顺序不稳定时破坏提示词缓存命中。
    skill_dirs.sort(key=lambda p: p.name.casefold())

    # 解析已下载的 SKILL.md
    for skill_path in skill_dirs:
        skill_md_path = skill_path / "SKILL.md"

        async with await skill_md_path.open("rb") as handle:
            raw_content = await handle.read(MAX_SKILL_CONTENT_BYTES)
        skill_content = raw_content.decode("utf-8", errors="replace")

        # 解析元数据
        skill_metadata = parse_skill_metadata(
            content=skill_content,
            skill_path=str(skill_md_path),
            skill_id=skill_path.name,
        )
        if skill_metadata:
            skills.append(skill_metadata)

    return skills


def _list_skills(source_path: Path) -> list[SkillMetadata]:
    """同步列出指定路径下的所有技能元数据。"""
    if not source_path.exists():
        return []

    skill_dirs = [path for path in source_path.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()]
    if not skill_dirs:
        return []

    skill_dirs.sort(key=lambda p: p.name.casefold())

    skills: list[SkillMetadata] = []
    for skill_path in skill_dirs:
        skill_md_path = skill_path / "SKILL.md"
        with skill_md_path.open("rb") as handle:
            raw_content = handle.read(MAX_SKILL_CONTENT_BYTES)
        skill_content = raw_content.decode("utf-8", errors="replace")
        skill_metadata = parse_skill_metadata(
            content=skill_content,
            skill_path=str(skill_md_path),
            skill_id=skill_path.name,
        )
        if skill_metadata:
            skills.append(skill_metadata)
    return skills


SKILLS_SYSTEM_PROMPT = """
<skills_system>
You have access to a skills library for specialized MoviePilot workflows.

**Available Skills:**

{skills_list}

When the user's request matches a skill description, call the `read_skill` tool with that skill name before taking task actions. Always use `read_skill`, never `read_file`, to load SKILL.md. The tool returns up to 512 KiB of SKILL.md plus the relative paths of all supporting files; if the body is truncated, do not use `read_file` to bypass the limit. Load only the listed supporting files that are actually needed. Do not create or rewrite skills unless the user explicitly asks for skill authoring.
</skills_system>
"""

SKILL_TOOL_NAME = "read_skill"
MOVIEPILOT_API_SKILL_NAME = "moviepilot-api"
SKILL_TOOL_DESCRIPTION = """Reads a MoviePilot skill by name or id.

Available skills:
{skills_catalog}

Call this tool when the user's task matches one of the available skills. It returns up to 512 KiB of SKILL.md content, metadata, and every supporting file path relative to the skill directory. If the content is truncated, do not use read_file to bypass the limit. Always use this tool instead of read_file for SKILL.md. Use read_file only for a listed supporting file when its content is needed. Do not use this for simple tasks that do not need a skill.
"""


def _extract_version(skill_md: Path) -> int:
    """从 SKILL.md 文件中快速提取 version 字段，无法提取时返回 0。"""
    try:
        with skill_md.open("rb") as handle:
            content = handle.read(MAX_SKILL_CONTENT_BYTES).decode("utf-8", errors="replace")
    except Exception as err:
        logger.debug(f"读取技能版本失败: {summarize_error(err)}")
        return 0
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return 0
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return 0
    if not isinstance(frontmatter, dict):
        return 0
    raw = frontmatter.get("version")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _sync_bundled_skills(bundled_dir: Path, target_dir: Path) -> None:
    """将项目自带的技能同步到用户目录。

    - 目标目录中不存在对应技能子目录时，直接复制。
    - 目标目录中已存在时，比较内置与用户目录中 SKILL.md 的 version 字段：
      - 内置版本更高时，直接覆盖用户目录中的旧版本。
      - 版本相同或用户版本更高时，跳过。
    - 内置 SKILL.md 无 version 字段（视为 0）时，不覆盖。

    Parameters
    ----------
    bundled_dir : Path
        项目内置技能目录（如 ``ROOT_PATH / "skills"``）。
    target_dir : Path
        用户配置技能目录（如 ``CONFIG_PATH / "agent" / "skills"``）。
    """
    if not bundled_dir.is_dir():
        return

    target_dir.mkdir(parents=True, exist_ok=True)

    for skill_src in bundled_dir.iterdir():
        if not skill_src.is_dir():
            continue
        skill_md = skill_src / "SKILL.md"
        if not skill_md.is_file():
            continue

        skill_dst = target_dir / skill_src.name

        if not skill_dst.exists():
            # 目标不存在，直接复制
            try:
                shutil.copytree(str(skill_src), str(skill_dst))
                logger.info("已自动复制内置技能 '%s' -> '%s'", skill_src.name, skill_dst)
            except Exception as e:
                logger.warning(
                    "复制内置技能 '%s' 失败: %s",
                    sanitize_for_host(skill_src.name),
                    summarize_error(e),
                )
            continue

        # 目标已存在，比较版本号
        bundled_version = _extract_version(skill_md)
        if bundled_version <= 0:
            # 内置技能无版本号，保持旧逻辑不覆盖
            continue

        user_skill_md = skill_dst / "SKILL.md"
        user_version = _extract_version(user_skill_md) if user_skill_md.is_file() else 0

        if bundled_version <= user_version:
            # 用户版本 >= 内置版本，跳过
            continue

        # 内置版本更高，删除旧版本后覆盖
        try:
            shutil.rmtree(str(skill_dst))
            shutil.copytree(str(skill_src), str(skill_dst))
            logger.info(
                "已更新内置技能 '%s' (v%d -> v%d)",
                skill_src.name,
                user_version,
                bundled_version,
            )
        except Exception as e:
            logger.warning(
                "更新内置技能 '%s' 失败: %s",
                sanitize_for_host(skill_src.name),
                summarize_error(e),
            )


class _SkillToolProvider:
    """Skill 工具的目录扫描和文件读取实现。"""

    def __init__(self, *, sources: list[str]) -> None:
        """初始化 Skill 工具数据源。"""
        self._sources = sources
        self._allowed_api_operations: set[str] = set()
        self._api_scope_declared = False

    @property
    def api_scope_declared(self) -> bool:
        """返回当前会话是否加载过声明 API 操作范围的 Skill。"""
        return self._api_scope_declared

    def reset_api_scope(self) -> None:
        """在新一轮 Agent 执行前清除上轮 Skill 的 API 操作范围。"""
        self._allowed_api_operations.clear()
        self._api_scope_declared = False

    def is_api_operation_allowed(self, operation_id: str) -> bool:
        """判断 operation ID 是否位于当前已加载 Skill 的联合授权范围。"""
        return operation_id in self._allowed_api_operations

    async def ensure_api_operation_allowed(self, operation_id: str) -> bool:
        """在 API 网关首次调用时自动加载内置网关 Skill 并校验操作范围。"""
        if not operation_id:
            return False
        if self.is_api_operation_allowed(operation_id):
            return True
        await self.load_skill(MOVIEPILOT_API_SKILL_NAME)
        return self.is_api_operation_allowed(operation_id)

    @staticmethod
    def _normalize_name(value: object) -> str:
        """标准化技能名称用于匹配。"""
        return str(value or "").strip().casefold()

    @classmethod
    def _skill_matches(cls, skill: SkillMetadata, query: str) -> bool:
        """判断技能元数据是否匹配用户提供的名称。"""
        normalized_query = cls._normalize_name(query)
        candidates = [
            skill.get("id"),
            skill.get("name"),
        ]
        return any(cls._normalize_name(candidate) == normalized_query for candidate in candidates)

    async def _find_skill(self, name: str) -> Optional[SkillMetadata]:
        """从中间件配置的 skills 目录中查找指定技能。"""
        all_skills: dict[str, SkillMetadata] = {}
        for source_path in self._sources:
            skill_source_path = AsyncPath(source_path)
            if not await skill_source_path.exists():
                continue
            for skill in await _alist_skills(skill_source_path):
                all_skills[skill["name"]] = skill

        for skill in all_skills.values():
            if self._skill_matches(skill, name):
                return skill
        return None

    @staticmethod
    async def _read_skill_content(skill_path: str) -> tuple[str, bool]:
        """读取最多 512 KiB 技能主体，并报告内容是否截断。"""
        path = AsyncPath(skill_path)
        async with await path.open("rb") as handle:
            raw_content = await handle.read(MAX_SKILL_CONTENT_BYTES + 1)
        truncated = len(raw_content) > MAX_SKILL_CONTENT_BYTES
        bounded_content = raw_content[:MAX_SKILL_CONTENT_BYTES]
        decode_errors = "ignore" if truncated else "replace"
        return bounded_content.decode("utf-8", errors=decode_errors), truncated

    @staticmethod
    async def _list_supporting_files(skill_path: str) -> list[str]:
        """列出技能目录内除 SKILL.md 外的全部普通文件相对路径。"""
        skill_file = AsyncPath(skill_path)
        skill_root = skill_file.parent
        root_path = Path(str(skill_root))
        supporting_files = []
        async for path in skill_root.rglob("*", recurse_symlinks=False):
            if await path.is_symlink() or not await path.is_file():
                continue
            relative_path = Path(str(path)).relative_to(root_path).as_posix()
            if relative_path != "SKILL.md":
                supporting_files.append(relative_path)
        return sorted(supporting_files, key=str.casefold)

    async def load_skill(self, name: str) -> str:
        """加载指定 Skill 的受限主体和辅助文件列表并返回 JSON 字符串。"""
        logger.info(f"加载 Skill: name={sanitize_for_host(name)}")
        try:
            skill = await self._find_skill(name)
            if not skill:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"未找到 Skill: {name}",
                    },
                    ensure_ascii=False,
                )

            content, truncated = await self._read_skill_content(skill["path"])
            supporting_files = await self._list_supporting_files(skill["path"])
            declared_operations = skill.get("allowed_api_operations", [])
            if declared_operations:
                self._api_scope_declared = True
                self._allowed_api_operations.update(declared_operations)
            return json.dumps(
                {
                    "success": True,
                    "skill": {
                        "id": skill.get("id"),
                        "name": skill.get("name"),
                        "description": skill.get("description"),
                        "path": skill.get("path"),
                        "allowed_tools": skill.get("allowed_tools", []),
                        "allowed_api_operations": declared_operations,
                    },
                    "content": content,
                    "content_limit_bytes": MAX_SKILL_CONTENT_BYTES,
                    "supporting_files": supporting_files,
                    "truncated": truncated,
                    "truncation_message": SKILL_CONTENT_TRUNCATION_MESSAGE if truncated else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as err:
            error_summary = summarize_error(err)
            logger.error(f"加载 Skill 失败: {error_summary}")
            return json.dumps(
                {
                    "success": False,
                    "message": f"加载 Skill 时发生错误: {error_summary}",
                },
                ensure_ascii=False,
            )


def _format_skill_tool_catalog(skills: list[SkillMetadata]) -> str:
    """渲染 Skill 工具描述中的可用技能目录。"""
    if not skills:
        return "(No skills are currently available.)"
    return "\n".join(f"- {skill['id']}: {skill['name']} - {skill['description']}" for skill in skills)


class SkillsMiddleware(AgentMiddleware[SkillsState, ContextT, ResponseT]):  # noqa
    """加载并向系统提示词注入 Agent Skill 的中间件。

    按源顺序加载 Skill，后加载的会覆盖重名的。
    启动时自动将项目内置技能（bundled_skills_dir）同步到用户技能目录。
    """

    state_schema = SkillsState

    def __init__(
        self,
        *,
        sources: list[str],
        bundled_skills_dir: str | None = None,
        stream_handler: Optional[Any] = None,
    ) -> None:
        """初始化 Skill 中间件。

        Parameters
        ----------
        sources : list[str]
            用户技能目录列表。
        bundled_skills_dir : str | None
            项目内置技能目录路径。若提供，在首次加载前会将其中不存在于
            sources 首个目录的技能自动复制过去。
        stream_handler : Optional[Any]
            流式输出处理器，用于记录 read_skill 工具调用摘要。
        """
        self.sources = sources
        self.bundled_skills_dir = bundled_skills_dir
        self.stream_handler = stream_handler
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT
        self._skill_provider = _SkillToolProvider(sources=sources)
        # read_skill 保持为中间件私有 StructuredTool：不注册到 MoviePilotToolFactory，
        # 因而不会进入 HTTP/MCP 工具目录，也不会经过 MoviePilotTool 的 64 KiB 结果裁剪。
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._skill_provider.load_skill,
                name=SKILL_TOOL_NAME,
                description=SKILL_TOOL_DESCRIPTION.format(
                    skills_catalog=_format_skill_tool_catalog(self._load_skills_metadata())
                ),
                args_schema=SkillToolInput,
                tags=[ToolTag.Read, ToolTag.Skill],
            )
        ]

    def _sync_bundled_skills(self) -> None:
        """将项目内置 Skill 同步到首个用户技能目录。"""
        if not self.bundled_skills_dir or not self.sources:
            return
        bundled = Path(self.bundled_skills_dir)
        target = Path(self.sources[0])
        try:
            _sync_bundled_skills(bundled, target)
        except Exception as e:
            logger.warning(f"同步内置技能失败: {summarize_error(e)}")

    def _load_skills_metadata(self) -> list[SkillMetadata]:
        """同步加载当前配置目录中的 Skill 元数据。"""
        self._sync_bundled_skills()
        all_skills: dict[str, SkillMetadata] = {}
        for source_path in self.sources:
            for skill in _list_skills(Path(source_path)):
                all_skills[skill["name"]] = skill
        return list(all_skills.values())

    def _refresh_skill_tool_description(self, skills: list[SkillMetadata]) -> None:
        """刷新 read_skill 工具描述中的可用技能目录。"""
        if not self.tools:
            return
        self.tools[0].description = SKILL_TOOL_DESCRIPTION.format(skills_catalog=_format_skill_tool_catalog(skills))

    @staticmethod
    def _format_skills_list(skills: list[SkillMetadata]) -> str:
        """格式化技能元数据列表用于系统提示词。"""
        if not skills:
            return "(No skills available yet.)"

        lines = []
        for skill in skills:
            annotations = _format_skill_annotations(skill)
            desc_line = f"- **{skill['id']}**: {skill['name']} - {skill['description']}"
            if annotations:
                desc_line += f" ({annotations})"
            lines.append(desc_line)
            if skill["allowed_tools"]:
                lines.append(f"  -> Allowed tools: {', '.join(skill['allowed_tools'])}")
            if skill["allowed_api_operations"]:
                lines.append(f"  -> Allowed API operations: {', '.join(skill['allowed_api_operations'])}")

        return "\n".join(lines)

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """将技能文档注入模型请求的系统消息中。"""
        skills_metadata = request.state.get("skills_metadata", [])  # noqa
        skills_list = self._format_skills_list(skills_metadata)

        skills_section = self.system_prompt_template.format(
            skills_list=skills_list,
        )

        new_system_message = append_to_system_message(request.system_message, skills_section)

        return request.override(system_message=new_system_message)

    async def abefore_agent(  # noqa
        self, state: SkillsState, runtime: Runtime, config: RunnableConfig
    ) -> SkillsStateUpdate | None:  # ty: ignore[invalid-method-override]
        """在 Agent 执行前异步加载技能元数据。

        首次加载时，会先将内置技能同步到用户目录（如不存在）。
        """
        self._sync_bundled_skills()
        self._skill_provider.reset_api_scope()

        all_skills: dict[str, SkillMetadata] = {}

        # 遍历源按顺序加载技能，重名时后者覆盖前者
        for source_path in self.sources:
            skill_source_path = AsyncPath(source_path)
            if not await skill_source_path.exists():
                await skill_source_path.mkdir(parents=True, exist_ok=True)
                continue
            source_skills = await _alist_skills(skill_source_path)
            for skill in source_skills:
                all_skills[skill["name"]] = skill

        skills = list(all_skills.values())
        self._refresh_skill_tool_description(skills)
        return SkillsStateUpdate(skills_metadata=skills)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """在模型调用时注入技能文档。"""
        modified_request = self.modify_request(request)
        return await handler(modified_request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在 read_skill 工具执行时输出当前模式对应的执行信息。"""
        tool = request.tool
        tool_name = getattr(tool, "name", None)
        tool_call = request.tool_call or {}
        tool_args = tool_call.get("args") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}

        if tool_name == "moviepilot_api":
            operation_id = str(tool_args.get("operation_id") or "")
            if not await self._skill_provider.ensure_api_operation_allowed(operation_id):
                logger.warning(f"Skill API 操作范围拒绝调用: operation={sanitize_for_host(operation_id)}")
                return ToolMessage(
                    content=json.dumps(
                        {
                            "success": False,
                            "error": "skill_operation_denied",
                            "message": (
                                "当前已加载 Skill 未授权该 MoviePilot API 操作，"
                                "请先加载声明了该 operation 的领域 Skill。"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    tool_call_id=str(tool_call.get("id") or ""),
                    name="moviepilot_api",
                )

        if tool_name != SKILL_TOOL_NAME:
            return await handler(request)

        logged_args = sanitize_for_host(tool_args)
        if not isinstance(logged_args, dict):
            logged_args = {}
        logger.info(f"开始执行 Skill 工具: name={logged_args.get('name') or '-'}")
        if self.stream_handler and getattr(self.stream_handler, "is_streaming", False):
            self.stream_handler.report_tool_call(
                tool_name=SKILL_TOOL_NAME,
                tool_message=f"读取技能说明：{logged_args.get('name') or '-'}",
                tool_kwargs=tool_args,
            )
        try:
            result = await handler(request)
        except Exception as err:
            logger.error(f"Skill 工具执行失败: error={summarize_error(err)}")
            raise
        logger.info("Skill 工具执行完成")
        return result


__all__ = [
    "MAX_SKILL_CONTENT_BYTES",
    "MOVIEPILOT_API_SKILL_NAME",
    "SKILL_TOOL_NAME",
    "SkillMetadata",
    "SkillsMiddleware",
]
