"""命令工具服务门面。

Agent 工具与 API 端点对命令注册表的操作统一经本模块调用，
Command 实现由 startup 组合根在导入期注册，避免 application 层
静态依赖顶层 command 模块。

依赖方向：

    agent.tools / api.endpoints -> application.commands <- startup（注册 Command 类）
"""

from typing import Any, Dict, List, Optional

# Command 类：由 startup/command_initializer 在导入期注册。
_command_class: Any = None


def register_command_class(command_class: Any) -> None:
    """注册 Command 类（组合根在导入期调用）。"""
    global _command_class
    _command_class = command_class


def get_command_object() -> Any:
    """返回命令注册表实例。"""
    if _command_class is None:
        raise RuntimeError(
            "命令服务未初始化：请先通过 register_command_class 注册 Command 类"
        )
    return _command_class()


def get_commands() -> Dict[str, Any]:
    """返回全部已注册命令。"""
    return get_command_object().get_commands()


def get_command(name: str) -> Optional[Any]:
    """按命令名查询注册表。"""
    return get_command_object().get(name)


def get_command_origins() -> List[Any]:
    """返回全部命令词的来源分层，含失效的插件声明。"""
    return get_command_object().command_origins()


def init_commands(plugin_id: Optional[str] = None) -> None:
    """初始化命令（可指定单个插件）。"""
    get_command_object().init_commands(plugin_id)
