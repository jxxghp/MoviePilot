"""Agent 工具标签定义。"""

from enum import Enum


class ToolTag(str, Enum):
    """Agent 工具能力标签。"""

    AgentTool = "agent_tool"
    Read = "read"
    Write = "write"
    Admin = "admin"
    Message = "message"
    UserInteraction = "user_interaction"
    TerminalResponse = "terminal_response"
    Media = "media"
    Resource = "resource"
    Site = "site"
    Subscription = "subscription"
    Download = "download"
    Library = "library"
    Transfer = "transfer"
    System = "system"
    Settings = "settings"
    Plugin = "plugin"
    Workflow = "workflow"
    Scheduler = "scheduler"
    AgentTask = "agent_task"
    File = "file"
    Directory = "directory"
    Web = "web"
    Command = "command"
    FilterRule = "filter_rule"
    Persona = "persona"
    SlashCommand = "slash_command"
    Recommendation = "recommendation"
    Metadata = "metadata"
    Skill = "skill"


__all__ = ["ToolTag"]
