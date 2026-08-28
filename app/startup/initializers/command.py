from concurrent.futures import Future

from app.application.commands import register_command_class, reset_command_class
from app.command import Command


def configure_command_runtime() -> None:
    """在命令生命周期启动阶段登记 concrete Command。"""
    register_command_class(Command)


def reset_command_runtime() -> None:
    """清除 concrete Command 登记，支持重复 lifespan。"""
    reset_command_class()


def init_command():
    """
    初始化命令
    """
    configure_command_runtime()
    try:
        Command()
    except Exception:
        reset_command_runtime()
        raise


def restart_command() -> Future:
    """
    重建命令并返回完成信号。
    """
    return Command().init_commands()


def stop_command() -> None:
    """释放命令 provider；Command 实例本身没有独立关闭资源。"""
    reset_command_runtime()
