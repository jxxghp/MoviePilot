"""工作流定时服务的调度登记。

本模块只负责把工作流的 cron 配置翻译成调度器登记项，作业执行状态、锁与
调度器实例由组合根持有，混入类通过 ``self`` 取用。
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

from app.application.orchestration.scheduler import SchedulerChain
from app.runtime.log import logger
from app.schemas.workflow import Workflow
from app.workflow.service import WorkflowChain


class WorkflowScheduling:
    """工作流定时服务的注册与回收。"""

    def init_workflow_jobs(self):
        """
        初始化工作流定时服务
        """
        for workflow in WorkflowChain().get_timer_workflows() or []:
            self.update_workflow_job(workflow)

    def remove_workflow_job(self, workflow: Workflow):
        """
        移除工作流服务
        """
        if not self._scheduler:
            return
        with self._lock:
            job_id = f"workflow-{workflow.id}"
            service = self._jobs.pop(job_id, {})
            if not service:
                return
            try:
                # 在调度器中查找并移除对应的 job
                job_removed = False
                for job in list(self._scheduler.get_jobs()):
                    if job_id == job.id:
                        try:
                            self._scheduler.remove_job(job.id)
                            job_removed = True
                        except JobLookupError:
                            pass
                        break
                if job_removed:
                    logger.info(f"移除工作流服务：{service.get('name')}")
            except Exception as e:
                logger.error(f"移除工作流服务失败：{str(e)} - {job_id}: {service}")
                SchedulerChain().messagehelper.put(
                    title=f"工作流 {workflow.name} 服务移除失败",
                    message=str(e),
                    role="system",
                )

    def update_workflow_job(self, workflow: Workflow):
        """
        更新工作流定时服务
        """
        if not self._scheduler:
            return
        # 移除该工作流的全部服务
        self.remove_workflow_job(workflow)
        # 添加工作流服务
        with self._lock:
            try:
                job_id = f"workflow-{workflow.id}"
                self._jobs[job_id] = {
                    "func": WorkflowChain().process,
                    "name": workflow.name,
                    "provider_name": "工作流",
                    "running": False,
                }
                self._scheduler.add_job(
                    self.start,
                    trigger=CronTrigger.from_crontab(workflow.timer),
                    id=job_id,
                    name=workflow.name,
                    kwargs={"job_id": job_id, "workflow_id": workflow.id},
                    replace_existing=True,
                )
                logger.info(f"注册工作流服务：{workflow.name} - {workflow.timer}")
            except Exception as e:
                logger.error(f"注册工作流服务失败：{workflow.name} - {str(e)}")
                SchedulerChain().messagehelper.put(
                    title=f"工作流 {workflow.name} 服务注册失败",
                    message=str(e),
                    role="system",
                )
