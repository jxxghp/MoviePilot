"""调度器业务能力装配与生命周期入口。"""

import asyncio
from typing import Awaitable, Optional

from app.application.agenttask import AgentTaskRepository
from app.application.image import WallpaperHelper
from app.application.scheduling import (
    get_scheduler,
    register_scheduler_class,
    reset_scheduler_class,
)
from app.chain.mediaserver import MediaServerChain
from app.chain.recommend import RecommendChain
from app.chain.site import SiteChain
from app.chain.subscribe.facade import SubscribeChain
from app.chain.transfer.facade import TransferChain
from app.chain.workflow import WorkflowChain
from app.scheduler.chain import SchedulerChain
from app.scheduler.facade import Scheduler
from app.scheduler.services import SchedulerServices


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


def configure_scheduler_services() -> None:
    """构造一次调度任务能力，并在启动前注入 concrete Scheduler。"""
    scheduler_chain = SchedulerChain()
    site_chain = SiteChain()
    mediaserver_chain = MediaServerChain()
    subscribe_chain = SubscribeChain()
    transfer_chain = TransferChain()
    recommend_chain = RecommendChain()
    workflow_chain = WorkflowChain()
    Scheduler().configure_services(
        SchedulerServices(
            sync_cookies=site_chain.sync_cookies,
            sync_mediaserver=mediaserver_chain.sync,
            check_subscribe=subscribe_chain.check,
            search_subscribe=subscribe_chain.search,
            refresh_subscribe=subscribe_chain.refresh,
            follow_subscribe=subscribe_chain.follow,
            process_transfer=transfer_chain.process,
            clear_cache=scheduler_chain.clear_cache,
            cleanup_data=scheduler_chain.cleanup,
            run_modules=scheduler_chain.scheduler_job,
            get_wallpapers=WallpaperHelper().get_wallpapers,
            refresh_site_data=site_chain.refresh_userdatas,
            refresh_recommend=recommend_chain.refresh_recommend,
            cache_subscribe_calendar=subscribe_chain.cache_calendar,
            list_workflows=workflow_chain.get_timer_workflows,
            process_workflow=workflow_chain.process,
            put_message=scheduler_chain.messagehelper.put,
            post_message=scheduler_chain.post_message,
        )
    )


def init_scheduler() -> None:
    """
    初始化定时器
    """
    configure_scheduler_runtime()
    configure_scheduler_services()
    try:
        Scheduler().init()
    except Exception:
        reset_scheduler_runtime()
        raise


def stop_scheduler() -> Optional[Awaitable[None]]:
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


def restart_scheduler() -> None:
    """
    重启定时器
    """
    configure_scheduler_runtime()
    Scheduler().init()


def init_plugin_scheduler() -> None:
    """
    初始化插件定时器
    """
    Scheduler().init_plugin_jobs()
