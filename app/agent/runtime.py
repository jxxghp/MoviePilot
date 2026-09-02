"""Agent 根层运行时配置管理。"""

from __future__ import annotations

import importlib
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from app.application.configuration import get_runtime_settings
from app.runtime.log import logger

CURRENT_PERSONA_FILE = "CURRENT_PERSONA.md"
SYSTEM_RUNTIME_DIR = "runtime"
MEMORY_DIR = "memory"
SKILLS_DIR = "skills"
JOBS_DIR = "jobs"
ACTIVITY_DIR = "activity"
PERSONAS_DIR = "personas"
PERSONA_FILE = "PERSONA.md"
SUBAGENTS_DIR = "subagents"
SUBAGENT_FILE = "SUBAGENT.md"
CURRENT_PERSONA_SCHEMA_VERSION = 3
PERSONA_SCHEMA_VERSION = 1
SUBAGENT_SCHEMA_VERSION = 1
DEFAULT_PERSONA_ID = "default"
PERSONA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _default_agent_root_dir() -> Path:
    """从组合根设置服务取得 Agent 目录，导入早期保留旧设置回退。"""
    try:
        config_path = get_runtime_settings().get("CONFIG_PATH")
    except RuntimeError:
        legacy_settings = importlib.import_module("app.runtime.config").settings
        config_path = legacy_settings.CONFIG_PATH
    return Path(config_path) / "agent"


ROOT_LEVEL_RUNTIME_FILES = {
    CURRENT_PERSONA_FILE,
}

OBSOLETE_AGENT_ROOT_FILES = {
    "AGENT_CORE.md",
    "AGENT_PROFILE.md",
    "AGENT_WORKFLOW.md",
    "AGENT_HOOKS.md",
    "USER_PREFERENCES.md",
    "SYSTEM_TASKS.md",
    "WAKE_FORMAT.md",
}

OBSOLETE_RUNTIME_FILES = {
    Path("AGENT_CORE.md"),
    Path("AGENT_PROFILE.md"),
    Path("AGENT_WORKFLOW.md"),
    Path("AGENT_HOOKS.md"),
    Path("USER_PREFERENCES.md"),
    Path("SYSTEM_TASKS.md"),
    Path("WAKE_FORMAT.md"),
    Path("personas") / DEFAULT_PERSONA_ID / "AGENT_PROFILE.md",
    Path("personas") / DEFAULT_PERSONA_ID / "AGENT_WORKFLOW.md",
    Path("personas") / DEFAULT_PERSONA_ID / "AGENT_HOOKS.md",
    Path("system_tasks") / "SYSTEM_TASKS.md",
    Path("templates") / "WAKE_FORMAT.md",
}

FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class AgentRuntimeConfigError(ValueError):
    """根层配置加载异常。"""


@dataclass
class ParsedMarkdownDocument:
    """解析后的 Markdown 文档。"""

    metadata: dict[str, Any]
    body: str


@dataclass
class PersonaDefinition:
    """单个人格定义。"""

    persona_id: str
    path: Path
    label: str
    description: str
    text: str
    aliases: list[str] = field(default_factory=list)

    def matches(self, query: str) -> bool:
        """判断 query 是否命中当前人格。"""
        normalized = query.strip().casefold()
        if not normalized:
            return False
        candidates = [self.persona_id, self.label, *self.aliases]
        return any(candidate.strip().casefold() == normalized for candidate in candidates)

    def summary_line(self) -> str:
        """渲染可读的一行人格摘要。"""
        parts = [f"`{self.persona_id}`"]
        if self.label and self.label != self.persona_id:
            parts.append(self.label)
        if self.description:
            parts.append(self.description)
        return " - ".join(parts)

    def to_dict(self, *, is_active: bool) -> dict[str, Any]:
        """输出给查询工具的结构化信息。"""
        return {
            "persona_id": self.persona_id,
            "label": self.label,
            "description": self.description,
            "aliases": self.aliases,
            "is_active": is_active,
            "path": str(self.path),
        }


