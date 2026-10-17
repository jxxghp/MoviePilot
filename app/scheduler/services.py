"""
调度器由启动组合根注入的业务能力。
"""

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.application.workflow import WorkflowSnapshot
from app.schemas.message import Message

JobCallable = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class SchedulerServices:
    """
    保存任务目录和动态投影所需的已装配业务 callable。

    :param sync_cookies: 同步站点 Cookie 的任务入口
    :param sync_mediaserver: 同步媒体服务器资料的任务入口
    :param check_subscribe: 检查现有订阅状态的任务入口
    :param search_subscribe: 按条件搜索订阅候选的任务入口
    :param resume_subscribe_search: 恢复持久化订阅搜索队列的任务入口
    :param refresh_subscribe: 刷新订阅信息的任务入口
    :param follow_subscribe: 跟进订阅更新的任务入口
    :param process_transfer: 处理待整理文件的任务入口
    :param clear_cache: 清理过期缓存的任务入口
    :param cleanup_data: 清理保留期外数据的任务入口
    :param run_modules: 执行模块定时任务的入口
    :param get_wallpapers: 获取并缓存壁纸的任务入口
    :param refresh_site_data: 刷新站点资料的任务入口
    :param refresh_recommend: 刷新推荐资料的任务入口
    :param cache_subscribe_calendar: 缓存订阅日历的任务入口
    :param list_workflows: 返回当前可执行工作流快照的入口
    :param process_workflow: 执行指定工作流的任务入口
    :param put_message: 将消息放入异步队列的任务入口
    :param post_message: 发送指定消息的任务入口
    :param clear_wallpaper_cache: 可选的壁纸缓存清理入口；None 表示未装配
    """

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
    clear_wallpaper_cache: Callable[[], None] | None = None
