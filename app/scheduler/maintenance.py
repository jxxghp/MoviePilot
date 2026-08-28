"""调度器内置维护任务。"""

import gc

from app.application.backup import BackupArtifact
from app.application.database import get_database_governance
from app.runtime.gc import get_memory_usage
from app.runtime.log import logger
from app.scheduler.contract import _SchedulerOwnerBase


class SchedulerMaintenanceOwner(_SchedulerOwnerBase):
    """调度器内置维护任务。"""

    @staticmethod
    def database_backup() -> BackupArtifact:
        """按当前宿主策略创建一次定时数据库备份。"""
        return get_database_governance().create_backup()

    def clear_cache(self) -> None:
        """
        清理缓存
        """
        self._scheduler_services().clear_cache()

    @staticmethod
    def full_gc() -> None:
        """
        主动内存回收
        """
        memory_before = get_memory_usage()
        collected = gc.collect()
        memory_after = get_memory_usage()
        memory_freed = memory_before - memory_after
        logger.info(f"主动内存回收完成，回收对象数: {collected}，释放内存: {memory_freed:.2f} MB")

    @staticmethod
    async def agent_heartbeat() -> None:
        """
        智能体心跳唤醒：检查并执行待处理的定时任务
        """
        from app.application.agent import get_running_agent_manager

        manager = get_running_agent_manager()
        if manager is None:
            logger.debug("智能助手服务未运行，跳过心跳任务")
            return
        await manager.heartbeat_check_jobs()
