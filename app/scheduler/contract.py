"""Scheduler 职责 owner 的静态组合宿主合同。"""

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.application.agenttask import AgentTaskRepository
    from app.scheduler.registry import ExecutionRegistry, SchedulerHandle
    from app.scheduler.services import SchedulerServices

    class _SchedulerOwnerHost:
        """声明 Scheduler Facade 提供给各职责 owner 的共享状态与入口。"""

        _agent_task_interruptions_reconciled: bool
        _agent_tasks: AgentTaskRepository | None
        _auth_count: int
        _auth_message: bool
        _auth_plugin_routes_pending: bool
        _event: Any
        _jobs: dict[str, dict[str, Any]]
        _lifecycle_state: str
        _lock: Any
        _registry: ExecutionRegistry
        _scheduler: Any
        _services: SchedulerServices | None

        _accepting_submissions: Callable[..., bool]
        _accepts_handle: Callable[..., bool]
        _assign_job_generation: Callable[..., None]
        _await_cancelled_handles: Callable[..., Any]
        _await_progress_handles: Callable[..., Any]
        _build_mediaserver_sync_schedules: Callable[..., Any]
        _build_progress_callback: Callable[..., Any]
        _cancel_handle: Callable[[SchedulerHandle], None]
        _finish_job: Callable[..., Any]
        _finish_unsubmitted_job: Callable[..., None]
        _format_time: Callable[..., str]
        _get_result_error: Callable[..., Any]
        _get_progress_key: Callable[[str], str]
        _handle_job_error: Callable[..., None]
        _initialize_catalog: Callable[..., None]
        _is_job_active: Callable[[str], bool]
        _register_handle: Callable[..., bool]
        _release_job_generation: Callable[..., None]
        _scheduler_services: Callable[[], SchedulerServices]
        _shutdown_scheduler_sync: Callable[[Any], None]
        _supports_progress_callback: Callable[..., bool]
        _submit_cross_thread: Callable[..., bool]
        _submit_to_loop: Callable[..., bool]
        _reconcile_agent_task_interruptions: Callable[..., None]
        aget_progress: Callable[..., Any]
        agent_heartbeat: Callable[..., Any]
        clear_cache: Callable[..., Any]
        database_backup: Callable[..., Any]
        execute_agent_task: Callable[..., Any]
        full_gc: Callable[..., Any]
        get_progress: Callable[..., Any]
        init_agent_task_jobs: Callable[..., None]
        init_plugin_jobs: Callable[..., None]
        init_workflow_jobs: Callable[..., None]
        list: Callable[..., Any]
        remove_agent_task_job: Callable[..., None]
        remove_plugin_job: Callable[..., None]
        remove_workflow_job: Callable[..., None]
        start: Callable[..., bool]
        stop: Callable[..., None]
        update_agent_task_job: Callable[..., Any]
        update_plugin_job: Callable[..., None]
        update_workflow_job: Callable[..., None]
        user_auth: Callable[..., Any]

    class _SchedulerOwnerBase(_SchedulerOwnerHost):
        """仅向静态检查器暴露完整的 Scheduler 组合宿主合同。"""
else:
    _SchedulerOwnerBase = object
