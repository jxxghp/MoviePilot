import json
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, List, Optional
from typing import NotRequired, TypedDict

import yaml  # noqa
from anyio import Path as AsyncPath
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain.agents.middleware.types import PrivateStateAttr  # noqa
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from app.agent.middleware.utils import append_to_system_message
from app.agent.tools.tags import ToolTag
from app.log import logger

# 磁盘读取上限与模型返回上限分离，避免异常大的 Skill 文件撑爆内存或上下文。
MAX_SKILL_FILE_SIZE = 1 * 1024 * 1024
MAX_SKILL_RESULT_CHARS = 64 * 1024
SKILL_CONTENT_TRUNCATION_SUFFIX = "\n...(Skill 内容已截断)"

# Agent Skills 规范约束 (https://agentskills.io/specification)
MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_DESCRIPTION_LENGTH = 1024
MAX_SKILL_COMPATIBILITY_LENGTH = 500


class SkillMetadata(TypedDict):
    """Skill 元数据，符合 Agent Skills 规范。"""

    path: str
    """SKILL.md 文件路径。"""

    id: str
    """Skill 标识符。
    约束: 1-64 字符，仅限小写字母/数字/连字符，不能以连字符开头或结尾，无连续连字符，需与父目录名一致。
    """

    name: str
    """Skill 名称。
    约束: Skill中文描述。
    """

    version: int
    """Skill 版本号。
    用于内置技能的版本管理，同步时比较版本号决定是否覆盖用户目录中的旧版本。
    """

    description: str
    """Skill 功能描述。
    约束: 1-1024 字符，应说明功能及适用场景。
    """

    license: str | None
    """许可证信息。"""

    compatibility: str | None
    """环境依赖或兼容性要求 (最多 500 字符)。"""

    metadata: dict[str, str]
    """附加元数据。"""

    allowed_tools: list[str]
    """(实验性) Skill 建议使用的工具列表。"""


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


def _parse_skill_metadata(  # noqa: C901
    content: str,
    skill_path: str,
    skill_id: str,
) -> SkillMetadata | None:
    """从 SKILL.md 内容中解析 YAML 前言并验证元数据。"""
    if len(content) > MAX_SKILL_FILE_SIZE:
        logger.warning(
            "Skipping %s: content too large (%d bytes)", skill_path, len(content)
        )
        return None

    # 匹配 --- 分隔的 YAML 前言
    frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
    match = re.match(frontmatter_pattern, content, re.DOTALL)
    if not match:
        logger.warning("Skipping %s: no valid YAML frontmatter found", skill_path)
        return None
    frontmatter_str = match.group(1)

    # 解析 YAML
    try:
        frontmatter_data = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in %s: %s", skill_path, e)
        return None

    if not isinstance(frontmatter_data, dict):
        logger.warning("Skipping %s: frontmatter is not a mapping", skill_path)
        return None

    # SKill名称和描述
    name = str(frontmatter_data.get("name", "")).strip()
    description = str(frontmatter_data.get("description", "")).strip()
    if not name or not description:
        logger.warning(
            "Skipping %s: missing required 'name' or 'description'", skill_path
        )
        return None
    description_str = description
    if len(description_str) > MAX_SKILL_DESCRIPTION_LENGTH:
        logger.warning(
            "Description exceeds %d characters in %s, truncating",
            MAX_SKILL_DESCRIPTION_LENGTH,
            skill_path,
        )
        description_str = description_str[:MAX_SKILL_DESCRIPTION_LENGTH]

    # 可选的工具列表，支持空格或逗号分隔
    raw_tools = frontmatter_data.get("allowed-tools")
    if isinstance(raw_tools, str):
        allowed_tools = [
            t.strip(",")  # 兼容 Claude Code 风格的逗号分隔
            for t in raw_tools.split()
            if t.strip(",")
        ]
    else:
        if raw_tools is not None:
            logger.warning(
                "Ignoring non-string 'allowed-tools' in %s (got %s)",
                skill_path,
                type(raw_tools).__name__,
            )
        allowed_tools = []

    # 能力或环境兼容性说明，最多 500 字符
    compatibility_str = str(frontmatter_data.get("compatibility", "")).strip() or None
    if compatibility_str and len(compatibility_str) > MAX_SKILL_COMPATIBILITY_LENGTH:
        logger.warning(
            "Compatibility exceeds %d characters in %s, truncating",
            MAX_SKILL_COMPATIBILITY_LENGTH,
            skill_path,
        )
        compatibility_str = str(compatibility_str)[:MAX_SKILL_COMPATIBILITY_LENGTH]

    # 版本号，默认为 0（表示未设置版本）
    raw_version = frontmatter_data.get("version")
    version = 0
    if raw_version is not None:
        try:
            version = int(raw_version)
        except (ValueError, TypeError):
            logger.warning(
                "Invalid 'version' in %s (got %r), defaulting to 0",
                skill_path,
                raw_version,
            )

    return SkillMetadata(
        id=skill_id,
        name=name,
        version=version,
        description=description_str,
        path=skill_path,
        metadata=_validate_metadata(frontmatter_data.get("metadata", {}), skill_path),
        license=str(frontmatter_data.get("license", "")).strip() or None,
        compatibility=compatibility_str,
        allowed_tools=allowed_tools,
    )


