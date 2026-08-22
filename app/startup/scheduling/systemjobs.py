"""宿主自己实现的系统定时作业。"""

import gc
from typing import Any, Callable

from app.application.database import get_database_governance
from app.application.orchestration.scheduler import SchedulerChain
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.config import settings
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.gc import get_memory_usage
from app.runtime.log import logger
from app.schemas.message import Message, MessageType
from app.schemas.types import SystemConfigKey


def database_backup() -> Any:
    """
    按当前宿主策略创建一次数据库备份。

    :return: 备份结果
    """
    return get_database_governance().create_backup()


def clear_cache() -> None:
    """
    广播缓存清理。
    """
    SchedulerChain().clear_cache()


def full_gc() -> None:
    """
    主动内存回收并记录释放量。
    """
    memory_before = get_memory_usage()
    collected = gc.collect()
    memory_after = get_memory_usage()
    memory_freed = memory_before - memory_after
    logger.info(
        f"主动内存回收完成，回收对象数: {collected}，释放内存: {memory_freed:.2f} MB"
    )


async def agent_heartbeat() -> None:
    """
    智能体心跳唤醒：检查并执行待处理的定时任务。
    """
    from app.agent.runtime_loader import get_running_agent_manager

    manager = get_running_agent_manager()
    if manager is None:
        logger.debug("智能助手服务未运行，跳过心跳任务")
        return
    await manager.heartbeat_check_jobs()


class UserAuthChecker:
    """
    用户认证检查作业。

    失败次数在进程内累计，超过上限后停止重试并只提示一次；认证通过时清零计数
    并回调宿主重新装载插件定时服务。
    """

    MAX_RETRY = 30

    def __init__(self, on_authenticated: Callable[[], None]) -> None:
        """
        创建认证检查作业。

        :param on_authenticated: 认证通过后的回调
        """
        self._on_authenticated = on_authenticated
        self._auth_count = 0
        self._auth_message = False

    def check(self) -> None:
        """
        检查用户认证状态，未认证时尝试认证。
        """
        if SitesHelper().auth_level >= 2:
            return
        if self._auth_count > self.MAX_RETRY:
            if not self._auth_message:
                SchedulerChain().messagehelper.put(
                    title="用户认证失败",
                    message="用户认证失败次数过多，将不再尝试认证！",
                    role="system",
                )
                self._auth_message = True
            return
        logger.info("用户未认证，正在尝试认证...")
        auth_conf = SystemConfigOper().get(SystemConfigKey.UserSiteAuthParams)
        if auth_conf:
            status, msg = SitesHelper().check_user(**auth_conf)
        else:
            status, msg = SitesHelper().check_user()
        if status:
            self._auth_count = 0
            logger.info(f"{msg} 用户认证成功")
            SchedulerChain().post_message(
                Message(
                    mtype=MessageType.Manual,
                    title="MoviePilot用户认证成功",
                    text=f"使用站点：{msg}，如有插件使用异常，请重启MoviePilot。",
                    link=settings.MP_DOMAIN("#/site"),
                )
            )
            # 认证通过后重新初始化插件
            PluginManager().init_config()
            self._on_authenticated()
        else:
            self._auth_count += 1
            logger.error(f"用户认证失败，{msg}，共失败 {self._auth_count} 次")
            if self._auth_count >= self.MAX_RETRY:
                logger.error("用户认证失败次数过多，将不再尝试认证！")
