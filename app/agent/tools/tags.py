"""
Agent 工具标签定义。
"""

from enum import Enum


class ToolTag(str, Enum):
    """
    Agent 工具能力标签。
    """

    # 标记由 Agent 工厂管理的宿主工具
    AgentTool = "agent_tool"
    # 读取信息的能力
    Read = "read"
    # 修改状态或产生外部副作用的能力
    Write = "write"
    # 涉及管理员操作的能力分类
    Admin = "admin"
    # 向用户发送消息的能力
    Message = "message"
    # 需要用户作出选择的交互能力
    UserInteraction = "user_interaction"
    # 直接完成本轮用户回复的能力
    TerminalResponse = "terminal_response"
    # 媒体信息查询或操作
    Media = "media"
    # 可下载资源查询或操作
    Resource = "resource"
    # 站点配置及站点内容操作
    Site = "site"
    # 订阅管理与执行
    Subscription = "subscription"
    # 下载器任务及文件操作
    Download = "download"
    # 媒体库查询与管理
    Library = "library"
    # 文件整理和转移
    Transfer = "transfer"
    # 系统状态检查和运维
    System = "system"
    # 运行配置查询与修改
    Settings = "settings"
    # 插件管理及插件扩展操作
    Plugin = "plugin"
    # 工作流定义和执行
    Workflow = "workflow"
    # 定时作业查询与调度
    Scheduler = "scheduler"
    # 持久化自主任务管理
    AgentTask = "agent_task"
    # 本地文件读取和修改
    File = "file"
    # 本地目录查询和管理
    Directory = "directory"
    # 网络搜索和网页交互
    Web = "web"
    # 宿主命令或代码执行
    Command = "command"
    # 资源过滤规则管理
    FilterRule = "filter_rule"
    # 智能体人格管理
    Persona = "persona"
    # 斜杠命令发现与执行
    SlashCommand = "slash_command"
    # 媒体推荐查询
    Recommendation = "recommendation"
    # 媒体元数据读取与维护
    Metadata = "metadata"
    # 技能发现、加载和管理
    Skill = "skill"


__all__ = ["ToolTag"]
