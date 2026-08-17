"""查询所有可用斜杠命令工具（系统命令 + 插件命令）"""

import json
from typing import Optional, Type

from pydantic import BaseModel

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class ListSlashCommandsInput(BaseModel):
    """查询所有可用斜杠命令工具的输入参数模型"""


class ListSlashCommandsTool(MoviePilotTool):
    name: str = "list_slash_commands"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.SlashCommand,
        ToolTag.Admin,
    ]
    description: str = (
        "List all available slash commands in the system, including system preset commands "
        "(e.g. /cookiecloud, /sites, /subscribes, /downloading, /transfer, /restart, etc.) "
        "and plugin-registered commands. "
        "Use this tool to discover what slash commands are available before executing them with run_slash_command. "
        "This is especially useful when the user describes an action in natural language and you need to "
        "find the matching command to fulfill their request."
    )
    args_schema: Type[BaseModel] = ListSlashCommandsInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        return "查询所有可用命令"

    async def run(self, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}")

        try:
            from app.application.commands import get_commands

            all_commands = get_commands()

            if not all_commands:
                return "当前没有可用的命令"

            commands_list = []
            for cmd, info in all_commands.items():
                cmd_info = {
                    "command": cmd,
                    "description": info.get("description", ""),
                }
                if info.get("category"):
                    cmd_info["category"] = info["category"]
                # 标识命令类型
                if info.get("type") == "scheduler":
                    cmd_info["type"] = "scheduler"
                elif info.get("pid"):
                    cmd_info["type"] = "plugin"
                    cmd_info["plugin_id"] = info["pid"]
                else:
                    cmd_info["type"] = "system"
                commands_list.append(cmd_info)

            result = {
                "total": len(commands_list),
                "commands": commands_list,
            }
            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"查询可用命令失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"查询可用命令时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
