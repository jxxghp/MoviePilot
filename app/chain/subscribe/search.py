"""订阅主动搜索编排"""

import random
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Callable, Optional, cast
from uuid import uuid4

from app.application.configuration import get_configured_system_config
from app.application.subscription.contract import (
    SubscriptionRepository,
    SubscriptionSnapshot,
    build_subscribe_meta,
    subscribe_media_key,
)
from app.application.subscription.execution import (
    SearchBatchSnapshot,
    SearchTaskSnapshot,
    SubscriptionExecutionContext,
    SubscriptionSearchRepository,
    raise_subscription_site_budget_deferral,
    raise_subscription_site_budget_failures,
)
from app.application.subscription.observability import (
    SearchExecutionSummary,
    SearchTaskOutcome,
    batch_finished_count,
    batch_progress_text,
    inline_search_result,
)
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.sitebudget import (
    SubscriptionSearchCancelled,
    SubscriptionSearchDeferred,
)
from app.chain.media import MediaChain
from app.chain.search.facade import SearchChain
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.identity import subscribe_recognize_kwargs
from app.chain.subscribe.metadata import apply_subscription_classification
from app.chain.subscribe.searchtask import (
    SubscriptionSearchTaskRunner,
    retry_at_after,
)
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.types import (
    MediaType,
    SystemConfigKey,
)

_NEW_SUBSCRIPTION_EDIT_SECONDS = 60


def _ensure_execution_active(
    execution_context: Optional[SubscriptionExecutionContext],
) -> None:
    """在可安全停止的边界区分用户取消与执行超时。"""
    if execution_context is None:
        return
    if execution_context.is_cancel_requested():
        raise SubscriptionSearchCancelled("搜索已停止")
    if execution_context.is_expired():
        raise TimeoutError("这次搜索用时过长，已停止")


def _search_source_and_priority(
    *,
    sid: Optional[int],
    sids: Optional[tuple[int, ...]],
    state: Optional[str],
    manual: Optional[bool],
) -> tuple[str, int]:
    """把兼容入口归一为持久来源和公平队列优先级。"""
    if manual:
        target_count = 1 if sid else len(sids or ())
        return "manual", 120 if target_count == 1 else 100
    if sid or sids is not None:
        return "targeted", 80
    if state in {"R", "P"}:
        return "fallback", 10
    return "new", 50


def _search_task_available_at(
    source: str,
    subscription_ids: tuple[int, ...],
    *,
    now: Optional[datetime] = None,
) -> dict[int, str]:
    """把兜底搜索的随机节奏持久化为逐订阅到期时间。"""
    ordered_ids = tuple(dict.fromkeys(subscription_ids))
    if not ordered_ids:
        return {}
    cursor = now or datetime.now(timezone.utc)
    if source == "fallback":
        cursor += timedelta(seconds=random.randint(0, 60))
    available_at: dict[int, str] = {}
    for position, subscription_id in enumerate(ordered_ids):
        if source == "fallback" and position:
            cursor += timedelta(seconds=random.randint(60, 300))
        available_at[subscription_id] = cursor.isoformat(timespec="seconds")
    return available_at


