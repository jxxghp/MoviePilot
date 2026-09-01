"""调度器由启动组合根注入的业务能力。"""

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.application.workflow import WorkflowSnapshot
from app.schemas.message import Message

JobCallable = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class SchedulerServices:
    """保存任务目录和动态投影所需的已装配业务 callable。"""

    sync_cookies: JobCallable
    sync_mediaserver: JobCallable
    check_subscribe: JobCallable
    search_subscribe: JobCallable
    resume_subscribe_search: JobCallable
    refresh_subscribe: JobCallable
    follow_subscribe: JobCallable
    process_transfer: JobCallable
    clear_cache: JobCallable
    cleanup_data: JobCallable
    run_modules: JobCallable
    get_wallpapers: JobCallable
    refresh_site_data: JobCallable
    refresh_recommend: JobCallable
    cache_subscribe_calendar: JobCallable
    list_workflows: Callable[[], Iterable[WorkflowSnapshot]]
    process_workflow: JobCallable
    put_message: JobCallable
    post_message: Callable[[Message], Any]
