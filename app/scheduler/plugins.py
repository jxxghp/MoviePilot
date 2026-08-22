"""插件定时服务的调度登记。

任务归属按服务声明的实例键构造，同一插件的多个实例声明同名服务 id 时互不
覆盖，按实例键回收也不会误伤兄弟实例。作业执行状态、锁与调度器实例由组合根
持有，混入类通过 ``self`` 取用。
"""

import traceback
from typing import Optional

from apscheduler.jobstores.base import JobLookupError

from app.application.orchestration.scheduler import SchedulerChain
from app.runtime.events import Event, eventmanager
from app.runtime.extensions.contract.instance import matches_extension, split_instance_key
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.log import logger, wrap_for_plugin_instance
from app.schemas.types import EventType


class PluginScheduling:
    """插件定时服务的注册、重载与按实例键回收。"""

    def init_plugin_jobs(self):
        """
        初始化插件定时服务
        """
        for pid in PluginManager().get_running_plugin_ids():
            self.update_plugin_job(pid)

    @eventmanager.register(EventType.PluginReload)
    def on_plugin_reload(self, event: Event) -> None:
        """插件重载后按当前实例重新注册全部定时服务"""
        plugin_id = event.event_data.get("plugin_id")
        if not plugin_id:
            return
        self.update_plugin_job(plugin_id)

    def remove_plugin_job(self, pid: str, job_id: Optional[str] = None):
        """
        移除定时服务，可以是单个服务（包括默认服务）或整个插件的所有服务
        :param pid: 插件 ID 或实例键，插件 ID 命中该插件全部实例的服务，实例键只命中该实例
        :param job_id: 可选，指定要移除的单个服务的 job_id。如果不提供，则移除该插件（或该实例）的所有服务，当移除单个服务时，默认服务也包含在内
        """
        if not self._scheduler:
            return
        with self._lock:
            if job_id:
                # 移除单个服务
                service = self._jobs.pop(job_id, None)
                if not service:
                    return
                jobs_to_remove = [(job_id, service)]
            else:
                # 移除插件（或该实例）的所有服务，按归属实例键筛选
                jobs_to_remove = [
                    (job_id, service)
                    for job_id, service in self._jobs.items()
                    if matches_extension(service.get("pid"), pid)
                ]
                for job_id, _ in jobs_to_remove:
                    self._jobs.pop(job_id, None)
            if not jobs_to_remove:
                return
            plugin_name = PluginManager().get_plugin_attr(pid, "plugin_name")
            # 遍历移除任务
            for job_id, service in jobs_to_remove:
                try:
                    # 在调度器中查找并移除对应的 job
                    job_removed = False
                    for job in list(self._scheduler.get_jobs()):
                        job_id_from_service = job.id.split("|")[0]
                        if job_id == job_id_from_service:
                            try:
                                self._scheduler.remove_job(job.id)
                                job_removed = True
                            except JobLookupError:
                                pass
                    if job_removed:
                        logger.info(
                            f"移除插件服务({plugin_name})：{service.get('name')}"
                        )  # noqa
                except Exception as e:
                    logger.error(f"移除插件服务失败：{str(e)} - {job_id}: {service}")
                    SchedulerChain().messagehelper.put(
                        title=f"插件 {plugin_name} 服务移除失败",
                        message=str(e),
                        role="system",
                    )

    def update_plugin_job(self, pid: str):
        """
        更新插件定时服务
        :param pid: 插件 ID 或实例键，插件 ID 时按插件当前全部实例重新注册
        """
        if not self._scheduler or not pid:
            return
        # 移除该插件（或该实例）的全部服务
        self.remove_plugin_job(pid)
        # 获取插件服务列表
        with self._lock:
            plugin_manager = PluginManager()
            try:
                plugin_services = plugin_manager.get_plugin_services(pid=pid)
            except Exception as e:
                logger.error(
                    f"运行插件 {pid} 服务失败：{str(e)} - {traceback.format_exc()}"
                )
                return
            # 任务 id 按服务声明的归属实例键（服务项的 pid 字段）构造，同一插件的
            # 多个实例声明同名服务 id 时才不会互相覆盖对方登记的任务
            for service in plugin_services:
                owner = service.get("pid") or pid
                plugin_name = plugin_manager.get_plugin_attr(owner, "plugin_name")
                try:
                    sid = f"{owner}_{service['id']}"
                    job_id = sid.split("|")[0]
                    self.remove_plugin_job(owner, job_id)
                    # 定时任务的实际调用发生在宿主稍后触发的调度线程/事件循环里，
                    # 这里把回调按其归属实例键包一层，使触发时的日志落到该实例目录
                    owner_plugin_id, owner_instance_id = split_instance_key(owner)
                    self._jobs[job_id] = {
                        "func": wrap_for_plugin_instance(
                            service["func"], owner_plugin_id, owner_instance_id
                        ),
                        "name": service["name"],
                        "pid": owner,
                        "provider_name": plugin_name,
                        "kwargs": service.get("func_kwargs") or {},
                        "running": False,
                    }
                    self._scheduler.add_job(
                        self.start,
                        service["trigger"],
                        id=sid,
                        name=service["name"],
                        **(service.get("kwargs") or {}),
                        kwargs={"job_id": job_id},
                        replace_existing=True,
                    )
                    logger.info(
                        f"注册插件{plugin_name}服务：{service['name']} - {service['trigger']}"
                    )
                except Exception as e:
                    logger.error(f"注册插件{plugin_name}服务失败：{str(e)} - {service}")
                    SchedulerChain().messagehelper.put(
                        title=f"插件 {plugin_name} 服务注册失败",
                        message=str(e),
                        role="system",
                    )
