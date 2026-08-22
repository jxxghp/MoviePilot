"""定时服务组合根：把执行引擎与四类作业的登记路径装配到一起。"""

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.application.messaging.message import MessageHelper
from app.foundation.singleton import SingletonClass
from app.runtime.config import settings
from app.runtime.reload import ConfigReloadMixin
from app.runtime.scheduler import SchedulerEngine, lock
from app.schemas.types import SystemConfigKey
from app.scheduler.agent_tasks import AgentTaskScheduling
from app.scheduler.plugins import PluginScheduling
from app.scheduler.workflows import WorkflowScheduling
from app.startup.bindings.scheduling.manifest import build_host_jobs
from app.startup.bindings.scheduling.systemjobs import UserAuthChecker


class Scheduler(
    SchedulerEngine,
    AgentTaskScheduling,
    WorkflowScheduling,
    PluginScheduling,
    ConfigReloadMixin,
    metaclass=SingletonClass,
):
    """
    定时任务管理
    """

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
        "DB_BACKUP_ENABLE",
        "DB_BACKUP_CRON",
        "USAGE_STATISTIC_SHARE",
    }

    def __init__(self):
        """创建调度器状态；后台任务由应用生命周期显式启动。"""
        super().__init__()
        # 进程启动时只对账一次，配置热重载不得改写仍在执行的任务状态
        self._agent_task_interruptions_reconciled = False
        # 认证失败计数跨配置重载保持，认证检查作业与调度器实例同生命周期
        self._user_auth = UserAuthChecker(on_authenticated=self.init_plugin_jobs)

    def notify_job_failure(self, title: str, message: str) -> None:
        """
        把作业失败提示投递到消息中心。

        :param title: 提示标题
        :param message: 提示正文
        """
        MessageHelper().put(title=title, message=message, role="system")

    def on_config_changed(self) -> None:
        """
        配置变更后重新初始化定时服务。
        """
        self.init()

    def get_reload_name(self) -> str:
        """
        获取配置重载日志中的服务名称。
        """
        return "定时服务"

    def init(self) -> None:
        """
        初始化定时服务
        """

        # 停止定时服务
        self.stop()

        # 调试模式不启动定时服务
        if settings.DEV:
            return

        # 对账上个进程未收口的 Agent 任务；进程内重复初始化不会重复改写状态。
        self._reconcile_agent_task_interruptions()

        with lock:
            self._jobs = {}
            self._scheduler = BackgroundScheduler(
                timezone=settings.TZ,
                executors={"default": ThreadPoolExecutor(settings.CONF.scheduler)},
            )

            # 宿主业务作业按清单登记
            for job in build_host_jobs(user_auth=self._user_auth.check):
                self._register_job(job)

            # 初始化工作流服务
            self.init_workflow_jobs()

            # 恢复 Agent 自主定时任务
            if settings.AI_AGENT_ENABLE:
                self.init_agent_task_jobs()

            # 初始化插件服务
            self.init_plugin_jobs()

            # 启动定时服务
            self._scheduler.start()
