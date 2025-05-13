from scheduler import Scheduler


def init_scheduler():
    """
    初始化定时器
    """
    Scheduler()

def stop_scheduler():
    """
    停止定时器
    """
    Scheduler().stop()

def restart_scheduler():
    """
    重启定时器
    """
    Scheduler().init()

def init_plugin_scheduler():
    """
    初始化插件定时器
    """
    Scheduler().init_plugin_jobs()