class _SubscribeSearchQueueCoordinator(_SubscribeOwnerBase):
    """订阅主动搜索入口、队列提交与消费协调 owner。"""

    def _subscription_query(self) -> SubscriptionQueryService:
        """构造绑定订阅 Oper 的查询应用服务。"""
        return SubscriptionQueryService(self.subscription_repository)

    @classmethod
    def exists(
        cls,
        mediainfo: MediaInfo,
        meta: MetaBase = None,
        repository: Optional[SubscriptionRepository] = None,
    ) -> bool:
        """
        判断订阅是否已存在
        """
        chain = cls()
        if repository is not None:
            chain.subscription_repository = repository
        result: bool = chain._subscription_query().exists(mediainfo, meta)
        return result

    def _acquire_run_lock(
        self,
        operation: str,
        progress_callback: Optional[Callable[..., None]],
    ) -> bool:
        """获取 Search 或 Match 通道锁，保持各通道内部串行。"""
        operation_label = {"search": "搜索", "match": "资源检查"}[operation]
        lock = self._match_lock if operation == "match" else self._search_queue_lock
        if lock.acquire(blocking=True, timeout=self._SUBSCRIPTION_EXECUTION_TTL):
            logger.debug(f"订阅{operation_label}已开始：{datetime.now()}")
            return True
        progress_text = {
            "search": "订阅搜索正在处理中，本次不再重复开始",
            "match": "订阅资源检查正在进行，本次不再重复开始",
        }[operation]
        logger.error(f"订阅{operation_label}等待时间过长，本次不再重复开始")
        if progress_callback:
            progress_callback(
                value=100,
                text=progress_text,
            )
        return False

    def search(
        self,
        sid: Optional[int] = None,
        state: Optional[str] = "N",
        manual: Optional[bool] = False,
        progress_callback: Optional[Callable[..., None]] = None,
        sids: Optional[tuple[int, ...]] = None,
    ) -> Optional[str]:
        """
        执行订阅搜索。

        保持定时任务、API 和插件使用的公开签名，搜索实现委托给内部执行阶段。
        """
        return self._execute_search(
            sid=sid,
            sids=sids,
            state=state,
            manual=manual,
            progress_callback=progress_callback,
        )

    def _execute_search(
        self,
        sid: Optional[int] = None,
        state: Optional[str] = "N",
        manual: Optional[bool] = False,
        progress_callback: Optional[Callable[..., None]] = None,
        sids: Optional[tuple[int, ...]] = None,
    ) -> Optional[str]:
        """
        订阅搜索
        :param sid: 订阅ID，有值时只处理该订阅
        :param state: 订阅状态 N:新建, R:订阅中, P:待定, S:暂停
        :param manual: 是否手动搜索
        :param progress_callback: 定时服务进度更新回调
        :param sids: 订阅ID集合，有值时按给定顺序处理
        :return: 更新订阅状态为R或删除订阅
        """
        queue: Optional[SubscriptionSearchRepository] = getattr(
            self, "subscription_search_repository", None
        )
        if queue is not None:
            return self._execute_queued_search(
                queue=queue,
                sid=sid,
                sids=sids,
                state=state,
                manual=manual,
                progress_callback=progress_callback,
            )
        self._execute_inline_search(
            sid=sid,
            sids=sids,
            state=state,
            manual=manual,
            progress_callback=progress_callback,
        )
        return None

    def _execute_inline_search(
        self,
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        state: Optional[str],
        manual: Optional[bool],
        progress_callback: Optional[Callable[..., None]],
    ) -> None:
        """在独立 Search 通道内按订阅准入执行兼容搜索。"""
        lock_acquired = self._acquire_run_lock("search", progress_callback)
        if not lock_acquired:
            return
        subscribes = []
        processed = []
        summary: Optional[SearchExecutionSummary] = None
        try:
            subscribes = self._load_search_subscriptions(sid=sid, sids=sids, state=state)
            total = len(subscribes)
            source, _priority = _search_source_and_priority(
                sid=sid,
                sids=sids,
                state=state,
                manual=manual,
            )
            summary = SearchExecutionSummary(source="inline", requested=total)
            logger.info(summary.start_log())
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"开始订阅搜索，共 {total} 个订阅 ...",
                    data={"total": total, "finished": 0},
                )
            searchchain = SearchChain()
            for index, subscribe in enumerate(subscribes, start=1):
                if runtime_stop_state.is_system_stopped:
                    summary.stopped = True
                    break
                self._report_search_progress(progress_callback, subscribe, index, total)
                if self._recent_subscription_retry_at(subscribe, source):
                    summary.record("skipped", "recent_subscription")
                    continue
                self._wait_before_scheduled_search(sid, sids, state, progress_callback)
                lease = self._subscription_execution_admission.try_acquire(
                    subscription_id=subscribe.id,
                    operation="search",
                    ttl_seconds=self._SUBSCRIPTION_EXECUTION_TTL,
                )
                if lease is None:
                    logger.debug(f"订阅《{subscribe.name}》正在处理，本次搜索先不重复执行")
                    summary.record("skipped", "admission_conflict")
                    continue
                execution_context = SubscriptionExecutionContext(
                    lease=lease,
                    admission=self._subscription_execution_admission,
                    cancel_requested=lambda: runtime_stop_state.is_system_stopped,
                )
                current = None
                outcome: SearchTaskOutcome = "skipped"
                reason: Optional[str] = "not_eligible"
                try:
                    current = self.subscription_repository.get(subscribe.id)
                    if current is None or current.state == "S":
                        if current and current.state == "S":
                            logger.debug(f"订阅《{current.name}》已暂停，本次没有搜索")
                        continue
                    processed_result = self._process_search_subscription(
                        current,
                        searchchain,
                        execution_context=execution_context,
                    )
                    processed.append(processed_result or current)
                    current = processed_result
                    outcome = "completed"
                    reason = None
                except SubscriptionSearchCancelled:
                    reason = "ttl_timeout" if execution_context.is_expired() else "cancelled"
                    outcome = "failed" if reason == "ttl_timeout" else "cancelled"
                    logger.debug(f"订阅《{subscribe.name}》搜索已停止")
                except SubscriptionSearchDeferred as deferred:
                    outcome = "skipped"
                    reason = "site_budget_deferred"
                    logger.debug(
                        f"订阅《{subscribe.name}》遇到站点繁忙，将在稍后自动重试：{deferred.retry_at}"
                    )
                except Exception as err:
                    outcome = "failed"
                    reason = "error"
                    logger.error(f"订阅 {subscribe.name} 搜索失败：{str(err)}", exc_info=True)
                finally:
                    try:
                        if current and current.state == "N":
                            self._SubscribeChain__apply_subscribe_update(
                                current,
                                {"state": "R"},
                                scene="search_reset",
                            )
                    except Exception as err:
                        logger.error(
                            f"订阅《{subscribe.name}》搜索结束后没有恢复到正常状态，"
                            f"系统稍后会继续处理：{str(err)}",
                            exc_info=True,
                        )
                    finally:
                        released = self._subscription_execution_admission.release(lease)
                        if not released:
                            summary.release_failures += 1
                            logger.error(
                                f"订阅《{subscribe.name}》的搜索状态没有正常恢复，系统稍后会继续处理"
                            )
                        summary.record(outcome, reason)
                        self._report_search_progress(
                            progress_callback,
                            subscribe,
                            index,
                            total,
                            finished=True,
                        )
            self._notify_manual_search(manual, sid, sids, subscribes, processed)
            if progress_callback:
                text, data = inline_search_result(total, len(processed))
                progress_callback(value=100, text=text, data=data)
        finally:
            subscribes.clear()
            self._search_queue_lock.release()
            if summary is not None:
                logger.info(summary.finish_log())
            logger.debug(f"订阅搜索已结束：{datetime.now()}")

    def _execute_queued_search(
        self,
        *,
        queue: SubscriptionSearchRepository,
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        state: Optional[str],
        manual: Optional[bool],
        progress_callback: Optional[Callable[..., None]],
    ) -> str:
        """将搜索转为持久任务并在无 Match 长锁的短租约中串行消费。"""
        subscribes = self._load_search_subscriptions(sid=sid, sids=sids, state=state)
        source, priority = _search_source_and_priority(
            sid=sid,
            sids=sids,
            state=state,
            manual=manual,
        )
        subscription_ids = tuple(subscribe.id for subscribe in subscribes)
        enqueued = queue.enqueue(
            subscription_ids=subscription_ids,
            source=source,
            priority=priority,
            available_at_by_subscription=_search_task_available_at(
                source,
                subscription_ids,
            ),
        )
        total = len(subscribes)
        summary = SearchExecutionSummary(
            source=source,
            requested=total,
            batch_id=str(enqueued.batch.batch_id),
            coalesced=enqueued.coalesced_count,
        )
        logger.info(summary.start_log())
        batch: Optional[SearchBatchSnapshot] = None
        try:
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"开始订阅搜索，共 {total} 个订阅 ...",
                    data={
                        "batch_id": enqueued.batch.batch_id,
                        "total": total,
                        "finished": 0,
                        "coalesced": enqueued.coalesced_count,
                    },
                )
            processed = self._drain_search_queue(
                queue=queue,
                limit=max(1, enqueued.created_count + enqueued.coalesced_count),
                progress_callback=progress_callback,
                summary=summary,
            )
            processed_subscribes = [item for item in subscribes if item.id in processed]
            self._notify_manual_search(manual, sid, sids, subscribes, processed_subscribes)
            batch = queue.get_batch(enqueued.batch.batch_id)
            if progress_callback:
                progress_callback(
                    value=100,
                    text=batch_progress_text(batch),
                    data={
                        "batch_id": enqueued.batch.batch_id,
                        "total": total,
                        "finished": batch_finished_count(batch, len(processed_subscribes)),
                        "coalesced": enqueued.coalesced_count,
                    },
                )
            return str(enqueued.batch.batch_id)
        except Exception:
            summary.round_failed = True
            raise
        finally:
            logger.info(summary.finish_log(batch))

    def _drain_search_queue(
        self,
        *,
        queue: SubscriptionSearchRepository,
        limit: int,
        progress_callback: Optional[Callable[..., None]],
        summary: SearchExecutionSummary,
    ) -> set[int]:
        """有界消费可恢复任务；单任务失败不得阻止后续订阅。"""
        if not self._search_queue_lock.acquire(blocking=False):
            logger.debug("订阅搜索已经在后台进行，本次安排会接着处理")
            summary.consumer_conflicts += 1
            return set()
        owner = f"subscribe-search:{uuid4().hex}"
        processed: set[int] = set()
        try:
            searchchain = SearchChain()
            for index in range(1, limit + 1):
                if runtime_stop_state.is_system_stopped:
                    summary.stopped = True
                    break
                task = queue.claim_next(owner=owner)
                if task is None:
                    break
                subscription_id = self._execute_search_task(
                    queue=queue,
                    task=task,
                    owner=owner,
                    searchchain=searchchain,
                    index=index,
                    limit=limit,
                    progress_callback=progress_callback,
                    summary=summary,
                )
                if subscription_id is not None:
                    processed.add(subscription_id)
        finally:
            self._search_queue_lock.release()
        return processed