def _validate_metadata(
    raw: object,
    skill_path: str,
) -> dict[str, str]:
    """验证并规范化 YAML 前言中的元数据字段，确保为 dict[str, str] 类型。"""
    if not isinstance(raw, dict):
        if raw:
            logger.warning(
                "Ignoring non-dict metadata in %s (got %s)",
                skill_path,
                type(raw).__name__,
            )
        return {}
    return {str(k): str(v) for k, v in raw.items()}


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

        stat = await skill_md_path.stat()
        if stat.st_size > MAX_SKILL_FILE_SIZE:
            logger.warning(
                "Skipping %s: file too large (%d bytes)",
                skill_md_path,
                stat.st_size,
            )
            continue
        skill_content = (await skill_md_path.read_bytes()).decode(
            "utf-8", errors="replace"
        )

        # 解析元数据
        skill_metadata = _parse_skill_metadata(
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

    skill_dirs = [
        path
        for path in source_path.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    ]
    if not skill_dirs:
        return []

    skill_dirs.sort(key=lambda p: p.name.casefold())

    skills: list[SkillMetadata] = []
    for skill_path in skill_dirs:
        skill_md_path = skill_path / "SKILL.md"
        if skill_md_path.stat().st_size > MAX_SKILL_FILE_SIZE:
            logger.warning(
                "Skipping %s: file too large (%d bytes)",
                skill_md_path,
                skill_md_path.stat().st_size,
            )
            continue
        skill_content = skill_md_path.read_bytes().decode(
            "utf-8", errors="replace"
        )
        skill_metadata = _parse_skill_metadata(
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

When the user's request matches a skill description, call the `skill` tool with that skill name before taking task actions. Follow the loaded SKILL.md instructions, and load referenced supporting files only when needed. Do not create or rewrite skills unless the user explicitly asks for skill authoring.
</skills_system>
"""

SKILL_TOOL_NAME = "skill"
SKILL_TOOL_DESCRIPTION = """Loads the full instructions for a MoviePilot skill by name or id.

Available skills:
{skills_catalog}

Call this tool when the user's task matches one of the available skills. The tool returns the SKILL.md content and metadata so you can follow the skill's instructions. Do not use this for simple tasks that do not need a skill.
"""


def _extract_version(skill_md: Path) -> int:
    """从 SKILL.md 文件中快速提取 version 字段，无法提取时返回 0。"""
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception as err:
        logger.debug(f"读取技能版本失败: {err}")
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
                logger.info(
                    "已自动复制内置技能 '%s' -> '%s'", skill_src.name, skill_dst
                )
            except Exception as e:
                logger.warning("复制内置技能 '%s' 失败: %s", skill_src.name, e)
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
            logger.warning("更新内置技能 '%s' 失败: %s", skill_src.name, e)


class _SkillToolProvider:
    """Skill 工具的目录扫描和文件读取实现。"""

    def __init__(self, *, sources: list[str]) -> None:
        """初始化 Skill 工具数据源。"""
        self._sources = sources

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
        return any(
            cls._normalize_name(candidate) == normalized_query
            for candidate in candidates
        )

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
        """读取技能文件内容，并在超出上限时返回截断标记。"""
        path = AsyncPath(skill_path)
        stat = await path.stat()
        truncated = stat.st_size > MAX_SKILL_FILE_SIZE
        async with await path.open("rb") as handle:
            raw_content = await handle.read(MAX_SKILL_FILE_SIZE)
        return raw_content.decode("utf-8", errors="replace"), truncated

    @staticmethod
    def _serialize_skill_payload(payload: dict[str, Any]) -> str:
        """序列化 Skill 返回值，并严格限制最终进入模型的字符数。"""
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(serialized) <= MAX_SKILL_RESULT_CHARS:
            return serialized

        original_content = str(payload.get("content") or "")
        truncated_payload = dict(payload)
        truncated_payload["truncated"] = True
        low = 0
        high = len(original_content)
        best_result = json.dumps(
            {
                **truncated_payload,
                "content": SKILL_CONTENT_TRUNCATION_SUFFIX.strip(),
            },
            ensure_ascii=False,
            indent=2,
        )
        while low <= high:
            middle = (low + high) // 2
            candidate = json.dumps(
                {
                    **truncated_payload,
                    "content": (
                        original_content[:middle]
                        + SKILL_CONTENT_TRUNCATION_SUFFIX
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            if len(candidate) <= MAX_SKILL_RESULT_CHARS:
                best_result = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best_result

    async def load_skill(self, name: str) -> str:
        """加载指定 Skill 的完整说明并返回 JSON 字符串。"""
        logger.info(f"加载 Skill: name={name}")
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
            return self._serialize_skill_payload(
                {
                    "success": True,
                    "skill": {
                        "id": skill.get("id"),
                        "name": skill.get("name"),
                        "description": skill.get("description"),
                        "path": skill.get("path"),
                        "allowed_tools": skill.get("allowed_tools", []),
                    },
                    "content": content,
                    "truncated": truncated,
                }
            )
        except Exception as err:
            logger.error(f"加载 Skill 失败: {err}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": f"加载 Skill 时发生错误: {str(err)}",
                },
                ensure_ascii=False,
            )


def _format_skill_tool_catalog(skills: list[SkillMetadata]) -> str:
    """渲染 Skill 工具描述中的可用技能目录。"""
    if not skills:
        return "(No skills are currently available.)"
    return "\n".join(
        f"- {skill['id']}: {skill['name']} - {skill['description']}"
        for skill in skills
    )


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
            流式输出处理器，用于记录 skill 工具调用摘要。
        """
        self.sources = sources
        self.bundled_skills_dir = bundled_skills_dir
        self.stream_handler = stream_handler
        self.system_prompt_template = SKILLS_SYSTEM_PROMPT
        self._skill_provider = _SkillToolProvider(sources=sources)
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._skill_provider.load_skill,
                name=SKILL_TOOL_NAME,
                description=SKILL_TOOL_DESCRIPTION.format(
                    skills_catalog=_format_skill_tool_catalog(
                        self._load_skills_metadata()
                    )
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
            logger.warning("同步内置技能失败: %s", e)

    def _load_skills_metadata(self) -> list[SkillMetadata]:
        """同步加载当前配置目录中的 Skill 元数据。"""
        self._sync_bundled_skills()
        all_skills: dict[str, SkillMetadata] = {}
        for source_path in self.sources:
            for skill in _list_skills(Path(source_path)):
                all_skills[skill["name"]] = skill
        return list(all_skills.values())

    def _refresh_skill_tool_description(
        self, skills: list[SkillMetadata]
    ) -> None:
        """刷新 skill 工具描述中的可用技能目录。"""
        if not self.tools:
            return
        self.tools[0].description = SKILL_TOOL_DESCRIPTION.format(
            skills_catalog=_format_skill_tool_catalog(skills)
        )

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

        return "\n".join(lines)

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """将技能文档注入模型请求的系统消息中。"""
        skills_metadata = request.state.get("skills_metadata", [])  # noqa
        skills_list = self._format_skills_list(skills_metadata)

        skills_section = self.system_prompt_template.format(
            skills_list=skills_list,
        )

        new_system_message = append_to_system_message(
            request.system_message, skills_section
        )

        return request.override(system_message=new_system_message)

    async def abefore_agent(  # noqa
        self, state: SkillsState, runtime: Runtime, config: RunnableConfig
    ) -> SkillsStateUpdate | None:  # ty: ignore[invalid-method-override]
        """在 Agent 执行前异步加载技能元数据。

        首次加载时，会先将内置技能同步到用户目录（如不存在）。
        """
        self._sync_bundled_skills()

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
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """在模型调用时注入技能文档。"""
        modified_request = self.modify_request(request)
        return await handler(modified_request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """在 skill 工具执行时记录聚合摘要。"""
        tool = request.tool
        tool_name = getattr(tool, "name", None)
        if tool_name != SKILL_TOOL_NAME:
            return await handler(request)

        tool_call = request.tool_call or {}
        tool_args = tool_call.get("args") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}
        logger.info(
            f"开始执行 Skill 工具: name={tool_args.get('name') or '-'}"
        )
        if self.stream_handler and getattr(self.stream_handler, "is_streaming", False):
            self.stream_handler.record_tool_call(
                tool_name=SKILL_TOOL_NAME,
                tool_message="Skill loaded",
                tool_kwargs=tool_args,
            )
        try:
            result = await handler(request)
        except Exception as err:
            logger.error(f"Skill 工具执行失败: error={err}")
            raise
        logger.info("Skill 工具执行完成")
        return result


__all__ = ["SKILL_TOOL_NAME", "SkillMetadata", "SkillsMiddleware"]
