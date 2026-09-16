"""
整理任务：整理链的进程内工作项。

TransferTask 此前住在 app/schemas/transfer.py，但它不是出网的 DTO——meta 装的是领域侧
的 MetaBase 子类，mediainfo 装的是领域侧的 MediaInfo / MusicInfo，都带行为而非纯数据。
放在 app.schemas 的代价是它没法命名自己真正装的类型：app.schemas 一旦 import 领域类型，
app.schemas -> app.schemas.transfer -> app.domain.* -> app.schemas.types -> app.schemas
就闭环，仓库自己的 test_migrated_modules_are_not_in_import_cycles 会红（已实测）。于是
两个字段只能标成 Optional[Any]，把「这里到底能放什么」这件事整个交给了口头约定。

搬到应用层就没有这个约束：app.application 允许依赖 app.domain 与 app.schemas，两个
字段因此能标出真实类型。它面向前端的投影仍是 app/schemas/transfer.py 里的
TransferJob / TransferJobTask，那两个用 app.schemas 的同名 DTO——一个是工作项，一个是
视图，分开表达之后两边都不必再迁就对方。
"""
from typing import (
    Callable,
    List,
)

from app.application.transfer.feedback import TransferFailureNotification
from app.application.transfer.jobs import (
    DirectorySize,
    JobManager,
    configure_directory_size,
    job_lock,
)
from app.application.transfer.models import (
    TRANSFER_ADMISSION_ACCEPTED,
    TRANSFER_ADMISSION_PLANNED,
    TRANSFER_ADMISSION_PROVIDER_PENDING,
    TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION,
    TRANSFER_PLAN_CHECKPOINT_VERSION,
    TRANSFER_PLANNING_INPUT_VERSION,
    TRANSFER_PROVIDER_INVOCATION_VERSION,
    TransferAdmission,
    TransferAdmissionConflictError,
    TransferAdmissionProjectionError,
    TransferAdmissionRepository,
    TransferCallback,
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
    TransferQueue,
    TransferTask,
)
from app.application.transfer.notifications import (
    TransferFailureNotificationAggregator,
    build_transfer_failure_group_key,
)
from app.schemas.file import FileItem
from app.schemas.transfer import TransferJob

__all__ = [
    "DirectorySize",
    "JobManager",
    "configure_directory_size",
    "job_lock",
    "TRANSFER_ADMISSION_ACCEPTED",
    "TRANSFER_ADMISSION_PLANNED",
    "TRANSFER_ADMISSION_PROVIDER_PENDING",
    "TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION",
    "TRANSFER_PLAN_CHECKPOINT_VERSION",
    "TRANSFER_PLANNING_INPUT_VERSION",
    "TRANSFER_PROVIDER_INVOCATION_VERSION",
    "TransferAdmission",
    "TransferAdmissionConflictError",
    "TransferAdmissionProjectionError",
    "TransferAdmissionRepository",
    "TransferCallback",
    "TransferFailureNotification",
    "TransferFailureNotificationAggregator",
    "TransferLeaseLostError",
    "TransferPlanCheckpoint",
    "TransferPlanItem",
    "TransferPlanningInput",
    "TransferPlanningStateError",
    "TransferProviderInvocationSnapshot",
    "TransferProviderReference",
    "TransferQueue",
    "TransferTask",
    "build_transfer_failure_group_key",
    "TransferQueueService",
]


class TransferQueueService:
    """协调整理任务登记、入队、移除和队列视图查询。"""

    def __init__(
            self,
            *,
            register_task: Callable[[TransferTask], bool],
            admit_task: Callable[[TransferTask], TransferAdmission],
            enqueue: Callable[[TransferQueue], None],
            before_enqueue: Callable[[TransferTask], None],
            enqueue_failed: Callable[[TransferTask, Exception], None],
            remove_task: Callable[[FileItem], None],
            list_tasks: Callable[[], List[TransferJob]],
            expire_tasks: Callable[[], None],
    ) -> None:
        """保存队列用例依赖，避免 Application 服务绑定具体线程队列实现。"""
        self._register_task = register_task
        self._admit_task = admit_task
        self._enqueue = enqueue
        self._before_enqueue = before_enqueue
        self._enqueue_failed = enqueue_failed
        self._remove_task = remove_task
        self._list_tasks = list_tasks
        self._expire_tasks = expire_tasks

    def put(self, task: TransferTask, callback: TransferCallback) -> bool:
        """先持久化准入事实再入队；任何前置失败都撤销内存作业视图。"""
        if not task or not self._register_task(task):
            return False
        try:
            admission = self._admit_task(task)
            task.bind_admission_task_id(admission.task_id)
        except Exception:
            self._remove_task(task.fileitem)
            raise
        try:
            self._before_enqueue(task)
            self._enqueue(TransferQueue(task=task, callback=callback))
        except Exception as err:
            try:
                self._enqueue_failed(task, err)
            finally:
                self._remove_task(task.fileitem)
            raise
        return True

    def remove(self, fileitem: FileItem) -> None:
        """从整理任务视图移除指定文件。"""
        if fileitem:
            self._remove_task(fileitem)

    def list(self) -> List[TransferJob]:
        """先处理失活任务，再返回当前整理作业视图。"""
        self._expire_tasks()
        return self._list_tasks()
