"""Scheduler 启动、热重载和关闭生命周期。"""

import asyncio
import threading
import traceback
from typing import Any

from app.application.configuration import (
    get_scheduler_runtime_config,
)
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.scheduler.contract import _SchedulerOwnerBase
from app.scheduler.registry import SchedulerHandle

lock = threading.Lock()


class SchedulerLifecycleOwner(_SchedulerOwnerBase):
    """Scheduler 启动、热重载和关闭生命周期。"""

    async def on_config_changed(self) -> None:
        """
        配置热重载期间保留旧任务目录，直到新调度器启动。
        """
        reload_started, scheduler = self._begin_reload()
        if not reload_started:
            return
        await asyncio.to_thread(self._snapshot_and_shutdown, scheduler)
        with self._lock:
            if self._lifecycle_state != "reloading":
                return
        services = getattr(self, "_services", None)
        clear_wallpaper_cache = getattr(services, "clear_wallpaper_cache", None)
        if callable(clear_wallpaper_cache):
            clear_wallpaper_cache()
        self.init(_already_stopped=True)

    def _snapshot_and_shutdown(self, scheduler: Any) -> None:
        """在线程池中保存旧任务目录并收口旧调度器，避免阻塞事件循环。"""
        try:
            schedule_snapshot = self.list()
        except Exception as err:
            logger.error("保存定时服务重载前列表失败：%s", err)
            schedule_snapshot = []
        with self._lock:
            if self._lifecycle_state != "reloading":
                return
            self._reload_schedule_snapshot = schedule_snapshot
            if self._scheduler is scheduler:
                self._scheduler = None
        if scheduler:
            try:
                scheduler.remove_all_jobs()
            except Exception as err:
                logger.error("移除定时任务失败：%s", err)
        self._shutdown_scheduler_sync(scheduler)

    def init(self, *, _already_stopped: bool = False) -> None:
        """
        初始化定时服务
        """

        config = get_scheduler_runtime_config()
        # 停止定时服务
        if not _already_stopped:
            self.stop()

        # 调试模式不启动定时服务
        if config.dev:
            with self._lock:
                self._lifecycle_state = "stopped"
                self._reload_schedule_snapshot = None
            return

        # 对账上个进程未收口的 Agent 任务；进程内重复初始化不会重复改写状态。
        self._reconcile_agent_task_interruptions()
        with lock:
            with self._lock:
                self._event.clear()
                self._lifecycle_state = "starting"
            self._initialize_catalog(config)

            # 启动定时服务
            self._scheduler.start()
            with self._lock:
                self._lifecycle_state = "running"
                self._reload_schedule_snapshot = None

    def _begin_stop(self) -> tuple[Any, tuple[SchedulerHandle, ...]]:
        """关闭提交入口并摘出当前调度器与其拥有的异步句柄。"""
        with self._lock:
            self._lifecycle_state = "stopping"
            self._event.set()
            scheduler = self._scheduler
            self._scheduler = None
            self._reload_schedule_snapshot = None
            handles = self._registry.stop_snapshot()
        if scheduler:
            try:
                scheduler.remove_all_jobs()
            except Exception as err:
                logger.error("移除定时任务失败：%s", err)
        return scheduler, handles

    def _begin_reload(self) -> tuple[bool, Any]:
        """关闭旧计划的提交入口，保留已开始任务直到其自然完成。"""
        with self._lock:
            if runtime_stop_state.is_system_stopped or self._lifecycle_state in {"stopping", "reloading"}:
                return False, None
            self._lifecycle_state = "reloading"
            self._event.set()
            scheduler = self._scheduler
            self._registry.clear_reservations()
        return True, scheduler

    @staticmethod
    def _shutdown_scheduler_sync(scheduler: Any) -> None:
        """等待 APScheduler 自有线程池停止。"""
        if scheduler and scheduler.running:
            scheduler.shutdown()

    def stop(self) -> None:
        """
        关闭定时服务的同步兼容入口。

        应用生命周期使用 ``stop_async``，以便等待事件循环中的协程句柄；同步
        调用方仍可请求取消并等待 APScheduler 自有线程池收口。
        """
        with lock:
            try:
                scheduler, handles = self._begin_stop()
                for handle in handles:
                    self._cancel_handle(handle)
                self._shutdown_scheduler_sync(scheduler)
                with self._lock:
                    self._lifecycle_state = "stopped"
                logger.info("定时任务停止完成")
            except Exception as err:
                logger.error(f"停止定时任务失败：{err} - {traceback.format_exc()}")

    async def stop_async(self) -> None:
        """关闭调度器并等待已投递协程收口。"""
        scheduler, handles = self._begin_stop()
        for handle in handles:
            self._cancel_handle(handle)
        await asyncio.to_thread(self._shutdown_scheduler_sync, scheduler)
        await self._await_cancelled_handles(handles)
        with self._lock:
            self._lifecycle_state = "stopped"
        logger.info("定时任务停止完成")
