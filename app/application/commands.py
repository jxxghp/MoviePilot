"""命令工具服务门面。

Agent 工具与 API 端点对命令注册表的操作统一经本模块调用，
Command 实现由 startup 组合根在生命周期启动阶段注册，避免 application 层
静态依赖顶层 command 模块。

依赖方向：

    agent.tools / api.endpoints -> application.commands <- startup（注册 Command 类）
"""

from collections.abc import Callable
from typing import Any, Dict, Optional

from app.schemas.types import EventType, NotificationChannel

# Command 类：由 startup/initializers/command.py 在显式启动阶段注册。
_command_class: Any = None


def register_command_class(command_class: Any) -> Any:
    """注册 Command 类，并返回先前实现供隔离测试或失败回滚。"""
    global _command_class
    previous = _command_class
    _command_class = command_class
    return previous


def reset_command_class() -> None:
    """清除 Command 实现，防止重复 lifespan 保留上一轮 provider。"""
    register_command_class(None)


def get_command_object() -> Any:
    """返回命令注册表实例。"""
    if _command_class is None:
        raise RuntimeError("命令服务未初始化：请先通过 register_command_class 注册 Command 类")
    return _command_class()


def get_commands() -> Dict[str, Any]:
    """返回全部已注册命令。"""
    return get_command_object().get_commands()


def get_command(name: str) -> Optional[Any]:
    """按命令名查询注册表。"""
    return get_command_object().get(name)


def init_commands(plugin_id: Optional[str] = None) -> None:
    """初始化命令（可指定单个插件）。"""
    get_command_object().init_commands(plugin_id)


def dispatch_command(
    command: str,
    *,
    user_id: str,
    channel: Optional[NotificationChannel] = None,
    source: Optional[str] = None,
    publish_event: Callable[[EventType, dict[str, Any]], Any],
) -> dict[str, Any]:
    """校验斜杠命令并通过注入的事件端口异步触发执行。"""
    normalized = command.strip()
    if not normalized:
        raise ValueError("命令不能为空")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    command_name = normalized.split()[0]
    matched = get_command(command_name)
    if matched is None:
        available = [f"{name} - {info.get('description', '无描述')}" for name, info in get_commands().items()]
        raise ValueError(f"命令 {command_name} 不存在" + (f"；可用命令: {'; '.join(available)}" if available else ""))
    publish_event(
        EventType.CommandExcute,
        {
            "cmd": normalized,
            "user": user_id,
            "channel": channel,
            "source": source,
        },
    )
    return {
        "message": f"命令 {command_name} 已触发执行",
        "command": normalized,
        "command_desc": matched.get("description", ""),
        **({"plugin_id": matched["pid"]} if matched.get("pid") else {}),
    }
