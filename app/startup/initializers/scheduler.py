import asyncio

from app.application.agenttask import AgentTaskRepository
from app.application.scheduling import get_scheduler, register_scheduler_class
from app.scheduler import Scheduler

# 导入期即向 application 门面注册调度器类，保证工具调用时不依赖静态边。
register_scheduler_class(Scheduler)


def configure_scheduler_agent_tasks(repository: AgentTaskRepository) -> None:
    """在调度器启动前注入自主任务仓储。"""
    get_scheduler().configure_agent_tasks(repository)


def init_scheduler():
    """
    初始化定时器
    """
    Scheduler().init()


def stop_scheduler():
    """
    停止定时器；生命周期事件循环中返回可等待的收口协程。
    """
    scheduler = Scheduler()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        scheduler.stop()
        return None
    return scheduler.stop_async()


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