class _SubscribeSearchQueueOwner(_SubscribeSearchQueueCoordinator):
    """持久搜索任务执行与批次控制 owner。"""

    def _execute_search_task(
        self,
        *,
        queue: SubscriptionSearchRepository,
        task: SearchTaskSnapshot,
        owner: str,
        searchchain: SearchChain,
        index: int,
        limit: int,
        progress_callback: Optional[Callable[..., None]],
        summary: SearchExecutionSummary,
    ) -> Optional[int]:
        """执行一条已认领任务，返回实际进入搜索处理的订阅 ID。"""
        stop_state = getattr(self, "stop_state", runtime_stop_state)
        runner = SubscriptionSearchTaskRunner(
            queue=queue,
            task=task,
            owner=owner,
            searchchain=searchchain,
            index=index,
            limit=limit,
            progress_callback=progress_callback,
            summary=summary,
            subscription_repository=self.subscription_repository,
            execution_admission=self._subscription_execution_admission,
            execution_ttl=self._SUBSCRIPTION_EXECUTION_TTL,
            recent_retry_at=self._recent_subscription_retry_at,
            process_subscription=self._process_search_subscription,
            reset_subscription=partial(
                self._SubscribeChain__apply_subscribe_update,
                update_data={"state": "R"},
                scene="search_reset",
            ),
            report_progress=self._report_search_progress,
            stop_state=stop_state,
        )
        return runner.execute()

    def resume_search_queue(
        self,
        progress_callback: Optional[Callable[..., None]] = None,
        limit: int = 50,
        manual_sids: Optional[tuple[int, ...]] = None,
    ) -> None:
        """短周期恢复等待任务，并可优先反馈本次手工搜索结果。"""
        queue: Optional[SubscriptionSearchRepository] = getattr(
            self, "subscription_search_repository", None
        )
        if queue is None:
            return
        manual_ids = tuple(dict.fromkeys(manual_sids or ()))
        subscribes = (
            self._load_search_subscriptions(sid=None, sids=manual_ids, state=None)
            if manual_ids
            else []
        )
        summary = SearchExecutionSummary(
            source="manual" if manual_ids else "resume",
            requested=len(manual_ids) if manual_ids else max(1, limit),
        )
        if manual_ids:
            logger.info(summary.start_log())
        try:
            processed = self._drain_search_queue(
                queue=queue,
                limit=max(1, limit, len(manual_ids)),
                progress_callback=progress_callback,
                summary=summary,
            )
            if manual_ids:
                processed_subscribes = [item for item in subscribes if item.id in processed]
                self._notify_manual_search(
                    True,
                    manual_ids[0] if len(manual_ids) == 1 else None,
                    None if len(manual_ids) == 1 else manual_ids,
                    subscribes,
                    processed_subscribes,
                )
        except Exception:
            summary.round_failed = True
            raise
        finally:
            if not manual_ids and summary.processed:
                summary.requested = summary.processed
            if manual_ids or summary.processed or summary.round_failed:
                logger.info(summary.finish_log())

    def cancel_search_batch(self, batch_id: str) -> bool:
        """请求取消持久搜索批次；未注入队列时返回失败。"""
        queue: Optional[SubscriptionSearchRepository] = getattr(
            self, "subscription_search_repository", None
        )
        return bool(queue and queue.request_cancel(batch_id))

    def get_search_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """返回持久搜索批次状态；未注入队列时返回空。"""
        queue: Optional[SubscriptionSearchRepository] = getattr(
            self, "subscription_search_repository", None
        )
        return queue.get_batch(batch_id) if queue else None