@dataclass
class SubAgentDefinition:
    """单个子代理定义。"""

    subagent_id: str
    path: Path
    description: str
    text: str
    include_tags: list[str]
    exclude_tags: list[str]
    version: int = SUBAGENT_SCHEMA_VERSION
    label: str = ""

    def summary_line(self) -> str:
        """渲染可读的一行子代理摘要。"""
        parts = [f"`{self.subagent_id}`"]
        if self.label and self.label != self.subagent_id:
            parts.append(self.label)
        if self.description:
            parts.append(self.description)
        return " - ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """输出给查询或调试入口的结构化信息。"""
        return {
            "subagent_id": self.subagent_id,
            "label": self.label,
            "description": self.description,
            "include_tags": self.include_tags,
            "exclude_tags": self.exclude_tags,
            "version": self.version,
            "path": str(self.path),
        }


@dataclass
class AgentRuntimeConfig:
    """一次加载后的根层配置快照。"""

    source_root: Path
    active_persona: str
    current_persona_path: Path
    persona: PersonaDefinition
    available_personas: list[PersonaDefinition]
    available_subagents: list[SubAgentDefinition]
    extra_context_paths: list[Path]
    extra_contexts: list[tuple[Path, str]]
    warnings: list[str] = field(default_factory=list)
    used_fallback: bool = False

    def render_prompt_sections(self) -> str:
        """渲染进入系统提示词的运行时片段。"""
        sections: list[str] = [
            "<agent_runtime_config>",
            f"- Active persona: `{self.active_persona}`",
            f"- Active persona file: `personas/{self.persona.persona_id}/{PERSONA_FILE}`",
            "- Use `persona` with `action=list` before switching when the requested speaking style is unclear.",
            "- Subagent availability is exposed by the subagent task tools; do not rely on this runtime section as a catalog.",
            "</agent_runtime_config>",
        ]

        if self.warnings:
            sections.extend(
                [
                    "",
                    "<agent_runtime_warnings>",
                    *[f"- {warning}" for warning in self.warnings],
                    "</agent_runtime_warnings>",
                ]
            )

        sections.extend(
            [
                "",
                "<agent_persona>",
                f"- Persona ID: `{self.persona.persona_id}`",
            ]
        )
        if self.persona.label and self.persona.label != self.persona.persona_id:
            sections.append(f"- Persona Label: {self.persona.label}")
        if self.persona.description:
            sections.append(f"- Persona Description: {self.persona.description}")
        sections.extend(
            [
                "",
                self.persona.text.strip() or "(No persona instructions configured.)",
                "</agent_persona>",
            ]
        )
        for path, text in self.extra_contexts:
            if not text.strip():
                continue
            sections.extend(
                [
                    "",
                    f'<agent_extra_context source="{path.name}">',
                    text.strip(),
                    "</agent_extra_context>",
                ]
            )
        return "\n".join(sections).strip()

    def list_personas(self) -> list[dict[str, Any]]:
        """返回全部人格摘要。"""
        return [
            persona.to_dict(is_active=persona.persona_id == self.active_persona) for persona in self.available_personas
        ]


class AgentRuntimeManager:
    """统一管理 agent 根层运行时配置目录、校验与人格切换。"""

    def __init__(
        self,
        *,
        agent_root_dir: Optional[Path] = None,
        bundled_defaults_dir: Optional[Path] = None,
    ) -> None:
        self.agent_root_dir = agent_root_dir or _default_agent_root_dir()
        self.runtime_dir = self.agent_root_dir / SYSTEM_RUNTIME_DIR
        self.memory_dir = self.agent_root_dir / MEMORY_DIR
        self.skills_dir = self.agent_root_dir / SKILLS_DIR
        self.jobs_dir = self.agent_root_dir / JOBS_DIR
        self.activity_dir = self.agent_root_dir / ACTIVITY_DIR
        self.subagents_dir = self.runtime_dir / SUBAGENTS_DIR
        self.bundled_defaults_dir = bundled_defaults_dir or (Path(__file__).parent / "defaults")
        self._cache_lock = threading.Lock()
        self._cached_signature: Optional[tuple[tuple[str, int, int], ...]] = None
        self._cached_config: Optional[AgentRuntimeConfig] = None
        self._cached_signature_checked_at = 0.0
        self._signature_check_interval = 1.0
        self._layout_ready = False

    def ensure_layout(self) -> None:
        """创建目录、同步默认文件，并清理废弃的旧版 runtime 文件。"""
        with self._cache_lock:
            if self._layout_ready:
                return
        self.agent_root_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.activity_dir.mkdir(parents=True, exist_ok=True)
        self.subagents_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_root_runtime_files()
        self._remove_obsolete_runtime_files()
        self._sync_bundled_defaults()
        self._migrate_root_memory_files()
        with self._cache_lock:
            self._layout_ready = True

    def load_runtime_config(self) -> AgentRuntimeConfig:
        """加载配置。用户目录损坏时自动回退到内置默认配置。"""
        self.ensure_layout()
        signature = self.current_signature()
        with self._cache_lock:
            if self._cached_signature == signature and self._cached_config:
                return self._cached_config

            try:
                config = self._load_from_root(self.runtime_dir)
            except AgentRuntimeConfigError as err:
                logger.warning(f"Agent 根层配置无效，回退到内置默认配置: {err}")
                config = self._load_from_root(self.bundled_defaults_dir)
                config.used_fallback = True
                config.warnings.insert(0, f"用户运行时配置加载失败，已回退到内置默认配置: {err}")

            self._cached_signature = signature
            self._cached_config = config
            return config

    def invalidate_cache(self) -> None:
        """供测试或手动刷新时清理缓存。"""
        with self._cache_lock:
            self._cached_signature = None
            self._cached_config = None
            self._cached_signature_checked_at = 0.0
            self._layout_ready = False

    def current_signature(self) -> tuple[tuple[str, int, int], ...]:
        """返回当前运行时配置文件签名，供调用方判断缓存是否仍可复用。"""
        now = time.monotonic()
        with self._cache_lock:
            if (
                self._cached_signature is not None
                and now - self._cached_signature_checked_at < self._signature_check_interval
            ):
                return self._cached_signature

        signature = self._build_signature()
        with self._cache_lock:
            self._cached_signature = signature
            self._cached_signature_checked_at = now
        return signature

    def set_active_persona(self, persona_query: str) -> AgentRuntimeConfig:
        """切换当前激活人格，并立即刷新缓存。"""
        self.ensure_layout()
        runtime_root = self.runtime_dir
        current_path = runtime_root / CURRENT_PERSONA_FILE
        current_doc = self._read_markdown(current_path)
        current_meta = current_doc.metadata

        available_personas = self._load_personas(runtime_root)
        persona = self._resolve_persona_definition(persona_query, available_personas)

        document = self._render_current_persona_document(
            active_persona=persona.persona_id,
            extra_context_files=self._coerce_string_list(current_meta.get("extra_context_files")),
            deprecated_phrases=self._coerce_string_list(current_meta.get("deprecated_phrases")),
        )
        current_path.write_text(document, encoding="utf-8")
        self.invalidate_cache()
        logger.info(f"已切换 Agent 人格: {persona.persona_id}")
        return self.load_runtime_config()

    def list_personas(self) -> list[PersonaDefinition]:
        """列出当前可用人格。"""
        return self.load_runtime_config().available_personas

    def list_subagents(self) -> list[SubAgentDefinition]:
        """列出当前可用子代理。"""
        return self.load_runtime_config().available_subagents

    def update_persona_definition(
        self,
        persona_query: str,
        *,
        label: Optional[str] = None,
        description: Optional[str] = None,
        aliases: Optional[list[str]] = None,
        instructions: Optional[str] = None,
        append_instructions: Optional[list[str]] = None,
        create_if_missing: bool = False,
    ) -> tuple[PersonaDefinition, bool]:
        """更新或创建运行时人格定义。"""
        self.ensure_layout()
        runtime_root = self.runtime_dir
        available_personas = self._load_personas(runtime_root)

        created = False
        try:
            persona = self._resolve_persona_definition(persona_query, available_personas)
            target_persona_id = persona.persona_id
            target_path = persona.path
            existing_body = persona.text
            existing_label = persona.label
            existing_description = persona.description
            existing_aliases = list(persona.aliases)
        except AgentRuntimeConfigError:
            if not create_if_missing:
                raise
            target_persona_id = self._validate_new_persona_id(persona_query)
            target_path = runtime_root / PERSONAS_DIR / target_persona_id / PERSONA_FILE
            existing_body = ""
            existing_label = target_persona_id
            existing_description = ""
            existing_aliases = []
            created = True

        final_label = label.strip() if isinstance(label, str) and label.strip() else existing_label or target_persona_id
        final_description = (
            description.strip() if isinstance(description, str) and description.strip() else existing_description
        )
        final_aliases = self._normalize_persona_aliases(aliases, "aliases") if aliases is not None else existing_aliases
        final_body = (
            self._normalize_persona_body(instructions)
            if isinstance(instructions, str) and instructions.strip()
            else self._normalize_persona_body(existing_body)
        )
        final_body = self._merge_persona_instructions(
            final_body,
            append_instructions,
        )
        if not final_body.strip():
            raise AgentRuntimeConfigError("人格定义正文不能为空")

        document = self._render_persona_document(
            persona_id=target_persona_id,
            label=final_label,
            description=final_description,
            aliases=final_aliases,
            body=final_body,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(document, encoding="utf-8")
        self.invalidate_cache()

        runtime_config = self.load_runtime_config()
        updated_persona = self._resolve_persona_definition(
            target_persona_id,
            runtime_config.available_personas,
        )
        logger.info(
            "已%s Agent 人格定义: %s",
            "创建" if created else "更新",
            updated_persona.persona_id,
        )
        return updated_persona, created

    def _build_signature(self) -> tuple[tuple[str, int, int], ...]:
        """基于运行时配置和内置人格生成文件签名。"""
        entries: list[tuple[str, int, int]] = []
        for prefix, root in (
            ("runtime", self.runtime_dir),
            ("bundled", self.bundled_defaults_dir),
        ):
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                entries.append((f"{prefix}:{relative}", stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def _sync_bundled_defaults(self) -> None:
        """同步默认运行时文件，并按版本更新内置子代理定义。"""
        if not self.bundled_defaults_dir.exists():
            return
        for path in sorted(self.bundled_defaults_dir.rglob("*")):
            relative = path.relative_to(self.bundled_defaults_dir)
            target = self.runtime_dir / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists():
                if self._should_update_bundled_subagent(relative, path, target):
                    shutil.copy2(path, target)
                    logger.info(f"已更新默认 Agent 子代理定义: {target}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            logger.info(f"已同步默认 Agent 运行时文件: {target}")

    @classmethod
    def _should_update_bundled_subagent(
        cls,
        relative_path: Path,
        source_path: Path,
        target_path: Path,
    ) -> bool:
        """判断是否需要用更高版本的内置子代理定义覆盖用户目录副本。"""
        parts = relative_path.parts
        if len(parts) < 3 or parts[0] != SUBAGENTS_DIR or relative_path.name != SUBAGENT_FILE:
            return False

        source_version = cls._read_markdown_version(source_path)
        target_version = cls._read_markdown_version(target_path)
        return source_version > target_version

    @staticmethod
    def _read_markdown_version(path: Path) -> int:
        """读取 Markdown frontmatter 中的整数版本，失败时按 0 处理。"""
        try:
            document = AgentRuntimeManager._read_markdown(path)
        except AgentRuntimeConfigError as err:
            logger.warning(f"读取 Agent 运行时文件版本失败 {path}: {err}")
            return 0
        return AgentRuntimeManager._coerce_int_metadata(
            document.metadata.get("version"),
            default=0,
        )

    def _migrate_root_runtime_files(self) -> None:
        """兼容早期直接放在 `config/agent` 根目录的 CURRENT_PERSONA。"""
        source = self.agent_root_dir / CURRENT_PERSONA_FILE
        target = self.runtime_dir / CURRENT_PERSONA_FILE
        if not source.exists() or target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        logger.info(f"已迁移旧版 Agent 根配置文件: {source} -> {target}")

    def _remove_obsolete_runtime_files(self) -> None:
        """删除不再支持的旧版 Agent 配置文件，避免被误迁移到 memory。"""
        for filename in sorted(OBSOLETE_AGENT_ROOT_FILES):
            path = self.agent_root_dir / filename
            if not path.exists() or not path.is_file():
                continue
            path.unlink()
            logger.info(f"已删除废弃的 Agent 根配置文件: {path}")

        for relative_path in sorted(OBSOLETE_RUNTIME_FILES):
            path = self.runtime_dir / relative_path
            if not path.exists() or not path.is_file():
                continue
            path.unlink()
            logger.info(f"已删除废弃的 Agent 运行时文件: {path}")

    def _migrate_root_memory_files(self) -> None:
        """将旧版根目录 memory 文件移入 `config/agent/memory`。"""
        for path in sorted(self.agent_root_dir.glob("*.md")):
            if path.name in ROOT_LEVEL_RUNTIME_FILES:
                continue
            target = self.memory_dir / path.name
            if target.exists():
                continue
            path.rename(target)
            logger.info(f"已迁移旧版 Agent memory 文件: {path} -> {target}")

    def _load_from_root(self, root: Path) -> AgentRuntimeConfig:
        current_persona_path = root / CURRENT_PERSONA_FILE
        current_doc = self._read_markdown(current_persona_path)
        current_meta = current_doc.metadata

        active_persona = str(current_meta.get("active_persona") or DEFAULT_PERSONA_ID).strip()
        if not active_persona:
            raise AgentRuntimeConfigError("CURRENT_PERSONA.md 缺少 active_persona")

        extra_context_paths = self._resolve_optional_paths(root, current_meta.get("extra_context_files", []))

        available_personas = self._load_personas(root)
        persona = self._resolve_persona_definition(active_persona, available_personas)
        available_subagents = self._load_subagents(root)
        extra_contexts = [(path, self._read_markdown(path).body) for path in extra_context_paths]

        warnings = self._validate_runtime_config(
            current_meta=current_meta,
            persona_path=persona.path,
            extra_context_paths=extra_context_paths,
            persona_text=persona.text,
        )
        return AgentRuntimeConfig(
            source_root=root,
            active_persona=active_persona,
            current_persona_path=current_persona_path,
            persona=persona,
            available_personas=available_personas,
            available_subagents=available_subagents,
            extra_context_paths=extra_context_paths,
            extra_contexts=extra_contexts,
            warnings=warnings,
        )

    def _load_personas(self, root: Path) -> list[PersonaDefinition]:
        """扫描并解析所有可用人格。"""
        personas_root = root / PERSONAS_DIR
        if not personas_root.exists():
            raise AgentRuntimeConfigError(f"缺少 personas 目录: {personas_root}")

        personas: list[PersonaDefinition] = []
        seen_ids: set[str] = set()
        for persona_dir in sorted(personas_root.iterdir()):
            if not persona_dir.is_dir():
                continue
            persona_path = persona_dir / PERSONA_FILE
            if not persona_path.exists():
                continue
            document = self._read_markdown(persona_path)
            persona_id = str(document.metadata.get("persona_id") or persona_dir.name).strip()
            if not persona_id:
                raise AgentRuntimeConfigError(f"{persona_path} 缺少 persona_id")
            if persona_id in seen_ids:
                raise AgentRuntimeConfigError(f"检测到重复的人格 ID: {persona_id}")
            seen_ids.add(persona_id)
            aliases = self._normalize_string_list(
                document.metadata.get("aliases"),
                f"{persona_path}.aliases",
            )
            personas.append(
                PersonaDefinition(
                    persona_id=persona_id,
                    path=persona_path,
                    label=str(document.metadata.get("label") or persona_id).strip(),
                    description=str(document.metadata.get("description") or "").strip(),
                    text=document.body,
                    aliases=aliases,
                )
            )

        if not personas:
            raise AgentRuntimeConfigError(f"{personas_root} 中未找到任何人格定义")
        return personas

    def _load_subagents(self, root: Path) -> list[SubAgentDefinition]:
        """扫描并解析所有可用子代理。"""
        subagents_root = root / SUBAGENTS_DIR
        if not subagents_root.exists():
            raise AgentRuntimeConfigError(f"缺少 subagents 目录: {subagents_root}")

        subagents: list[SubAgentDefinition] = []
        seen_ids: set[str] = set()
        for subagent_dir in sorted(subagents_root.iterdir()):
            if not subagent_dir.is_dir():
                continue
            subagent_path = subagent_dir / SUBAGENT_FILE
            if not subagent_path.exists():
                continue
            document = self._read_markdown(subagent_path)
            subagent_id = str(document.metadata.get("subagent_id") or subagent_dir.name).strip()
            if not subagent_id:
                raise AgentRuntimeConfigError(f"{subagent_path} 缺少 subagent_id")
            if not PERSONA_ID_PATTERN.fullmatch(subagent_id):
                raise AgentRuntimeConfigError(
                    f"{subagent_path} 的 subagent_id 只能使用小写字母、数字、下划线和中划线，且必须以字母或数字开头"
                )
            if subagent_id in seen_ids:
                raise AgentRuntimeConfigError(f"检测到重复的子代理 ID: {subagent_id}")
            seen_ids.add(subagent_id)

            description = str(document.metadata.get("description") or "").strip()
            if not description:
                raise AgentRuntimeConfigError(f"{subagent_path} 缺少 description")
            include_tags = self._normalize_string_list(
                document.metadata.get("include_tags"),
                f"{subagent_path}.include_tags",
            )
            if not include_tags:
                raise AgentRuntimeConfigError(f"{subagent_path} 缺少 include_tags")
            exclude_tags = self._normalize_string_list(
                document.metadata.get("exclude_tags"),
                f"{subagent_path}.exclude_tags",
            )
            text = self._normalize_subagent_body(document.body)
            if not text:
                raise AgentRuntimeConfigError(f"{subagent_path} 子代理正文不能为空")

            subagents.append(
                SubAgentDefinition(
                    subagent_id=subagent_id,
                    path=subagent_path,
                    label=str(document.metadata.get("label") or subagent_id).strip(),
                    description=description,
                    text=text,
                    include_tags=include_tags,
                    exclude_tags=exclude_tags,
                    version=self._coerce_int_metadata(
                        document.metadata.get("version"),
                        default=SUBAGENT_SCHEMA_VERSION,
                    ),
                )
            )

        if not subagents:
            raise AgentRuntimeConfigError(f"{subagents_root} 中未找到任何子代理定义")
        return subagents

    @staticmethod
    def _resolve_persona_definition(
        persona_query: str,
        personas: list[PersonaDefinition],
    ) -> PersonaDefinition:
        """按 persona_id、label 或 aliases 解析人格。"""
        normalized = (persona_query or "").strip()
        if not normalized:
            raise AgentRuntimeConfigError("人格 ID 不能为空")

        for persona in personas:
            if persona.persona_id == normalized:
                return persona
        for persona in personas:
            if persona.matches(normalized):
                return persona

        available = ", ".join(persona.persona_id for persona in personas)
        raise AgentRuntimeConfigError(f"未找到人格 `{persona_query}`，可用人格: {available}")

    @staticmethod
    def _validate_new_persona_id(persona_id: str) -> str:
        """校验新建人格的 ID，避免写入非法路径。"""
        normalized = (persona_id or "").strip()
        if not normalized:
            raise AgentRuntimeConfigError("新建人格时 persona_id 不能为空")
        if not PERSONA_ID_PATTERN.fullmatch(normalized):
            raise AgentRuntimeConfigError(
                "新建人格时 persona_id 只能使用小写字母、数字、下划线和中划线，且必须以字母或数字开头"
            )
        return normalized

    @staticmethod
    def _read_markdown(path: Path) -> ParsedMarkdownDocument:
        if not path.exists():
            raise AgentRuntimeConfigError(f"缺少配置文件: {path}")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as err:  # noqa: BLE001
            raise AgentRuntimeConfigError(f"读取配置文件失败 {path}: {err}") from err

        metadata: dict[str, Any] = {}
        body = content
        match = FRONTMATTER_PATTERN.match(content)
        if match:
            try:
                metadata = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError as err:
                raise AgentRuntimeConfigError(f"YAML frontmatter 解析失败 {path}: {err}") from err
            if not isinstance(metadata, dict):
                raise AgentRuntimeConfigError(f"frontmatter 必须是映射类型: {path}")
            body = content[match.end() :]
        return ParsedMarkdownDocument(metadata=metadata, body=body.strip())

    @staticmethod
    def _resolve_optional_paths(root: Path, values: Any) -> list[Path]:
        if not values:
            return []
        if not isinstance(values, list):
            raise AgentRuntimeConfigError("extra_context_files 必须是数组")
        return [AgentRuntimeManager._resolve_relative_path(root, str(value)) for value in values]

    @staticmethod
    def _resolve_relative_path(root: Path, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (root / candidate).resolve()

    @staticmethod
    def _normalize_string_list(values: Any, field_name: str) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise AgentRuntimeConfigError(f"{field_name} 必须是字符串数组")
        normalized: list[str] = []
        for value in values:
            text = str(value).strip()
            if text:
                normalized.append(text)
        return normalized

    @staticmethod
    def _coerce_string_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    @staticmethod
    def _normalize_persona_aliases(values: Any, field_name: str) -> list[str]:
        """规范化人格别名，保持顺序并去重。"""
        normalized = AgentRuntimeManager._normalize_string_list(values, field_name)
        deduped: list[str] = []
        seen: set[str] = set()
        for alias in normalized:
            folded = alias.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            deduped.append(alias)
        return deduped

    @staticmethod
    def _merge_persona_instructions(
        base_body: str,
        append_instructions: Optional[list[str]],
    ) -> str:
        """把增量规则安全追加到人格正文末尾。"""
        merged = (base_body or "").strip()
        if not append_instructions:
            return merged

        extras: list[str] = []
        for item in append_instructions:
            text = str(item).strip()
            if not text:
                continue
            if not re.match(r"^([-*]|\d+\.)\s", text):
                text = f"- {text}"
            extras.append(text)

        if not extras:
            return merged
        if not merged:
            return "\n".join(extras)
        return merged.rstrip() + "\n\n" + "\n".join(extras)

    @staticmethod
    def _normalize_persona_body(body: Optional[str]) -> str:
        """去掉重复的 PERSONA 标题，保持正文可安全回写。"""
        normalized = (body or "").strip()
        if not normalized:
            return ""
        if normalized.startswith("# PERSONA"):
            _, _, remainder = normalized.partition("\n")
            return remainder.strip()
        return normalized

    @staticmethod
    def _normalize_subagent_body(body: Optional[str]) -> str:
        """去掉重复的 SUBAGENT 标题，保持正文可安全加载。"""
        normalized = (body or "").strip()
        if not normalized:
            return ""
        if normalized.startswith("# SUBAGENT"):
            _, _, remainder = normalized.partition("\n")
            return remainder.strip()
        return normalized

    @staticmethod
    def _coerce_int_metadata(value: Any, *, default: int = 0) -> int:
        """将 frontmatter 中的整数型元数据规范化。"""
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _validate_runtime_config(
        self,
        *,
        current_meta: dict[str, Any],
        persona_path: Path,
        extra_context_paths: list[Path],
        persona_text: str,
    ) -> list[str]:
        warnings: list[str] = []
        required_paths = [persona_path]
        duplicates = self._find_duplicate_paths(required_paths + extra_context_paths)
        if duplicates:
            warnings.append("检测到重复引用的根层配置文件: " + ", ".join(path.as_posix() for path in duplicates))

        deprecated_phrases = self._normalize_string_list(current_meta.get("deprecated_phrases"), "deprecated_phrases")
        if deprecated_phrases:
            for phrase in deprecated_phrases:
                if phrase and phrase in persona_text:
                    warnings.append(f"检测到已废弃短语 `{phrase}` 仍出现在 persona 中")
        return warnings

    @staticmethod
    def _find_duplicate_paths(paths: Iterable[Path]) -> list[Path]:
        seen: set[Path] = set()
        duplicates: list[Path] = []
        for path in paths:
            resolved = path.resolve()
            if resolved in seen and resolved not in duplicates:
                duplicates.append(resolved)
            seen.add(resolved)
        return duplicates

    @staticmethod
    def _render_current_persona_document(
        *,
        active_persona: str,
        extra_context_files: list[str],
        deprecated_phrases: list[str],
    ) -> str:
        """统一生成 CURRENT_PERSONA.md，避免手写时结构漂移。"""
        metadata = {
            "version": CURRENT_PERSONA_SCHEMA_VERSION,
            "active_persona": active_persona,
            "extra_context_files": extra_context_files,
            "deprecated_phrases": deprecated_phrases,
        }
        body_lines = [
            "# CURRENT_PERSONA",
            "",
            f"当前激活人格：`{active_persona}`",
            "",
            "运行时加载顺序固定如下：",
            "",
            "1. 核心系统提示词（程序内置，不可运行时覆盖）",
            "2. `personas/<active_persona>/PERSONA.md`",
            "3. `extra_context_files`",
            "4. `memory/*.md`",
            "5. `activity/*.md`",
            "",
            "`memory` 中的长期偏好可以细化回复方式，但不应覆盖系统核心身份、目标和安全边界。",
        ]
        frontmatter = yaml.safe_dump(
            metadata,
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        return f"---\n{frontmatter}\n---\n" + "\n".join(body_lines) + "\n"

    @staticmethod
    def _render_persona_document(
        *,
        persona_id: str,
        label: str,
        description: str,
        aliases: list[str],
        body: str,
    ) -> str:
        """统一生成人格定义文件，避免手写 frontmatter 漂移。"""
        metadata = {
            "version": PERSONA_SCHEMA_VERSION,
            "persona_id": persona_id,
            "label": label,
            "description": description,
            "aliases": aliases,
        }
        frontmatter = yaml.safe_dump(
            metadata,
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        normalized_body = AgentRuntimeManager._normalize_persona_body(body)
        return f"---\n{frontmatter}\n---\n# PERSONA\n\n{normalized_body}\n"


agent_runtime_manager = AgentRuntimeManager()
