import asyncio

from app.application.agenttask import AgentTaskRepository
from app.application.scheduling import (
    get_scheduler,
    register_scheduler_class,
    reset_scheduler_class,
)
from app.scheduler import Scheduler


def configure_scheduler_runtime() -> None:
    """在显式装配阶段登记 concrete Scheduler。"""
    register_scheduler_class(Scheduler)


def reset_scheduler_runtime() -> None:
    """清除 concrete Scheduler 登记，支持重复 lifespan。"""
    reset_scheduler_class()


def configure_scheduler_agent_tasks(repository: AgentTaskRepository) -> None:
    """在调度器启动前注入自主任务仓储。"""
    configure_scheduler_runtime()
    get_scheduler().configure_agent_tasks(repository)


def init_scheduler():
    """
    初始化定时器
    """
    configure_scheduler_runtime()
    try:
        Scheduler().init()
    except Exception:
        reset_scheduler_runtime()
        raise


def stop_scheduler():
    """
    停止定时器；生命周期事件循环中返回可等待的收口协程。
    """
    scheduler = Scheduler()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            scheduler.stop()
        finally:
            reset_scheduler_runtime()
        return None

    async def stop_and_reset() -> None:
        """等待异步调度器收口后清除 provider。"""
        try:
            await scheduler.stop_async()
        finally:
            reset_scheduler_runtime()

    return stop_and_reset()


def restart_scheduler():
    """
    重启定时器
    """
    configure_scheduler_runtime()
    Scheduler().init()


def init_plugin_scheduler():
    """
    初始化插件定时器
    """
    Scheduler().init_plugin_jobs()
