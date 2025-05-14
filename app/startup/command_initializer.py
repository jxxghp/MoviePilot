from app.command import Command


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


def restart_command():
    """
    重启命令
    """
    Command().init_commands()
