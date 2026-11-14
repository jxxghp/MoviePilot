"""
自动订阅刷新以有限并发消费到期站点，健康站点先进入共享候选匹配。
"""

from concurrent.futures import FIRST_COMPLETED, Future, wait
from datetime import datetime, timezone
from threading import Event
from typing import Any, Callable, Optional, cast

from app.application.site.protocol import site_domain
from app.application.subscription.refresh import RefreshPolicy, RefreshSchedule
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.torrents import TorrentsChain
from app.domain.context import Context
from app.runtime.log import logger
from app.runtime.observability import record_metric
from app.runtime.stop import runtime_stop_state
from app.runtime.thread import ThreadHelper

# 缓存仅包含时间片和站点计数；不保存下载意图，不替代持久搜索队列。
_SCHEDULE_CACHE = "__subscribe_refresh_schedule__"
_POLL_SECONDS = 30
# 合并窗口只保存安全结果计数，不落盘站点名称、资源、下载地址或错误原文。
_FACT_FIELDS = frozenset({"id", "status", "attempted", "candidates", "new_candidates", "seconds", "retry_after"})
RefreshResult = tuple[str, list[Context], dict[str, object]]


class SubscriptionRefreshPoll:
    """
    一轮自动刷新工作及其真实 Future 的单一 owner，不建立独立线程池。
    """

    def __init__(self, owner: _SubscribeOwnerBase, policy: RefreshPolicy,
                 progress: Optional[Callable[..., None]] = None,
                 stop_event: Optional[Event] = None) -> None:
        """
        绑定订阅链、当前策略和进度；在运行入口读取最新配置与订阅范围。

        :param owner: 提供订阅范围、匹配入口和内部缓存的订阅链
        :param policy: 本轮站点时间片与最大并发策略
        :param progress: 调度任务的可选同步进度回调
        :param stop_event: 协程宿主取消后的封口信号；不取消已经运行的同步 Future
        """
        self.owner = owner
        self.progress = progress
        self.stop_event = stop_event
        self.torrents = TorrentsChain()
        snapshot = owner.load_cache(_SCHEDULE_CACHE)
        self.schedule = RefreshSchedule(policy, snapshot)
        self._saved_snapshot = snapshot
        self.pending: dict[Future[RefreshResult], tuple[int, datetime]] = {}
        raw_facts = snapshot.get("pending_facts", []) if isinstance(snapshot, dict) else []
        self.facts: dict[int, dict[str, object]] = {
            fact["id"]: {key: value for key, value in fact.items() if key in _FACT_FIELDS}
            for fact in raw_facts if isinstance(fact, dict) and isinstance(fact.get("id"), int)
            and fact["id"] in self.schedule.states
        } if isinstance(raw_facts, list) else {}
        self.finished = 0

    @property
    def stopped(self) -> bool:
        """
        系统关闭或本轮取消均停止新提交和匹配，已接受 Future 仍由 finally 收尾。
        """
        return runtime_stop_state.is_system_stopped or bool(self.stop_event and self.stop_event.is_set())

    def run(self) -> None:
        """
        仅提交到期站点，在途时继续检查到期时间；关闭和异常均等待真实工作结束。
        """
        try:
            while True:
                sites = self.owner.get_subscribed_sites()
                if sites is None or self.stopped:
                    break
                indexers = self.torrents.refresh_indexers(sites)
                self._dispatch(indexers)
                if self.schedule.matching_due(datetime.now(timezone.utc)) and not self.stopped:
                    self._match(indexers)
                if not self.pending:
                    break
                completed, _ = wait(self.pending, timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
                self._collect(completed)
            self._report()
        finally:
            # 同步工作不能被 Future.cancel 杀死；收尾前保留 Scheduler 的本轮所有权。
            if self.pending:
                wait(self.pending)
                self._collect(set(self.pending))
            self._save()

    def _match(self, indexers: list[dict[str, Any]]) -> None:
        """
        复用全部有效站点的共享候选排序；匹配异常时保留意图供下一轮恢复。
        """
        domains = {site_domain(indexer.get("domain")) for indexer in indexers}
        candidates = {domain: contexts for domain, contexts in self.torrents.get_torrents().items()
                      if domain in domains}
        names = {indexer["id"]: indexer.get("name") for indexer in indexers}
        facts = tuple({**fact, "name": names[site_id]} for site_id, fact in self.facts.items() if site_id in names)
        self.owner.match(candidates, mtype=None, site_facts=facts, source="automatic")
        self.schedule.matched(datetime.now(timezone.utc))
        self.facts.clear()
        self._save()

    def _dispatch(self, indexers: list[dict[str, Any]]) -> None:
        """
        按等待时间提交到期站点，已在途的站点只保留一次到期机会。
        """
        now = datetime.now(timezone.utc)
        active_ids = {site_id for site_id, _ in self.pending.values()}
        by_id = {int(indexer["id"]): indexer for indexer in indexers}
        due = self.schedule.due(list(by_id.keys() | active_ids), now)
        for site_id in due:
            if len(self.pending) >= self.schedule.policy.concurrency or self.stopped:
                break
            if site_id in active_ids or site_id not in by_id:
                continue
            future = ThreadHelper().submit(self.torrents.refresh_site, by_id[site_id], "spider",
                                           self.owner.has_music_subscribe())
            self.pending[future] = site_id, now
        self._save()

    def _collect(self, completed: set[Future[RefreshResult]]) -> None:
        """
        单站失败只延后该站，其他已接受工作继续处理并保存独立进度。
        """
        for future in completed:
            site_id, started = self.pending.pop(future)
            try:
                _, _, fact = future.result()
            except Exception as error:
                logger.error(f"站点 {site_id} 自动刷新失败：{type(error).__name__}，请检查站点请求日志")
                fact = {"id": site_id, "status": "failed", "attempted": True}
            self.schedule.finish(site_id, started, fact, datetime.now(timezone.utc))
            self.facts[site_id] = {key: value for key, value in fact.items() if key in _FACT_FIELDS}
            self.finished += 1
            record_metric("subscription.refresh.site", owner="subscription", outcome=str(fact.get("status")))
            record_metric("subscription.refresh.site.duration", float(cast(float, fact.get("seconds") or 0)), owner="subscription")
        self._save()
        self._report()

    def _save(self) -> None:
        """
        保存可重建排程事实；失败向上传播，不能在未保存时宣称本轮完成。
        """
        snapshot = self.schedule.snapshot()
        if self.facts:
            snapshot["pending_facts"] = list(self.facts.values())
        if snapshot != self._saved_snapshot:
            self.owner.save_cache(snapshot, _SCHEDULE_CACHE, strict=True)
            self._saved_snapshot = snapshot

    def _report(self) -> None:
        """
        汇报真实站点处理结果和下一次到期时间，空扫描不伪造站点刷新。
        """
        if self.progress:
            self.progress(value=100 if not self.pending else 50,
                          text=f"已刷新 {self.finished} 个站点" if self.finished else "暂无到期站点",
                          data={"finished": self.finished, "running_sites": len(self.pending),
                                "next_refresh_at": self.schedule.next_due()})
