"""运行斜杠命令工具（系统命令 + 插件命令）"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.notification import resolve_channel
from app.schemas.types import EventType


class RunSlashCommandInput(BaseModel):
    """运行斜杠命令工具的输入参数模型"""

    command: str = Field(
        ...,
        description="The slash command to execute, e.g. '/cookiecloud'. "
        "Must start with '/'. Can include arguments after the command, e.g. '/command arg1 arg2'. "
        "Use query_plugin_capabilities tool to discover available plugin commands, "
        "or list_slash_commands tool to discover all available commands (including system commands).",
    )


class RunSlashCommandTool(MoviePilotTool):
    name: str = "run_slash_command"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.SlashCommand,
        ToolTag.Admin,
    ]
    description: str = (
        "Execute a slash command (system or plugin) by sending a CommandExcute event. "
        "This tool supports ALL registered slash commands, including: "
        "1) System preset commands (e.g. /cookiecloud, /sites, /subscribes, /downloading, /transfer, /restart, etc.) "
        "2) Plugin commands registered by installed plugins. "
        "Use the query_plugin_capabilities tool to discover plugin commands, "
        "or the list_slash_commands tool to discover all available commands. "
        "The command will be executed asynchronously. "
        "Note: This tool triggers the command execution but the actual processing happens in the background."
    )
    args_schema: Type[BaseModel] = RunSlashCommandInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        command = kwargs.get("command", "")
        return f"执行命令: {command}"

    async def run(self, command: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: command={command}")

        try:
            # 确保命令以 / 开头
            if not command.startswith("/"):
                command = f"/{command}"

            # 从命令注册表中验证命令是否存在（包含系统预设命令 + 插件命令 + 其他命令）
            from app.application.commands import get_command, get_commands

            cmd_name = command.split()[0]
            matched_command = get_command(cmd_name)

            if not matched_command:
                # 列出所有可用命令帮助用户
                all_commands = get_commands()
                available_cmds = [
                    f"{cmd} - {info.get('description', '无描述')}"
                    for cmd, info in all_commands.items()
                ]
                result = {
                    "success": False,
                    "message": f"命令 {cmd_name} 不存在",
                }
                if available_cmds:
                    result["available_commands"] = available_cmds
                return json.dumps(result, ensure_ascii=False, indent=2)

            # 构建消息渠道，优先使用当前会话的渠道信息
            channel = resolve_channel(self._channel)

            # 发送命令执行事件，与 message.py 中的方式一致
            eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": command,
                    "user": self._user_id,
                    "channel": channel,
                    "source": self._source,
                },
            )

            result = {
                "success": True,
                "message": f"命令 {cmd_name} 已触发执行",
                "command": command,
                "command_desc": matched_command.get("description", ""),
            }
            # 如果是插件命令，附加插件ID
            if matched_command.get("pid"):
                result["plugin_id"] = matched_command["pid"]
            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"执行命令失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"执行命令时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
