"""站点与插件资源 provider fan-out owner。"""

import asyncio
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Iterator, List, Optional

from app.application.configuration import get_configured_system_config
from app.application.site.observation import (
    capture_site_search_observation,
    report_site_search_outcome,
)
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.subscription.sitebudget import (
    SubscriptionSiteBudget,
    SubscriptionSiteBudgetDeferral,
    SubscriptionSiteBudgetUnavailable,
)
from app.chain.search.contract import _SearchOwnerBase
from app.domain.context import MediaInfo, SubtitleInfo, TorrentInfo
from app.runtime.log import logger
from app.runtime.progress import AsyncProgressHelper, ProgressHelper
from app.runtime.stop import runtime_stop_state
from app.runtime.thread import ThreadHelper
from app.schemas.types import MediaType, ProgressKey, SystemConfigKey

SiteIndexer = Dict[str, Any]
SyncPending = dict[Future[Any], tuple[SiteIndexer, int, int]]

_site_request_schedule_lock = threading.Lock()
_site_next_request_at: Dict[str, float] = {}


def _site_request_interval(site: SiteIndexer) -> float:
    """读取站点管理中已有的单次访问间隔配置。"""
    try:
        return max(0.0, float(site.get("limit_seconds") or 0))
    except (TypeError, ValueError):
        return 0.0


def _reserve_site_request(site: SiteIndexer) -> float:
    """按站点原子预约请求起始时间，避免并发搜索绕过同一流控窗口。"""
    interval = _site_request_interval(site)
    if interval <= 0:
        return 0.0
    site_key = str(site.get("id") or site.get("domain") or site.get("url") or site.get("name"))
    now = time.monotonic()
    with _site_request_schedule_lock:
        request_at = max(now, _site_next_request_at.get(site_key, now))
        _site_next_request_at[site_key] = request_at + interval
    return max(0.0, request_at - now)


def _wait_for_site_request(site: SiteIndexer) -> None:
    """同步等待当前站点预约时间，不阻塞其他站点的线程任务。"""
    delay = _reserve_site_request(site)
    if delay > 0:
        logger.debug(f"{site.get('name')} 站点流控等待 {delay:.1f} 秒 ...")
        time.sleep(delay)


async def _async_wait_for_site_request(site: SiteIndexer) -> None:
    """异步等待当前站点预约时间，让其他站点继续并发搜索。"""
    delay = _reserve_site_request(site)
    if delay > 0:
        logger.debug(f"{site.get('name')} 站点流控等待 {delay:.1f} 秒 ...")
        await asyncio.sleep(delay)


def _search_site_page(
    owner: "_SearchProviderSyncOwner",
    *,
    site: SiteIndexer,
    keyword: str,
    media_type: Optional[MediaType],
    page: int,
) -> List[TorrentInfo]:
    """等待站点预约后执行一页同步搜索，避免增加 owner 的复杂度。"""
    _wait_for_site_request(site)
    return owner.search_site_torrents(
        site=site,
        keyword=keyword,
        mtype=media_type,
        page=page,
    )


@dataclass(frozen=True)
class ProviderBatch:
    """一次 provider 页完成后发布的统一事实。"""

    site: SiteIndexer
    page: int
    items: tuple[Any, ...]
    finished: int
    total: int
    total_items: int
    continued: bool


