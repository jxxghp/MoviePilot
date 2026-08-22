from concurrent.futures import Future

from app.application.commands import register_command_class
from app.application.messaging.gateway import CommandChain
from app.runtime.command import Command, register_builtin_commands, register_command_messenger
from app.startup.bindings.builtin_commands import builtin_commands

# 导入期即向 application 门面注册命令类，保证工具调用时不依赖静态边。
register_command_class(Command)
# 导入期即向命令中枢注册内建命令清单，命令中枢因此不认识任何业务链。
register_builtin_commands(builtin_commands)
# 导入期即向命令中枢注册消息网关，命令中枢因此不依赖应用层的消息实现。
register_command_messenger(CommandChain)


def init_command():
    """
    初始化命令
    """
    Command()


def stop_command():
    """
    停止命令
    """
    pass


def restart_command() -> Future:
    """
    重建命令并返回完成信号。
    """
    return Command().init_commands()