class SubscribeSearchOwner(_SubscribeSearchQueueOwner):
    """订阅搜索加载、单项处理与结果通知 owner。"""

    def _load_search_subscriptions(
        self,
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        state: Optional[str],
    ) -> list[SubscriptionSnapshot]:
        """按单条、指定批次或状态读取本轮搜索订阅。"""
        repository = self.subscription_repository
        if sid:
            subscribe = repository.get(sid)
            return [subscribe] if subscribe else []
        if sids is not None:
            return [item for current_id in sids if (item := repository.get(current_id)) is not None]
        return cast(list[SubscriptionSnapshot], repository.list(self.get_states_for_search(state or "N")))

    @staticmethod
    def _recent_subscription_retry_at(
        subscribe: SubscriptionSnapshot,
        source: str,
    ) -> Optional[str]:
        """新订阅保留一分钟编辑时间，并返回自动开始搜索的时间。"""
        if source != "new" or not subscribe.date:
            return None
        try:
            subscribe_time = datetime.strptime(subscribe.date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning(f"订阅《{subscribe.name}》的添加时间无法识别，将直接开始搜索")
            return None
        remaining = _NEW_SUBSCRIPTION_EDIT_SECONDS - (
            datetime.now() - subscribe_time
        ).total_seconds()
        if remaining <= 0:
            return None
        retry_seconds = max(1, int(remaining) + 1)
        logger.debug(f"新订阅《{subscribe.name}》将在约 {retry_seconds} 秒后自动开始搜索")
        return retry_at_after(retry_seconds)

    def _SubscribeChain__queue_new_subscription_search(
        self,
        subscribe_id: int,
    ) -> Optional[str]:
        """在订阅保存后安排自动搜索，定时扫描仍作为恢复保障。"""
        queue: Optional[SubscriptionSearchRepository] = getattr(
            self, "subscription_search_repository", None
        )
        if queue is None:
            return None
        subscribe = self.subscription_repository.get(subscribe_id)
        if subscribe is None or subscribe.state != "N":
            return None
        available_at = self._recent_subscription_retry_at(subscribe, "new")
        enqueued = queue.enqueue(
            subscription_ids=(subscribe_id,),
            source="new",
            priority=50,
            available_at_by_subscription={
                subscribe_id: available_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
            },
        )
        if enqueued.created_count:
            logger.info(f"已安排新订阅《{subscribe.name}》自动搜索，保存好设置后会自动开始")
        return enqueued.active_batch_ids[0] if enqueued.active_batch_ids else None

    async def _SubscribeChain__async_queue_new_subscription_search(
        self,
        subscribe_id: int,
    ) -> Optional[str]:
        """在异步订阅提交后安排自动搜索。"""
        queue: Optional[SubscriptionSearchRepository] = getattr(
            self, "subscription_search_repository", None
        )
        if queue is None:
            return None
        subscribe = await self.subscription_repository.async_get(subscribe_id)
        if subscribe is None or subscribe.state != "N":
            return None
        available_at = self._recent_subscription_retry_at(subscribe, "new")
        enqueued = await queue.async_enqueue(
            subscription_ids=(subscribe_id,),
            source="new",
            priority=50,
            available_at_by_subscription={
                subscribe_id: available_at
                or datetime.now(timezone.utc).isoformat(timespec="seconds")
            },
        )
        if enqueued.created_count:
            logger.info(f"已安排新订阅《{subscribe.name}》自动搜索，保存好设置后会自动开始")
        return enqueued.active_batch_ids[0] if enqueued.active_batch_ids else None

    @staticmethod
    def _wait_before_scheduled_search(
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        state: Optional[str],
        progress_callback: Optional[Callable[..., None]],
    ) -> None:
        """未使用持久队列时为自动兜底搜索保留逐订阅随机间隔。"""
        if sid or sids is not None or state not in {"R", "P"}:
            return
        sleep_time = random.randint(60, 300)
        logger.debug(f"为了避免连续访问站点，约 {sleep_time} 秒后继续搜索")
        if progress_callback:
            progress_callback(text=f"为了避免连续访问站点，约 {sleep_time} 秒后继续搜索 ...")
        time.sleep(sleep_time)

    def _process_search_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        searchchain: SearchChain,
        execution_context: Optional[SubscriptionExecutionContext] = None,
    ) -> Optional[SubscriptionSnapshot]:
        """处理单个订阅，并返回下载后重新读取的状态快照。"""
        _ensure_execution_active(execution_context)
        logger.debug(f"开始搜索订阅，标题：{subscribe.name} ...")
        if subscribe.type == MediaType.MUSIC.value:
            self._search_music_subscribe(subscribe, execution_context=execution_context)
            return subscribe
        try:
            meta = build_subscribe_meta(subscribe)
        except ValueError:
            logger.error(f"订阅《{subscribe.name}》的媒体类型不受支持，暂时无法搜索")
            return subscribe
        mediainfo: MediaInfo = MediaChain().recognize_media(
            meta=meta,
            mtype=meta.type,
            **subscribe_recognize_kwargs(subscribe),
            episode_group=subscribe.episode_group,
            cache=False,
        )
        if not mediainfo:
            logger.warning(
                f"未识别到媒体信息，标题：{subscribe.name}，"
                f"媒体来源：{subscribe.media_source}，媒体 ID：{subscribe.media_id}"
            )
            return subscribe
        _ensure_execution_active(execution_context)
        mediainfo = apply_subscription_classification(mediainfo, subscribe)
        mediakey = subscribe_media_key(subscribe)
        exists, no_exists = self.check_and_handle_existing_media(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=mediakey,
        )
        if exists:
            return subscribe
        rule_key = (
            SystemConfigKey.BestVersionFilterRuleGroups
            if subscribe.best_version
            else SystemConfigKey.SubscribeFilterRuleGroups
        )
        if execution_context:
            execution_context.report_phase("searching")
        contexts = searchchain.process(
            mediainfo=mediainfo,
            keyword=subscribe.keyword,
            no_exists=no_exists,
            sites=self.get_sub_sites(subscribe),
            rule_groups=subscribe.filter_groups or get_configured_system_config().get(rule_key) or [],
            area="imdbid" if subscribe.search_imdbid and mediainfo.imdb_id else "title",
            custom_words=subscribe.custom_words.split("\n") if subscribe.custom_words else None,
            filter_params=self.get_params(subscribe),
        )
        site_budget_failures = searchchain.consume_subscription_site_budget_failures()
        site_budget_deferrals = searchchain.consume_subscription_site_budget_deferrals()
        _ensure_execution_active(execution_context)
        if not contexts:
            logger.debug(f"订阅 {subscribe.keyword or subscribe.name} 未搜索到资源")
            if not site_budget_failures:
                raise_subscription_site_budget_deferral(site_budget_deferrals, execution_context)
            self.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                lefts=no_exists,
            )
            raise_subscription_site_budget_failures(site_budget_failures)
            return subscribe
        matched = self._filter_search_contexts(subscribe, contexts)
        if not matched:
            logger.debug(f"订阅 {subscribe.name} 没有符合过滤条件的资源")
            if not site_budget_failures:
                raise_subscription_site_budget_deferral(site_budget_deferrals, execution_context)
            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo, lefts=no_exists)
            raise_subscription_site_budget_failures(site_budget_failures)
            return subscribe
        if execution_context:
            execution_context.report_phase("preparing")
        _ensure_execution_active(execution_context)
        downloads, lefts = self._SubscribeChain__download_best_version_with_full_pack_first(
            contexts=matched,
            no_exists=no_exists,
            subscribe=subscribe,
            mediakey=mediakey,
            username=subscribe.username,
            save_path=subscribe.save_path,
            downloader=subscribe.downloader,
            source=self.get_subscribe_source_keyword(subscribe),
            execution_context=execution_context,
        )
        current = self.subscription_repository.get(subscribe.id)
        if current:
            self.finish_subscribe_or_not(
                subscribe=current,
                meta=meta,
                mediainfo=mediainfo,
                downloads=downloads,
                lefts=lefts,
            )
        raise_subscription_site_budget_failures(site_budget_failures)
        raise_subscription_site_budget_deferral(site_budget_deferrals, execution_context)
        return cast(Optional[SubscriptionSnapshot], current)

    def _filter_search_contexts(
        self,
        subscribe: SubscriptionSnapshot,
        contexts: list[Any],
    ) -> list[Any]:
        """过滤不满足洗版范围或优先级的搜索候选，并释放原结果列表。"""
        matched = []
        try:
            for context in contexts:
                if runtime_stop_state.is_system_stopped:
                    break
                torrent_meta = context.meta_info
                torrent_info = context.torrent_info
                media = context.media_info
                if subscribe.best_version and media.type == MediaType.TV:
                    if not self._SubscribeChain__is_full_season_best_version_resource(torrent_meta, subscribe):
                        logger.debug(f"{subscribe.name} 正在全集洗版，{torrent_info.title} 不是全集资源")
                        continue
                    if not self._is_episode_range_covered(torrent_meta, subscribe):
                        logger.debug(f"{subscribe.name} 正在洗版，{torrent_info.title} 不符合订阅集数范围")
                        continue
                    if not self._SubscribeChain__prepare_best_version_tv_candidate(
                        subscribe,
                        context,
                        torrent_info.pri_order,
                    ):
                        logger.debug(f"{subscribe.name} 正在洗版，{torrent_info.title} 优先级未达到当前模式的升级条件")
                        continue
                if (
                    subscribe.best_version
                    and media.type != MediaType.TV
                    and subscribe.current_priority
                    and torrent_info.pri_order <= subscribe.current_priority
                ):
                    logger.debug(f"{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于或等于已下载优先级")
                    continue
                media = apply_subscription_classification(
                    media,
                    subscribe,
                )
                context.media_info = media
                if subscribe.episode_group:
                    media.episode_group = subscribe.episode_group
                matched.append(context)
        finally:
            contexts.clear()
        return matched

    @staticmethod
    def _report_search_progress(
        progress_callback: Optional[Callable[..., None]],
        subscribe: SubscriptionSnapshot,
        index: int,
        total: int,
        finished: bool = False,
    ) -> None:
        """发布单订阅搜索开始或完成进度。"""
        if not progress_callback:
            return
        done = index if finished else index - 1
        data = {"total": total, "finished": done}
        if not finished:
            data["current"] = subscribe.id
        progress_callback(
            value=done / total * 100 if total else 100,
            text=(
                f"订阅搜索（{index}/{total}）处理完成"
                if finished
                else f"正在搜索订阅（{index}/{total}）{subscribe.name} ..."
            ),
            data=data,
        )

    def _notify_manual_search(
        self,
        manual: Optional[bool],
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        subscribes: list[SubscriptionSnapshot],
        processed: list[SubscriptionSnapshot],
    ) -> None:
        """用清晰文案反馈手动搜索的完成或等待状态。"""
        if not manual:
            return
        if not subscribes:
            self.messagehelper.put("没有找到订阅！", title="订阅搜索", role="system")
        elif sid:
            message = (
                f"{subscribes[0].name} 搜索完成！"
                if processed
                else f"{subscribes[0].name} 已安排搜索，系统会自动继续处理。"
            )
            self.messagehelper.put(message, title="订阅搜索", role="system")
        elif sids is not None:
            for subscribe in processed:
                self.messagehelper.put(f"{subscribe.name} 搜索完成！", title="订阅搜索", role="system")
        else:
            message = (
                "所有订阅搜索完成！"
                if len(processed) == len(subscribes)
                else "订阅搜索已安排，暂未完成的项目会自动继续处理。"
            )
            self.messagehelper.put(message, title="订阅搜索", role="system")