class _SearchProviderSyncOwner(_SearchOwnerBase):
    """同步站点 provider 选择、预算与 fan-out owner。"""

    @staticmethod
    def _selected_site_ids(sites: Optional[List[int]]) -> List[int]:
        """返回调用方指定或系统配置的站点 ID。"""
        if sites:
            return list(sites)
        return list(get_configured_system_config().get(SystemConfigKey.IndexerSites) or [])

    @staticmethod
    def _select_indexers(
        indexers: List[SiteIndexer],
        selected_sites: List[int],
        *,
        subtitles: bool,
    ) -> List[SiteIndexer]:
        """按启用 ID 和能力筛选 indexer，保持目录顺序。"""
        return [
            indexer
            for indexer in indexers
            if (not subtitles or indexer.get("subtitles"))
            and (not selected_sites or indexer.get("id") in selected_sites)
        ]

    def _sync_indexers(
        self,
        sites: Optional[List[int]],
        *,
        subtitles: bool = False,
    ) -> List[SiteIndexer]:
        """从同步站点目录构造本次 provider 集合。"""
        return self._select_indexers(
            SitesHelper().get_indexers(),
            self._selected_site_ids(sites),
            subtitles=subtitles,
        )

    async def _async_indexers(
        self,
        sites: Optional[List[int]],
        *,
        subtitles: bool = False,
    ) -> List[SiteIndexer]:
        """从异步站点目录构造本次 provider 集合。"""
        return self._select_indexers(
            await SitesHelper().async_get_indexers(),
            self._selected_site_ids(sites),
            subtitles=subtitles,
        )

    @staticmethod
    def _torrent_keyword(
        keyword: Optional[str],
        mediainfo: Optional[MediaInfo],
        area: Optional[str],
    ) -> str:
        """投影站点实际请求关键字，缺少 IMDb ID 时回退媒体标题。"""
        if area == "imdbid" and mediainfo:
            return mediainfo.imdb_id or keyword or mediainfo.title or ""
        return keyword or ""

    @staticmethod
    def _torrent_type(
        mediainfo: Optional[MediaInfo],
        mtype: Optional[MediaType],
    ) -> Optional[MediaType]:
        """返回媒体详情优先的 provider 分类。"""
        return mediainfo.type if mediainfo else mtype

    def _submit_sync_site_page(
        self,
        *,
        pending: SyncPending,
        search_pages: List[int],
        site: SiteIndexer,
        page_index: int,
        search_keyword: str,
        media_type: Optional[MediaType],
    ) -> None:
        """向进程共享线程 owner 提交一页，并登记该站点的续页位置。"""
        page_number = search_pages[page_index]
        future = ThreadHelper().submit(
            self._search_site_torrents_with_budget,
            site=site,
            keyword=search_keyword,
            mtype=media_type,
            page=page_number,
        )
        pending[future] = (site, page_index, page_number)

    def _search_site_torrents_with_budget(
        self,
        *,
        site: SiteIndexer,
        keyword: str,
        mtype: Optional[MediaType],
        page: int,
    ) -> List[TorrentInfo]:
        """在订阅专属站点预算内执行一页同步搜索。"""
        budget = getattr(self, "_subscription_site_budget", None)
        site_id = site.get("id")
        if not isinstance(budget, SubscriptionSiteBudget) or not isinstance(site_id, int):
            return _search_site_page(
                self,
                site=site,
                keyword=keyword,
                media_type=mtype,
                page=page,
            )
        try:
            claim = budget.acquire(site_id)
        except SubscriptionSiteBudgetUnavailable as error:
            self.record_subscription_site_budget_deferred(
                SubscriptionSiteBudgetDeferral(
                    site_id=error.site_id,
                    retry_at=error.retry_at,
                )
            )
            logger.debug(str(error))
            return []
        with capture_site_search_observation() as observation:
            try:
                result = _search_site_page(
                    self,
                    site=site,
                    keyword=keyword,
                    media_type=mtype,
                    page=page,
                )
            except Exception as error:
                budget.record_request(site_id, 0)
                report_site_search_outcome(
                    attempted=True,
                    outcome="error",
                    error=str(error),
                )
                raise
            else:
                budget.record_request(site_id, len(result or []))
                if observation.attempted and observation.outcome not in {"success", "skipped"}:
                    failure = observation.error or observation.outcome
                    self.record_subscription_site_budget_failure(
                        f"站点 {site.get('name') or site_id} 搜索失败：{failure}"
                    )
                return result
            finally:
                try:
                    released = budget.finish(claim, observation)
                    if not released:
                        logger.error(
                            f"订阅站点预算释放失败: site_id={site_id} "
                            f"site={site.get('name') or '-'}"
                        )
                except Exception as error:  # noqa: BLE001
                    budget.record_release_failure()
                    logger.error(
                        f"站点 {site.get('name') or site_id} 搜索预算收口失败：{str(error)}",
                        exc_info=True,
                    )

    def _submit_next_sync_site(
        self,
        *,
        queued_sites: Iterator[SiteIndexer],
        pending: SyncPending,
        search_pages: List[int],
        search_keyword: str,
        media_type: Optional[MediaType],
    ) -> bool:
        """提交下一个尚未开始的站点，返回是否成功提交。"""
        try:
            site = next(queued_sites)
        except StopIteration:
            return False
        SearchProviderOwner._submit_sync_site_page(
            self,
            pending=pending,
            search_pages=search_pages,
            site=site,
            page_index=0,
            search_keyword=search_keyword,
            media_type=media_type,
        )
        return True

    def _collect_sync_site_results(
        self,
        *,
        keyword: str,
        indexer_sites: List[SiteIndexer],
        search_pages: List[int],
        search_keyword: str,
        media_type: Optional[MediaType],
        results: List[TorrentInfo],
        progress: ProgressHelper,
    ) -> None:
        """在共享线程池中按站点串行翻页，并把结果合并到调用方集合。"""
        total_num = len(indexer_sites) * len(search_pages)
        pending: SyncPending = {}
        queued_sites = iter(indexer_sites)
        max_workers = min(
            len(indexer_sites),
            self.runtime_config.search_threadpool_size or len(indexer_sites),
        )
        finish_count = 0

        for _ in range(max_workers):
            SearchProviderOwner._submit_next_sync_site(
                self,
                queued_sites=queued_sites,
                pending=pending,
                search_pages=search_pages,
                search_keyword=search_keyword,
                media_type=media_type,
            )

        try:
            while pending and not runtime_stop_state.is_system_stopped:
                done, _ = wait(
                    pending,
                    timeout=0.2,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    site, page_index, page_number = pending.pop(future)
                    page_results = future.result() or []
                    finish_count += 1
                    results.extend(page_results)
                    continued = self._should_continue_search_pages(
                        site=site,
                        page_results=page_results,
                        keyword=search_keyword,
                    ) and page_index + 1 < len(search_pages)
                    if continued:
                        SearchProviderOwner._submit_sync_site_page(
                            self,
                            pending=pending,
                            search_pages=search_pages,
                            site=site,
                            page_index=page_index + 1,
                            search_keyword=search_keyword,
                            media_type=media_type,
                        )
                    else:
                        logger.debug(f"{site.get('name')} 第 {page_number} 页返回 {len(page_results)} 条，停止继续翻页")
                        SearchProviderOwner._submit_next_sync_site(
                            self,
                            queued_sites=queued_sites,
                            pending=pending,
                            search_pages=search_pages,
                            search_keyword=search_keyword,
                            media_type=media_type,
                        )
                    logger.debug(f"站点搜索进度：{finish_count} / {total_num}")
                    progress.update(
                        value=finish_count / total_num * 100,
                        text=(f"正在搜索{keyword or ''}，已完成 {finish_count} / {total_num} 个请求 ..."),
                    )
        finally:
            for future in pending:
                future.cancel()

    def _search_all_sites(
        self,
        keyword: str,
        mediainfo: Optional[MediaInfo] = None,
        sites: Optional[List[int]] = None,
        page: Optional[int] = 0,
        area: Optional[str] = "title",
        mtype: Optional[MediaType] = None,
    ) -> Optional[List[TorrentInfo]]:
        """通过共享线程 owner 按站点顺序翻页并汇总同步 provider 结果。"""
        indexer_sites = self._sync_indexers(sites)
        media_type = self._torrent_type(mediainfo, mtype)
        search_keyword = self._torrent_keyword(keyword, mediainfo, area)
        plugin_results = self.search_plugin_torrents(
            keyword=search_keyword,
            mtype=media_type,
            page=page,
        )
        if not indexer_sites:
            logger.info(f"未开启有效站点，插件资源源返回 {len(plugin_results)} 条资源")
            return plugin_results

        progress = ProgressHelper(ProgressKey.Search)
        start_time = datetime.now()
        search_pages = self._build_search_pages(page)
        results = list(plugin_results)

        progress.start()
        try:
            progress.update(
                value=0,
                text=(f"开始搜索，共 {len(indexer_sites)} 个站点，{len(search_pages)} 页 ..."),
            )
            SearchProviderOwner._collect_sync_site_results(
                self,
                keyword=keyword,
                indexer_sites=indexer_sites,
                search_pages=search_pages,
                search_keyword=search_keyword,
                media_type=media_type,
                results=results,
                progress=progress,
            )

            elapsed = (datetime.now() - start_time).seconds
            progress.update(
                value=100,
                text=(f"站点搜索完成，有效资源数：{len(results)}，总耗时 {elapsed} 秒"),
            )
            log = (
                logger.debug
                if isinstance(getattr(self, "_subscription_site_budget", None), SubscriptionSiteBudget)
                else logger.info
            )
            log(f"站点搜索完成，有效资源数：{len(results)}，总耗时 {elapsed} 秒")
            return results
        finally:
            progress.end()


class SearchProviderOwner(_SearchProviderSyncOwner):
    """异步站点与插件资源 provider fan-out owner。"""

    async def _iter_provider_batches(
        self,
        *,
        indexer_sites: List[SiteIndexer],
        search_pages: List[int],
        initial_count: int,
        search_page: Callable[[SiteIndexer, int], Awaitable[Optional[List[Any]]]],
        should_continue: Callable[[SiteIndexer, List[Any]], bool],
        task_owner: str,
    ) -> AsyncGenerator[ProviderBatch, None]:
        """把站点逐页结果投影为唯一 batch 事实流。"""
        total = len(indexer_sites) * len(search_pages)
        total_items = initial_count
        if not indexer_sites:
            return

        finish_count = 0
        page_iterator = self._iter_site_page_results(
            indexer_sites=indexer_sites,
            search_pages=search_pages,
            search_page=search_page,
            should_continue=should_continue,
            task_owner=task_owner,
        )
        async with aclosing(page_iterator):
            async for site, page_number, page_results, continued in page_iterator:
                finish_count += 1
                total_items += len(page_results)
                yield ProviderBatch(
                    site=site,
                    page=page_number,
                    items=tuple(page_results),
                    finished=finish_count,
                    total=total,
                    total_items=total_items,
                    continued=continued,
                )

    @staticmethod
    def _plugin_event(
        *,
        initial_items: List[Any],
        initial_page: Optional[int],
        total: int,
        has_indexers: bool,
    ) -> Dict[str, Any]:
        """构造保持既有顺序和字段语义的插件资源事件。"""
        return {
            "type": "append",
            "stage": "searching",
            "value": 0 if has_indexers else 100,
            "text": f"插件资源源返回 {len(initial_items)} 条资源",
            "items": list(initial_items),
            "site": "插件资源源",
            "site_id": None,
            "page": initial_page,
            "finished": 0,
            "total": total,
            "total_items": len(initial_items),
        }

    @staticmethod
    def _done_event(*, text: str, total_items: int) -> Dict[str, Any]:
        """构造没有站点请求时的稳定完成事件。"""
        return {
            "type": "done",
            "stage": "searching",
            "value": 100,
            "text": text,
            "items": [],
            "finished": 0,
            "total": 0,
            "total_items": total_items,
        }

    @staticmethod
    def _events_without_indexers(
        *,
        initial_items: List[Any],
        initial_page: Optional[int],
        subtitle: bool,
    ) -> List[Dict[str, Any]]:
        """返回无站点场景的完整事件序列，保持插件先于完成事件。"""
        if subtitle and not initial_items:
            text = "未开启任何支持字幕搜索的有效站点，无法搜索字幕"
            logger.warning(text)
        else:
            text = f"搜索完成，共 {len(initial_items)} 条资源"
            logger.info(f"未开启有效站点，插件资源源返回 {len(initial_items)} 条资源")

        events = []
        if initial_items:
            events.append(
                SearchProviderOwner._plugin_event(
                    initial_items=initial_items,
                    initial_page=initial_page,
                    total=0,
                    has_indexers=False,
                )
            )
        events.append(
            SearchProviderOwner._done_event(
                text=text,
                total_items=len(initial_items),
            )
        )
        return events

    @staticmethod
    def _progress_event(
        *,
        text: str,
        total: int,
        total_items: int,
    ) -> Dict[str, Any]:
        """构造站点请求开始前的进度事件。"""
        return {
            "type": "progress",
            "stage": "searching",
            "value": 0,
            "text": text,
            "items": [],
            "finished": 0,
            "total": total,
            "total_items": total_items,
        }

    @staticmethod
    async def _batch_event(
        *,
        batch: ProviderBatch,
        keyword: str,
        label: str,
        progress: AsyncProgressHelper,
    ) -> Dict[str, Any]:
        """更新异步进度并把单页 batch 投影为追加事件。"""
        if not batch.continued:
            logger.debug(f"{batch.site.get('name')} {label}第 {batch.page} 页返回 {len(batch.items)} 条，停止继续翻页")
        progress_value = batch.finished / batch.total * 100
        progress_text = f"正在搜索{label}{keyword or ''}，已完成 {batch.finished} / {batch.total} 个请求 ..."
        logger.debug(f"站点{label}搜索进度：{batch.finished} / {batch.total}")
        await progress.update(value=progress_value, text=progress_text)
        return {
            "type": "append",
            "stage": "searching",
            "value": progress_value,
            "text": progress_text,
            "items": list(batch.items),
            "site": batch.site.get("name"),
            "site_id": batch.site.get("id"),
            "page": batch.page,
            "finished": batch.finished,
            "total": batch.total,
            "total_items": batch.total_items,
        }

    async def _iter_provider_events(
        self,
        *,
        keyword: str,
        indexer_sites: List[SiteIndexer],
        search_pages: List[int],
        initial_items: List[Any],
        initial_page: Optional[int],
        search_page: Callable[[SiteIndexer, int], Awaitable[Optional[List[Any]]]],
        should_continue: Callable[[SiteIndexer, List[Any]], bool],
        task_owner: str,
        subtitle: bool,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """统一管理 provider 进度生命周期，并把 batch 投影为流式事件。"""
        total = len(indexer_sites) * len(search_pages)
        label = "字幕" if subtitle else ""
        if not indexer_sites:
            for event in SearchProviderOwner._events_without_indexers(
                initial_items=initial_items,
                initial_page=initial_page,
                subtitle=subtitle,
            ):
                yield event
            return

        started_at = datetime.now()
        last_total_items = len(initial_items)
        # 既有流式协议约定插件资源先于站点进度发布；列表入口也消费同一事件流。
        if initial_items:
            yield SearchProviderOwner._plugin_event(
                initial_items=initial_items,
                initial_page=initial_page,
                total=total,
                has_indexers=True,
            )

        progress = AsyncProgressHelper(ProgressKey.Search)
        try:
            await progress.start()
            start_text = f"开始搜索{label}，共 {len(indexer_sites)} 个站点，{len(search_pages)} 页 ..."
            await progress.update(value=0, text=start_text)
            yield SearchProviderOwner._progress_event(
                text=start_text,
                total=total,
                total_items=last_total_items,
            )

            batches = self._iter_provider_batches(
                indexer_sites=indexer_sites,
                search_pages=search_pages,
                initial_count=last_total_items,
                search_page=search_page,
                should_continue=should_continue,
                task_owner=task_owner,
            )
            async with aclosing(batches):
                async for batch in batches:
                    last_total_items = batch.total_items
                    yield await SearchProviderOwner._batch_event(
                        batch=batch,
                        keyword=keyword,
                        label=label,
                        progress=progress,
                    )

            elapsed = (datetime.now() - started_at).seconds
            done_text = (
                f"站点{label}搜索完成，有效{'字幕' if subtitle else '资源'}数：{last_total_items}，总耗时 {elapsed} 秒"
            )
            await progress.update(value=100, text=done_text)
            logger.info(done_text)
        finally:
            await progress.end()

    async def _iter_torrent_events(
        self,
        *,
        keyword: str,
        mediainfo: Optional[MediaInfo],
        sites: Optional[List[int]],
        page: Optional[int],
        area: Optional[str],
        mtype: Optional[MediaType],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """构造种子 provider 的 canonical 事件流。"""
        indexer_sites = await self._async_indexers(sites)
        media_type = self._torrent_type(mediainfo, mtype)
        search_keyword = self._torrent_keyword(keyword, mediainfo, area)
        plugin_results = await self.async_search_plugin_torrents(
            keyword=search_keyword,
            mtype=media_type,
            page=page,
        )
        search_pages = self._build_search_pages(page)

        async def search_site_page(
            site: SiteIndexer,
            page_number: int,
        ) -> List[TorrentInfo]:
            """在统一分页器调度下请求一页站点资源。"""
            await _async_wait_for_site_request(site)
            return await self.async_search_site_torrents(
                site=site,
                keyword=search_keyword,
                mtype=media_type,
                page=page_number,
            )

        def should_continue(site: SiteIndexer, page_results: List[Any]) -> bool:
            """按站点实际页容量决定是否请求下一页。"""
            return bool(
                self._should_continue_search_pages(
                    site=site,
                    page_results=page_results,
                    keyword=search_keyword,
                )
            )

        events = self._iter_provider_events(
            keyword=keyword,
            indexer_sites=indexer_sites,
            search_pages=search_pages,
            initial_items=list(plugin_results),
            initial_page=page,
            search_page=search_site_page,
            should_continue=should_continue,
            task_owner="chain.search.media.site_page",
            subtitle=False,
        )
        async with aclosing(events):
            async for event in events:
                yield event

    async def _async_search_all_sites(
        self,
        keyword: str,
        mediainfo: Optional[MediaInfo] = None,
        sites: Optional[List[int]] = None,
        page: Optional[int] = 0,
        area: Optional[str] = "title",
        mtype: Optional[MediaType] = None,
    ) -> Optional[List[TorrentInfo]]:
        """收集 canonical provider 事件流并返回完整资源列表。"""
        results: List[TorrentInfo] = []
        events = self._iter_torrent_events(
            keyword=keyword,
            mediainfo=mediainfo,
            sites=sites,
            page=page,
            area=area,
            mtype=mtype,
        )
        async with aclosing(events):
            async for event in events:
                if event.get("type") == "append":
                    results.extend(event.get("items") or [])
        return results

    async def _async_search_all_sites_stream(
        self,
        keyword: str,
        mediainfo: Optional[MediaInfo] = None,
        sites: Optional[List[int]] = None,
        page: Optional[int] = 0,
        area: Optional[str] = "title",
        mtype: Optional[MediaType] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """直接转发 canonical provider 事件流。"""
        events = self._iter_torrent_events(
            keyword=keyword,
            mediainfo=mediainfo,
            sites=sites,
            page=page,
            area=area,
            mtype=mtype,
        )
        async with aclosing(events):
            async for event in events:
                yield event

    async def _iter_subtitle_events(
        self,
        *,
        keyword: str,
        sites: Optional[List[int]],
        page: Optional[int],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """构造字幕 provider 的 canonical 事件流。"""
        indexer_sites = await self._async_indexers(sites, subtitles=True)
        search_pages = self._build_search_pages(page)

        async def search_site_page(
            site: SiteIndexer,
            page_number: int,
        ) -> List[SubtitleInfo]:
            """在统一分页器调度下请求一页字幕资源。"""
            return await self.async_search_subtitles(
                site=site,
                keyword=keyword,
                page=page_number,
            )

        def should_continue(site: SiteIndexer, page_results: List[Any]) -> bool:
            """按字幕站点页容量决定是否请求下一页。"""
            return bool(
                self._should_continue_subtitle_search_pages(
                    site=site,
                    page_results=page_results,
                )
            )

        events = self._iter_provider_events(
            keyword=keyword,
            indexer_sites=indexer_sites,
            search_pages=search_pages,
            initial_items=[],
            initial_page=None,
            search_page=search_site_page,
            should_continue=should_continue,
            task_owner="chain.search.subtitle.site_page",
            subtitle=True,
        )
        async with aclosing(events):
            async for event in events:
                yield event

    async def _async_search_subtitles_all_sites(
        self,
        keyword: str,
        sites: Optional[List[int]] = None,
        page: Optional[int] = 0,
    ) -> Optional[List[SubtitleInfo]]:
        """收集 canonical 字幕事件流并返回完整字幕列表。"""
        results: List[SubtitleInfo] = []
        events = self._iter_subtitle_events(
            keyword=keyword,
            sites=sites,
            page=page,
        )
        async with aclosing(events):
            async for event in events:
                if event.get("type") == "append":
                    results.extend(event.get("items") or [])
        return results

    async def _async_search_subtitles_all_sites_stream(
        self,
        keyword: str,
        sites: Optional[List[int]] = None,
        page: Optional[int] = 0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """直接转发 canonical 字幕 provider 事件流。"""
        events = self._iter_subtitle_events(
            keyword=keyword,
            sites=sites,
            page=page,
        )
        async with aclosing(events):
            async for event in events:
                yield event
