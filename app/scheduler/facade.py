"""调度器具体实现的组合门面与旧插件 ABI。"""

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from app.application.agenttask import AgentTaskRepository
from app.foundation.singleton import SingletonClass
from app.runtime.events import Event, eventmanager
from app.runtime.reload import ConfigReloadMixin
from app.scheduler.bridge import SchedulerBridgeOwner
from app.scheduler.catalog import SchedulerCatalogOwner
from app.scheduler.execution import SchedulerExecutionOwner
from app.scheduler.lifecycle import SchedulerLifecycleOwner
from app.scheduler.maintenance import SchedulerMaintenanceOwner
from app.scheduler.progress import SchedulerProgressOwner
from app.scheduler.reconcile import SchedulerReconcileOwner
from app.scheduler.registry import ExecutionRegistry
from app.scheduler.services import SchedulerServices
from app.schemas.types import EventType, SystemConfigKey

_PUBLIC_MODULE = "app.scheduler"
_Handler = TypeVar("_Handler", bound=Callable[..., Any])


def _public_handler(handler: _Handler) -> _Handler:
    """在事件注册前恢复插件可见的稳定模块身份。"""
    handler.__module__ = _PUBLIC_MODULE
    return handler


class Scheduler(
    SchedulerLifecycleOwner,
    SchedulerReconcileOwner,
    SchedulerBridgeOwner,
    SchedulerProgressOwner,
    SchedulerExecutionOwner,
    SchedulerCatalogOwner,
    SchedulerMaintenanceOwner,
    ConfigReloadMixin,
    metaclass=SingletonClass,
):
    """
    定时任务管理
    """

    __module__ = _PUBLIC_MODULE

    CONFIG_WATCH = {
        "DEV",
        "COOKIECLOUD_INTERVAL",
        "MEDIASERVER_SYNC_INTERVAL",
        SystemConfigKey.MediaServers.value,
        "SUBSCRIBE_SEARCH",
        "SUBSCRIBE_SEARCH_INTERVAL",
        "SUBSCRIBE_MODE",
        "SUBSCRIBE_RSS_INTERVAL",
        "SITEDATA_REFRESH_INTERVAL",
        "AI_AGENT_ENABLE",
        "AI_AGENT_JOB_INTERVAL",
        "DATA_CLEANUP_ENABLE",
        "DATA_CLEANUP_MESSAGE_DAYS",
        "DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS",
        "DATA_CLEANUP_SITE_USERDATA_DAYS",
        "DATA_CLEANUP_TRANSFER_HISTORY_DAYS",
        "DATA_CLEANUP_DOWNLOAD_FAILURE_DAYS",
        "DATA_CLEANUP_SUBSCRIBE_HISTORY_DAYS",
        "DATA_CLEANUP_AGENT_CHAT_DAYS",
        "DATA_CLEANUP_AGENT_TASK_RUN_DAYS",
        "DATA_CLEANUP_OUTBOX_COMPLETED_DAYS",
        "DATA_CLEANUP_OUTBOX_DEAD_DAYS",
        "DB_BACKUP_ENABLE",
        "DB_BACKUP_CRON",
        "USAGE_STATISTIC_SHARE",
    }

    def __init__(self) -> None:
        """创建调度器状态；后台任务由应用生命周期显式启动。"""
        # 定时服务
        self._scheduler = None
        # 退出事件
        self._event = threading.Event()
        # 锁
        self._lock = threading.RLock()
        # 各服务的运行状态
        self._jobs = {}
        # 生命周期门禁与事件循环句柄由调度器实例独立持有。
        self._lifecycle_state = "new"
        self._registry = ExecutionRegistry(self._lock)
        # 进程启动时只对账一次，配置热重载不得改写仍在执行的任务状态
        self._agent_task_interruptions_reconciled = False
        # 用户认证失败次数
        self._auth_count = 0
        # 用户认证失败消息发送
        self._auth_message = False
        # 插件已按认证结果重建，但动态路由尚未完成投影时保留重试状态。
        self._auth_plugin_routes_pending = False
        self._agent_tasks: AgentTaskRepository | None = None
        self._services: SchedulerServices | None = None

    def configure_services(self, services: SchedulerServices) -> None:
        """在调度器启动前绑定由组合根构造的业务能力。"""
        if self._lifecycle_state not in {"new", "stopped"}:
            raise RuntimeError("Scheduler 已运行，不能替换业务能力")
        self._services = services

    def _scheduler_services(self) -> SchedulerServices:
        """返回显式注入能力；缺少组合根装配时稳定失败。"""
        if self._services is None:
            raise RuntimeError("Scheduler 的业务能力尚未注入")
        return self._services

    def get_reload_name(self) -> str:
        """
        获取配置重载日志中的服务名称。
        """
        return "定时服务"

    @eventmanager.register(EventType.PluginReload)  # type: ignore[misc]
    @_public_handler
    def on_plugin_reload(self, event: Event) -> None:
        """插件重载后按当前实例重新注册全部定时服务"""
        plugin_id = event.event_data.get("plugin_id")
        if not plugin_id:
            return
        self.update_plugin_job(plugin_id)
