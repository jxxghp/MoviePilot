"""搜索分页计划与异步逐页调度 owner。"""

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
)
from app.chain.search.contract import _SearchOwnerBase
from app.runtime.stop import runtime_stop_state
from app.runtime.tasks import get_task_registry

PageResult = Tuple[List[Any], Optional[Exception]]
PageTask = asyncio.Task[PageResult]
SiteIndexer = Dict[str, Any]
PendingPages = dict[PageTask, Tuple[SiteIndexer, int, int]]


async def _run_site_page(
    semaphore: asyncio.Semaphore,
    search_page: Callable[[SiteIndexer, int], Awaitable[Optional[List[Any]]]],
    site: SiteIndexer,
    page_number: int,
) -> PageResult:
    """执行一页请求，并把业务异常交回编排层统一传播。"""
    async with semaphore:
        try:
            return await search_page(site, page_number) or [], None
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            # TaskRegistry 会报告未收口的后台异常；这里由等待方同步传播，
            # 避免同一 provider 故障同时被登记器和调用链重复记录。
            return [], error


def _submit_site_page(
    *,
    pending_tasks: PendingPages,
    search_pages: List[int],
    site: SiteIndexer,
    page_index: int,
    semaphore: asyncio.Semaphore,
    search_page: Callable[[SiteIndexer, int], Awaitable[Optional[List[Any]]]],
    task_owner: str,
) -> None:
    """登记一页请求及其续页位置，供统一终态收口。"""
    page_number = search_pages[page_index]
    task = get_task_registry().create(
        _run_site_page(semaphore, search_page, site, page_number),
        owner="chain.search.site_page",
    )
    # Registry owner 必须是静态责任域；任务名保留媒体/字幕调用方诊断粒度。
    task.set_name(task_owner)
    pending_tasks[task] = (site, page_index, page_number)


async def _cancel_pending_pages(pending_tasks: PendingPages) -> None:
    """取消并等待仍由本次分页会话持有的全部任务。"""
    tasks = tuple(pending_tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class SearchPaginationOwner(_SearchOwnerBase):
    """搜索分页计划与异步逐页调度 owner。"""

    @staticmethod
    def _get_search_resource_pages() -> int:
        """
        获取搜索资源需要抓取的页数。

        settings 可能被环境变量写成字符串，这里统一兜底为 1，避免异常配置导致搜索中断。
        """
        pages = get_chain_runtime_config_snapshot().search_resource_pages
        try:
            pages = int(pages)
        except (TypeError, ValueError):
            return 1
        return max(pages, 1)

    @classmethod
    def _build_search_pages(cls, page: Optional[int] = 0) -> List[int]:
        """
        根据起始页和配置页数生成需要请求的页码列表。
        """
        try:
            start_page = int(page or 0)
        except (TypeError, ValueError):
            start_page = 0
        start_page = max(start_page, 0)
        return list(range(start_page, start_page + cls._get_search_resource_pages()))

    def _should_continue_search_pages(
        self,
        site: SiteIndexer,
        page_results: Optional[List[Any]],
        keyword: Optional[str] = None,
    ) -> bool:
        """
        判断是否继续抓取下一页；少于站点单页容量时视为当前站点已到末页。
        """
        page_size = self.get_search_page_size(site=site, keyword=keyword)
        return page_size is not None and len(page_results or []) >= page_size

    @staticmethod
    def _should_continue_subtitle_search_pages(site: SiteIndexer, page_results: Optional[List[Any]]) -> bool:
        """
        判断字幕搜索是否继续抓取下一页。
        """
        subtitle_conf = (site or {}).get("subtitles") or {}
        try:
            page_size = int(subtitle_conf.get("result_num") or site.get("result_num") or 100)
        except (TypeError, ValueError):
            page_size = 100
        return page_size > 0 and len(page_results or []) >= page_size

    async def _iter_site_page_results(
        self,
        *,
        indexer_sites: List[SiteIndexer],
        search_pages: List[int],
        search_page: Callable[[SiteIndexer, int], Awaitable[Optional[List[Any]]]],
        should_continue: Callable[[SiteIndexer, List[Any]], bool],
        task_owner: str,
    ) -> AsyncIterator[Tuple[SiteIndexer, int, List[Any], bool]]:
        """统一调度站点逐页请求，并在调用方退出时取消、等待全部请求。"""
        total_num = len(indexer_sites) * len(search_pages)
        semaphore = asyncio.Semaphore(self.runtime_config.search_threadpool_size or max(1, total_num))
        pending_tasks: PendingPages = {}

        for site in indexer_sites:
            _submit_site_page(
                pending_tasks=pending_tasks,
                search_pages=search_pages,
                site=site,
                page_index=0,
                semaphore=semaphore,
                search_page=search_page,
                task_owner=task_owner,
            )

        try:
            while pending_tasks:
                if runtime_stop_state.is_system_stopped:
                    break
                done_tasks, _ = await asyncio.wait(
                    pending_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done_tasks:
                    site, page_index, page_number = pending_tasks.pop(task)
                    page_results, error = await task
                    if error is not None:
                        raise error
                    continued = should_continue(site, page_results) and page_index + 1 < len(search_pages)
                    if continued:
                        _submit_site_page(
                            pending_tasks=pending_tasks,
                            search_pages=search_pages,
                            site=site,
                            page_index=page_index + 1,
                            semaphore=semaphore,
                            search_page=search_page,
                            task_owner=task_owner,
                        )
                    yield site, page_number, page_results, continued
        finally:
            await _cancel_pending_pages(pending_tasks)
