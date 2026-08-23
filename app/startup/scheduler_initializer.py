import asyncio

from app.application.scheduling import register_scheduler_class
from app.scheduler import Scheduler

# 导入期即向 application 门面注册调度器类，保证工具调用时不依赖静态边。
register_scheduler_class(Scheduler)


def init_scheduler():
    """
    初始化定时器
    """
    Scheduler().init()


def stop_scheduler():
    """
    停止定时器；生命周期事件循环中返回有限等待的兼容协程。
    """
    scheduler = Scheduler()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        scheduler.stop()
        return None
    return scheduler.async_stop()


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
