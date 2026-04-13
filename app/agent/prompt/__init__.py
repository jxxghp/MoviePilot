"""提示词管理器"""

import socket
from pathlib import Path
from time import strftime
from typing import Dict

from app.core.config import settings
from app.log import logger
from app.schemas import (
    ChannelCapability,
    ChannelCapabilities,
    MessageChannel,
    ChannelCapabilityManager,
)
from app.utils.system import SystemUtils


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

    def get_agent_prompt(
        self, channel: str = None, prefer_voice_reply: bool = False
    ) -> str:
        """
        获取智能体提示词
        :param channel: 消息渠道（Telegram、微信、Slack等）
        :param prefer_voice_reply: 是否优先使用语音回复
        :return: 提示词内容
        """
        # 基础提示词
        base_prompt = self.load_prompt("Agent Prompt.txt")

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

        # 啰嗦模式
        verbose_spec = ""
        if not settings.AI_AGENT_VERBOSE:
            verbose_spec = (
                "\n\n[Important Instruction] STRICTLY ENFORCED: DO NOT output any conversational "
                "text, thinking processes, or explanations before or during tool calls. Call tools "
                "directly without any transitional phrases. "
                "You MUST remain completely silent until the task is completely finished. "
                "DO NOT output any content whatsoever until your final summary reply."
            )

        # MoviePilot系统信息
        moviepilot_info = self._get_moviepilot_info()
        voice_reply_spec = self._generate_voice_reply_instructions(
            prefer_voice_reply=prefer_voice_reply
        )

        # 始终替换占位符，避免后续 .format() 时因残留花括号报 KeyError
        base_prompt = base_prompt.format(
            markdown_spec=markdown_spec,
            verbose_spec=verbose_spec,
            moviepilot_info=moviepilot_info,
            voice_reply_spec=voice_reply_spec,
        )

        return base_prompt

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
            db_info = f"PostgreSQL ({settings.DB_POSTGRESQL_USERNAME}:{db_password}@{settings.DB_POSTGRESQL_HOST}:{settings.DB_POSTGRESQL_PORT}/{settings.DB_POSTGRESQL_DATABASE})"

        info_lines = [
            f"- 当前时间: {strftime('%Y-%m-%d %H:%M:%S')}",
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
    def _generate_voice_reply_instructions(prefer_voice_reply: bool) -> str:
        if not prefer_voice_reply:
            return (
                "- Voice replies: Use normal text replies by default. "
                "Only call `send_voice_message` when spoken playback is clearly better than plain text."
            )
        return (
            "- Current message context: The user sent a voice message.\n"
            "- Reply preference: Prioritize calling `send_voice_message` for the main user-facing reply.\n"
            "- Fallback: If voice is unavailable on the current channel, `send_voice_message` will fall back to text.\n"
            "- Do not repeat the same full reply again after calling `send_voice_message`."
        )

    def clear_cache(self):
        """
        清空缓存
        """
        self.prompts_cache.clear()
        logger.info("提示词缓存已清空")


prompt_manager = PromptManager()
