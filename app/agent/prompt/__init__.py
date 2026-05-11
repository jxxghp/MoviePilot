"""提示词管理器"""

import socket
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from time import strftime
from typing import Any, Dict, Optional

import yaml

from app.core.config import settings
from app.log import logger
from app.schemas import (
    ChannelCapability,
    ChannelCapabilities,
    MessageChannel,
    ChannelCapabilityManager,
)
from app.utils.system import SystemUtils

SYSTEM_TASKS_FILE = "System Tasks.yaml"
SYSTEM_TASKS_SCHEMA_VERSION = 2


class PromptConfigError(ValueError):
    """程序内置提示词定义加载异常。"""


@dataclass
class SystemTaskTypeDefinition:
    """单个后台系统任务定义。"""

    header: str
    objective: str
    context_title: Optional[str] = None
    context_lines: list[str] = field(default_factory=list)
    steps_title: Optional[str] = None
    steps: list[str] = field(default_factory=list)
    task_rules: list[str] = field(default_factory=list)
    empty_result: Optional[str] = None


@dataclass
class SystemTasksDefinition:
    """程序内置后台系统任务定义。"""

    path: Path
    version: int
    shared_rules: list[str]
    task_types: dict[str, SystemTaskTypeDefinition]


class PromptManager:
    """
    提示词管理器
    """

    def __init__(self, prompts_dir: str = None):
        if prompts_dir is None:
            self.prompts_dir = Path(__file__).parent
        else:
            self.prompts_dir = Path(prompts_dir)
        self.prompts_cache: Dict[str, str] = {}
        self._system_tasks_cache: Optional[SystemTasksDefinition] = None
        self._system_tasks_signature: Optional[tuple[int, int]] = None

    def load_prompt(self, prompt_name: str) -> str:
        """
        加载指定的提示词
        """
        if prompt_name in self.prompts_cache:
            return self.prompts_cache[prompt_name]

        prompt_file = self.prompts_dir / prompt_name
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            # 缓存提示词
            self.prompts_cache[prompt_name] = content
            logger.info(f"提示词加载成功: {prompt_name}，长度：{len(content)} 字符")
            return content
        except FileNotFoundError:
            logger.error(f"提示词文件不存在: {prompt_file}")
            raise
        except Exception as e:
            logger.error(f"加载提示词失败: {prompt_name}, 错误: {e}")
            raise

    def get_agent_prompt(self, channel: str = None) -> str:
        """
        获取智能体提示词
        :param channel: 消息渠道（Telegram、微信、Slack等）
        :return: 提示词内容
        """
        # 基础提示词只保留 MoviePilot 运行时和渠道能力相关约束。
        # 根层运行时配置由 RuntimeConfigMiddleware 在每次模型调用前动态注入，
        # 这样人格切换可以在同一轮 Agent 执行里立即生效。
        base_prompt = self.load_prompt("System Core Prompt.txt")

        # 识别渠道
        markdown_spec = ""
        msg_channel = (
            next(
                (c for c in MessageChannel if c.value.lower() == channel.lower()), None
            )
            if channel
            else None
        )
        # 获取渠道能力说明
        if msg_channel:
            caps = ChannelCapabilityManager.get_capabilities(msg_channel)
            if caps:
                markdown_spec = self._generate_formatting_instructions(caps)
        button_choice_spec = self._generate_button_choice_instructions(msg_channel)

        # 啰嗦模式
        verbose_spec = ""
        if not settings.AI_AGENT_VERBOSE:
            verbose_spec = (
                "\n\n[Important Instruction] STRICTLY ENFORCED: "
                "If tools are needed, DO NOT output any conversational text, explanations, progress updates, "
                "or acknowledgements before the first tool call or between tool calls. "
                "Call tools directly without any transitional phrases. "
                "You MUST remain completely silent until all required tools have finished and you have the final result. "
                "Only then may you send one final user-facing reply. "
                "DO NOT output any intermediate content whatsoever."
            )

        # MoviePilot系统信息
        moviepilot_info = self._get_moviepilot_info()
        voice_reply_spec = self._generate_voice_reply_instructions()

        # 始终替换占位符，避免后续 .format() 时因残留花括号报 KeyError
        base_prompt = base_prompt.format(
            markdown_spec=markdown_spec,
            verbose_spec=verbose_spec,
            moviepilot_info=moviepilot_info,
            voice_reply_spec=voice_reply_spec,
            button_choice_spec=button_choice_spec,
        )

        return base_prompt

    def load_system_tasks_definition(self) -> SystemTasksDefinition:
        """加载程序内置的后台系统任务定义。"""
        system_tasks_path = self.prompts_dir / SYSTEM_TASKS_FILE
        try:
            stat = system_tasks_path.stat()
        except FileNotFoundError as err:
            logger.error(f"系统任务定义文件不存在: {system_tasks_path}")
            raise PromptConfigError(f"系统任务定义文件不存在: {system_tasks_path}") from err

        signature = (stat.st_mtime_ns, stat.st_size)
        if (
            self._system_tasks_signature == signature
            and self._system_tasks_cache is not None
        ):
            return self._system_tasks_cache

        try:
            content = system_tasks_path.read_text(encoding="utf-8")
        except Exception as err:  # noqa: BLE001
            logger.error(f"读取系统任务定义失败: {system_tasks_path}, 错误: {err}")
            raise PromptConfigError(
                f"读取系统任务定义失败 {system_tasks_path}: {err}"
            ) from err

        try:
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as err:
            raise PromptConfigError(f"YAML 解析失败 {system_tasks_path}: {err}") from err
        if not isinstance(data, dict):
            raise PromptConfigError(
                f"YAML 根节点必须是映射类型: {system_tasks_path}"
            )

        definition = self._parse_system_tasks_definition(system_tasks_path, data)
        self._system_tasks_signature = signature
        self._system_tasks_cache = definition
        return definition

    def render_system_task_message(
        self,
        task_type: str,
        *,
        template_context: Optional[dict[str, Any]] = None,
        extra_rules: Optional[list[str]] = None,
    ) -> str:
        """根据程序内置 YAML 渲染后台系统任务提示词。"""
        system_tasks = self.load_system_tasks_definition()
        task_definition = system_tasks.task_types.get(task_type)
        if not task_definition:
            raise PromptConfigError(f"未定义的后台系统任务类型: {task_type}")

        rendered_context = self._render_template_lines(
            task_definition.context_lines,
            template_context,
            task_type,
            "context_lines",
        )
        rendered_steps = self._render_template_lines(
            task_definition.steps,
            template_context,
            task_type,
            "steps",
        )
        rendered_task_rules = self._render_template_lines(
            task_definition.task_rules,
            template_context,
            task_type,
            "task_rules",
        )

        sections = [
            self._render_template_text(
                task_definition.header,
                template_context,
                task_type,
                "header",
            ).strip(),
            self._render_template_text(
                task_definition.objective,
                template_context,
                task_type,
                "objective",
            ).strip(),
        ]
        if rendered_context:
            sections.append(
                self._format_titled_lines(
                    task_definition.context_title or "Task context",
                    rendered_context,
                )
            )
        if rendered_steps:
            sections.append(
                self._format_titled_lines(
                    task_definition.steps_title or "Follow these steps",
                    rendered_steps,
                )
            )

        rules = list(system_tasks.shared_rules)
        if task_definition.empty_result:
            rules.append(task_definition.empty_result)
        rules.extend(rendered_task_rules)
        if extra_rules:
            rules.extend(rule.strip() for rule in extra_rules if rule and rule.strip())
        if rules:
            sections.append(self._format_numbered_rules("IMPORTANT", rules))
        return "\n\n".join(section for section in sections if section).strip()

    @staticmethod
    def _get_moviepilot_info() -> str:
        """
        获取MoviePilot系统信息，用于注入到系统提示词中
        """
        # 获取主机名和IP地址
        try:
            hostname = socket.gethostname()
            ip_address = socket.gethostbyname(hostname)
        except Exception:  # noqa
            hostname = "localhost"
            ip_address = "127.0.0.1"

        # 配置文件和日志文件目录
        config_path = str(settings.CONFIG_PATH)
        log_path = str(settings.LOG_PATH)

        # API地址构建
        api_port = settings.PORT
        api_path = settings.API_V1_STR

        # API令牌
        api_token = settings.API_TOKEN or "未设置"

        # 数据库信息
        db_type = settings.DB_TYPE
        if db_type == "sqlite":
            db_info = f"SQLite ({settings.CONFIG_PATH / 'db' / 'moviepilot.db'})"
        else:
            db_password = settings.DB_POSTGRESQL_PASSWORD or ""
            db_info = (
                f"PostgreSQL ({settings.DB_POSTGRESQL_USERNAME}:{db_password}@"
                f"{settings.DB_POSTGRESQL_TARGET}/{settings.DB_POSTGRESQL_DATABASE})"
            )

        # 保留日期用于提供“今天是哪天”的稳定上下文，但不再注入秒级时间，
        # 避免每次请求都生成不同的 system prompt，影响 provider 侧 cache 命中率。
        info_lines = [
            f"- 当前日期: {strftime('%Y-%m-%d')}",
            f"- 运行环境: {SystemUtils.platform} {'docker' if SystemUtils.is_docker() else ''}",
            f"- 主机名: {hostname}",
            f"- IP地址: {ip_address}",
            f"- API端口: {api_port}",
            f"- API路径: {api_path}",
            f"- API令牌: {api_token}",
            f"- 外网域名: {settings.APP_DOMAIN or '未设置'}",
            f"- 数据库类型: {db_type}",
            f"- 数据库: {db_info}",
            f"- 配置文件目录: {config_path}",
            f"- 日志文件目录: {log_path}",
            f"- 系统安装目录: {settings.ROOT_PATH}",
        ]

        return "\n".join(info_lines)

    @staticmethod
    def _generate_formatting_instructions(caps: ChannelCapabilities) -> str:
        """
        根据渠道能力动态生成格式指令
        """
        instructions = []
        if ChannelCapability.RICH_TEXT not in caps.capabilities:
            instructions.append(
                "- Formatting: Use **Plain Text ONLY**. The channel does NOT support Markdown."
            )
            instructions.append(
                "- No Markdown Symbols: NEVER use `**`, `*`, `__`, or `[` blocks. Use natural text to emphasize (e.g., using ALL CAPS or separators)."
            )
            instructions.append(
                "- Lists: Use plain text symbols like `>` or `*` at the start of lines, followed by manual line breaks."
            )
            instructions.append("- Links: Paste URLs directly as text.")
        return "\n".join(instructions)

    @staticmethod
    def _generate_voice_reply_instructions() -> str:
        return (
            "- Voice replies: Use normal text replies by default. "
            "Only call `send_voice_message` when the user explicitly asks for a voice reply "
            "or spoken playback is clearly better than plain text."
        )

    @staticmethod
    def _generate_button_choice_instructions(
        channel: MessageChannel = None,
    ) -> str:
        if (
            channel
            and ChannelCapabilityManager.supports_buttons(channel)
            and ChannelCapabilityManager.supports_callbacks(channel)
        ):
            return (
                "- User questions: If you need the user to choose from a few clear options, "
                "call `ask_user_choice` to send button options. After the user clicks a button, "
                "the selected value will come back as the user's next message. After calling this tool, "
                "wait for the user's selection instead of repeating the question in plain text."
            )
        return "- User questions: When you truly need user input, ask briefly in plain text."

    def _parse_system_tasks_definition(
        self,
        path: Path,
        data: dict[str, Any],
    ) -> SystemTasksDefinition:
        """把 YAML 结构转换成系统任务定义对象。"""
        version = self._normalize_positive_int(data.get("version"), "version", default=1)
        if version < SYSTEM_TASKS_SCHEMA_VERSION:
            raise PromptConfigError(
                f"{path} 的 version={version} 过旧，"
                f"当前要求 System Tasks schema v{SYSTEM_TASKS_SCHEMA_VERSION} 或更高版本"
            )

        shared_rules = self._normalize_string_list(data.get("shared_rules"), "shared_rules")
        if not shared_rules:
            raise PromptConfigError(f"{path} 缺少 shared_rules")

        raw_task_types = data.get("task_types")
        if not isinstance(raw_task_types, dict) or not raw_task_types:
            raise PromptConfigError(f"{path} 缺少 task_types 映射")

        task_types: dict[str, SystemTaskTypeDefinition] = {}
        for key, raw in raw_task_types.items():
            if not isinstance(raw, dict):
                raise PromptConfigError(f"task_types.{key} 必须是映射")

            header = str(raw.get("header") or "").strip()
            objective = str(raw.get("objective") or "").strip()
            if not header or not objective:
                raise PromptConfigError(f"task_types.{key} 缺少 header 或 objective")

            task_types[str(key)] = SystemTaskTypeDefinition(
                header=header,
                objective=objective,
                context_title=str(raw.get("context_title") or "").strip() or None,
                context_lines=self._normalize_string_list(
                    raw.get("context_lines"),
                    f"task_types.{key}.context_lines",
                ),
                steps_title=str(raw.get("steps_title") or "").strip() or None,
                steps=self._normalize_string_list(
                    raw.get("steps"),
                    f"task_types.{key}.steps",
                ),
                task_rules=self._normalize_string_list(
                    raw.get("task_rules"),
                    f"task_types.{key}.task_rules",
                ),
                empty_result=str(raw.get("empty_result") or "").strip() or None,
            )
        return SystemTasksDefinition(
            path=path,
            version=version,
            shared_rules=shared_rules,
            task_types=task_types,
        )

    @classmethod
    def _render_template_text(
        cls,
        text: str,
        template_context: Optional[dict[str, Any]],
        task_type: str,
        field_name: str,
    ) -> str:
        if not text:
            return ""

        formatter = Formatter()
        required_fields = {
            placeholder_name
            for _, placeholder_name, _, _ in formatter.parse(text)
            if placeholder_name
        }
        if not required_fields:
            return text

        context = cls._normalize_template_context(template_context)
        missing_fields = sorted(f for f in required_fields if f not in context)
        if missing_fields:
            raise PromptConfigError(
                f"系统任务定义 `{task_type}` 的 `{field_name}` 缺少变量: "
                + ", ".join(f"`{f}`" for f in missing_fields)
            )

        # 这里统一做字符串替换，让 YAML 成为后台任务文案的唯一行为来源。
        return text.format_map(context)

    @classmethod
    def _render_template_lines(
        cls,
        items: list[str],
        template_context: Optional[dict[str, Any]],
        task_type: str,
        field_name: str,
    ) -> list[str]:
        return [
            cls._render_template_text(
                item,
                template_context,
                task_type,
                f"{field_name}[{index}]",
            ).rstrip()
            for index, item in enumerate(items, start=1)
            if item and item.rstrip()
        ]

    @staticmethod
    def _normalize_template_context(
        template_context: Optional[dict[str, Any]],
    ) -> dict[str, str]:
        if not template_context:
            return {}
        return {
            str(key): "" if value is None else str(value)
            for key, value in template_context.items()
        }

    @staticmethod
    def _format_numbered_rules(title: str, items: list[str]) -> str:
        return "\n".join(
            [f"{title}:"] + [f"{index}. {item}" for index, item in enumerate(items, start=1)]
        )

    @staticmethod
    def _format_titled_lines(title: str, items: list[str]) -> str:
        cleaned = [item.rstrip() for item in items if item and item.rstrip()]
        return "\n".join([f"{title}:"] + cleaned)

    @staticmethod
    def _normalize_positive_int(
        value: Any,
        field_name: str,
        *,
        default: int,
    ) -> int:
        if value in (None, ""):
            return default
        try:
            normalized = int(value)
        except (TypeError, ValueError) as err:
            raise PromptConfigError(f"{field_name} 必须是正整数") from err
        if normalized <= 0:
            raise PromptConfigError(f"{field_name} 必须是正整数")
        return normalized

    @staticmethod
    def _normalize_string_list(values: Any, field_name: str) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise PromptConfigError(f"{field_name} 必须是字符串数组")
        normalized: list[str] = []
        for value in values:
            text = str(value).strip()
            if text:
                normalized.append(text)
        return normalized

    def clear_cache(self):
        """
        清空缓存
        """
        self.prompts_cache.clear()
        self._system_tasks_cache = None
        self._system_tasks_signature = None
        logger.info("提示词缓存已清空")


prompt_manager = PromptManager()
