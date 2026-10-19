"""
订阅业务活动事实、限量轮次明细和持久化端口。
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Protocol, cast
from uuid import uuid4

from app.application.subscription.contract import SubscriptionHistorySnapshot, SubscriptionSnapshot
from app.application.subscription.sitebudget import SubscriptionSearchCancelled, SubscriptionSearchDeferred
from app.runtime.log import logger
from app.schemas.common import JsonData

RULE_FIELDS = (
    "include", "exclude", "quality", "resolution", "effect", "sites", "filter_groups",
    "best_version", "best_version_full", "start_episode", "total_episode", "custom_words",
    "audio_quality", "audio_format", "min_bitrate", "min_bit_depth", "min_sample_rate",
)
EDIT_FIELDS = RULE_FIELDS + ("keyword", "search_interval", "state", "downloader", "save_path", "episode_group")
DETAIL_LIMIT = 100
DECISION_RANKS = {"related": 0, "rejected": 1, "qualified": 2, "failed": 3, "submitted": 4}


def _decision_rank(decision: str) -> int:
    """
    按业务重要性排列资源摘要，普通相关和被拒绝项优先让出有界存储。
    """
    return DECISION_RANKS.get(decision, 0)


def _detail_objects(details: Mapping[str, JsonData], field_name: str) -> list[dict[str, JsonData]]:
    """
    恢复由当前服务写入的 JSON 对象列表，忽略旧记录中的不兼容值。
    """
    values = details.get(field_name)
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def activity_time() -> str:
    """
    返回可排序的 UTC 活动时间。
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class SubscriptionActivity:
    """
    脱离数据库会话的单订阅业务事件快照。

    :param activity_key: 贯穿活动订阅与历史订阅的业务活动归属键
    :param event_id: 同一业务轮次的稳定事件标识
    :param kind: 业务活动类型，例如搜索、匹配或订阅变更
    :param source: 触发本轮执行的入口，例如手工搜索或自动调度
    :param status: 当前轮次的运行或结束状态
    :param started_at: 活动开始的 UTC 时间文本
    :param finished_at: 完成执行的时间文本；尚未完成时为 None
    :param counts: 按业务分类聚合的数量
    :param details: 供用户查看的有限量业务明细
    :param tracking: 供恢复轮次使用的内部去重标识列表，按跟踪类型分组
    """

    activity_key: str
    event_id: str
    kind: str
    source: str
    status: str
    started_at: str
    finished_at: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, JsonData] = field(default_factory=dict)
    tracking: dict[str, list[str]] = field(default_factory=dict)


class SubscriptionActivityRepository(Protocol):
    """
    后台轮次写入与带订阅归属校验的分页读取端口。
    """

    def save(self, event: SubscriptionActivity) -> None:
        """
        在独立短事务中新增或更新同一轮次。

        :param event: 要写入的单订阅业务活动快照
        """
        ...

    def get(self, activity_key: str, event_id: str) -> SubscriptionActivity | None:
        """
        读取同订阅同任务的已保存进度，恢复时继续累计同一轮次。

        :param activity_key: 活动订阅与历史订阅共用的活动归属键
        :param event_id: 同一业务轮次的稳定事件标识
        :return: 同一活动归属键和事件标识下的已保存快照；未保存时返回 None
        """
        ...

    async def list_accessible(
        self, subscription_id: int, username: str, superuser: bool, *,
        history: bool, kind: str | None, since: str | None, before: str | None, limit: int,
    ) -> tuple[list[SubscriptionActivity], str | None] | None:
        """
        先确认活动或历史订阅归属，再返回稳定游标分页。

        :param subscription_id: 目标订阅或订阅历史的数据库主键
        :param username: 发起读取的本地用户名，用于校验订阅归属
        :param superuser: 当前用户是否具有跨用户访问权限
        :param history: 是否按订阅历史记录校验归属
        :param kind: 要筛选的活动类型；None 表示不限类型
        :param since: 活动开始时间下界；None 表示不限制
        :param before: 上一页返回的稳定游标；None 表示读取第一页
        :param limit: 最多返回的记录数量
        :return: 活动列表与下一页游标；无后续页时游标为 None，订阅不存在或无权访问时返回 None
        """
        ...

    async def get_accessible(
        self, subscription_id: int, event_id: str, username: str, superuser: bool, *, history: bool,
    ) -> SubscriptionActivity | None:
        """
        按订阅归属和事件身份读取单条完整详情。

        :param subscription_id: 目标订阅或订阅历史的数据库主键
        :param event_id: 同一业务轮次的稳定事件标识
        :param username: 发起读取的本地用户名，用于校验订阅归属
        :param superuser: 当前用户是否具有跨用户访问权限
        :param history: 是否按订阅历史记录校验归属
        :return: 完整活动详情；订阅或活动不存在，或无权访问时返回 None
        """
        ...


def lifecycle_activity(
    snapshot: SubscriptionSnapshot | SubscriptionHistorySnapshot, kind: str, *, old: Mapping[str, JsonData] | None = None,
) -> SubscriptionActivity | None:
    """
    构造创建、修改或完成事实；无可见字段变更不制造活动。
    """
    if not snapshot.activity_key:
        return None
    changes: list[JsonData] = []
    current = snapshot.to_dict()
    if old is not None:
        changes = [
            {"field": key, "before": old.get(key), "after": current.get(key)}
            for key in EDIT_FIELDS if old.get(key) != current.get(key)
        ]
        if not changes:
            return None
        kind = "status" if all(isinstance(item, dict) and item["field"] == "state" for item in changes) else "updated"
    now = activity_time()
    return SubscriptionActivity(
        activity_key=snapshot.activity_key, event_id=uuid4().hex, kind=kind,
        source="user" if kind in {"created", "updated", "status"} else "system",
        status="completed", started_at=now, finished_at=now,
        details={"changes": changes, "rules": {key: current.get(key) for key in RULE_FIELDS}},
    )


def queued_activity(subscribe: SubscriptionSnapshot, task_id: str, source: str,
                    retry_at: str | None) -> SubscriptionActivity | None:
    """
    在搜索请求入队时记录真实请求时间，与恢复执行共用任务事件身份。
    """
    if not subscribe.activity_key:
        return None
    run = SubscriptionActivityRun(subscribe, "search", source, task_id)
    run.status = "waiting"
    run.details["phase"] = "queued"
    run.details["reason"] = "搜索请求已安排，系统会按顺序处理"
    if retry_at:
        run.details["retry_at"] = retry_at
    return run.snapshot()


def settled_activity(event: SubscriptionActivity, state: str, finished_at: str | None) -> SubscriptionActivity | None:
    """
    以队列终态收口尚未进入处理的事件，不把取消和暂停跳过遗留为等待。
    """
    if event.status not in {"running", "waiting"} or state not in {"completed", "failed", "cancelled", "skipped"}:
        return None
    status = "cancelled" if state in {"cancelled", "skipped"} else state
    details = dict(event.details)
    details.pop("retry_at", None)
    details["reason"] = "本次搜索已停止" if status == "cancelled" else "本次搜索处理失败" if status == "failed" else "本次搜索已处理结束"
    return replace(event, status=status, finished_at=finished_at or activity_time(), details=details)


class SubscriptionActivityRun:
    """
    线程安全地累计一轮业务结果，只持久化安全且有上限的资源摘要。
    """

    def __init__(self, subscribe: SubscriptionSnapshot, kind: str, source: str, run_id: str | None = None) -> None:
        """
        冻结本轮订阅条件并初始化各处理阶段的独立计数。
        """
        self.subscribe = subscribe
        self.event_id = run_id or uuid4().hex
        self.kind = kind
        self.source = source
        self.started_at = activity_time()
        self.status = "running"
        self.finished_at: str | None = None
        self.counts: dict[str, int] = {"sites": 0, "requested_sites": 0, "discovered": 0, "related": 0, "qualified": 0, "submitted": 0,
                                       "initial_submitted": 0, "upgrade_submitted": 0, "full_upgrade_submitted": 0}
        self.details: dict[str, JsonData] = {
            "rules": {key: subscribe.to_dict().get(key) for key in RULE_FIELDS},
            "resources": [], "sites": [],
        }
        self._resources: dict[str, dict[str, JsonData]] = {}
        self._related: set[str] = set()
        self._qualified: set[str] = set()
        self._submitted: set[str] = set()
        self._failed: set[str] = set()
        self._sites: dict[int, dict[str, JsonData]] = {}
        self._requested_sites: set[int] = set()
        self._planned_sites: set[int] = set()
        self._lock = RLock()

    def resource(self, context: object, decision: str, reason: str | None = None) -> None:
        """
        按站点和资源稳定键去重，保留标题、阶段和用户可理解的原因。
        """
        torrent = getattr(context, "torrent_info", context)
        site_id = getattr(torrent, "site", None)
        title = str(getattr(torrent, "title", ""))[:500]
        key = hashlib.sha256(f"{site_id}:{getattr(torrent, 'tid', None) or title}".encode()).hexdigest()[:32]
        with self._lock:
            previous = self._resources.get(key)
            if key not in self._related:
                self._related.add(key)
                self.counts["related"] += 1
            elif previous is not None and previous["decision"] == decision and previous.get("reason") == reason:
                return
            if decision in {"qualified", "submitted"} and key not in self._qualified:
                self._qualified.add(key)
                self.counts["qualified"] += 1
            if decision == "submitted" and key not in self._submitted:
                self._submitted.add(key)
                self.counts["submitted"] += 1
                submission = getattr(context, "subscription_download", None) or {}
                submission_type = submission.get("submission_type")
                if submission_type in {"initial", "upgrade"}:
                    self.counts[f"{submission_type}_submitted"] += 1
                    if submission_type == "upgrade" and submission.get("best_version_full"):
                        self.counts["full_upgrade_submitted"] += 1
                self._failed.discard(key)
            if decision == "failed" and key not in self._submitted:
                self._failed.add(key)
            self.counts["download_failed"] = len(self._failed)
            if key in self._submitted:
                decision, reason = "submitted", None
            # 跟踪去重键不保留下载地址、passkey、磁力链接或站点原始响应。
            self._make_detail_room(key, decision)
            if key in self._resources or len(self._resources) < DETAIL_LIMIT:
                self._resources[key] = {
                    "resource_id": key, "title": title, "site": str(getattr(torrent, "site_name", "") or ""),
                    "decision": decision, "reason": reason,
                    "subscription_download": copy.deepcopy(getattr(context, "subscription_download", None)
                                                           or (previous or {}).get("subscription_download"))
                    if decision == "submitted" else None,
                }
            resources: list[dict[str, JsonData]] = sorted(
                self._resources.values(),
                key=lambda item: -_decision_rank(str(item["decision"])),
            )
            self.details["resources"] = cast(JsonData, resources)
            self.details["resources_omitted"] = max(0, len(self._related) - DETAIL_LIMIT)

    def _make_detail_room(self, key: str, decision: str) -> None:
        """
        详情达到上限时优先保留提交、失败和符合条件资源，不能让拒绝项挤掉下载回执。
        """
        if key in self._resources or len(self._resources) < DETAIL_LIMIT:
            return
        lowest = min(self._resources, key=lambda item: _decision_rank(str(self._resources[item]["decision"])))
        if _decision_rank(decision) > _decision_rank(str(self._resources[lowest]["decision"])):
            self._resources.pop(lowest)

    def plan_sites(self, site_ids: Iterable[int]) -> None:
        """
        累计同轮实际计划过的站点，恢复只补查一个站点时保留原计划分母。
        """
        with self._lock:
            self._planned_sites.update(site_ids)
            self.counts["sites"] = max(self.counts["sites"], len(self._planned_sites))

    def discover(self, candidates: int) -> None:
        """
        累计插件等额外来源返回的候选项，与并发站点观察共用同一锁。
        """
        with self._lock:
            self.counts["discovered"] += candidates

    def site(self, site_id: int, *, name: str = "", candidates: int = 0, status: str = "completed", attempted: bool | None = None) -> None:
        """
        累计站点请求结果，与本订阅相关资源计数分别保存。
        """
        with self._lock:
            self._planned_sites.add(site_id)
            self.counts["sites"] = max(self.counts["sites"], len(self._planned_sites))
            previous = self._sites.get(site_id)
            total = candidates + (int(cast(int, previous["candidates"])) if previous else 0)
            did_request = attempted if attempted is not None else status != "waiting"
            self._sites[site_id] = {"id": site_id, "name": name or (previous or {}).get("name") or str(site_id), "candidates": total, "status": status, "attempted": did_request}
            if did_request:
                self._requested_sites.add(site_id)
            self.counts["requested_sites"] = len(self._requested_sites)
            self.counts["discovered"] += candidates
            self.details["sites"] = list(self._sites.values())

    def snapshot(self) -> SubscriptionActivity:
        """
        复制当前计数和明细，避免持久化过程与工作线程共享可变容器。
        """
        with self._lock:
            self.counts["failed_sites"] = sum(site["status"] in {"failed", "error"} for site in self._sites.values())
            self.counts["waiting_sites"] = sum(site["status"] in {"waiting", "deferred"} for site in self._sites.values())
            return SubscriptionActivity(
                self.subscribe.activity_key or "", self.event_id, self.kind, self.source,
                self.status, self.started_at, self.finished_at, dict(self.counts), copy.deepcopy(self.details),
                {"related": sorted(self._related), "qualified": sorted(self._qualified), "submitted": sorted(self._submitted), "failed": sorted(self._failed),
                 "requested_sites": [str(site_id) for site_id in sorted(self._requested_sites)],
                 "planned_sites": [str(site_id) for site_id in sorted(self._planned_sites)]},
            )

    def restore(self, event: SubscriptionActivity) -> None:
        """
        恢复累计事实和有界资源摘要，重试不会重新计算已统计的候选。
        """
        self.started_at = event.started_at
        self.counts.update(event.counts)
        self.details.update(copy.deepcopy(event.details))
        if event.details.get("phase") == "queued":
            self.details["rules"] = {key: self.subscribe.to_dict().get(key) for key in RULE_FIELDS}
        self._related.update(event.tracking.get("related", []))
        self._qualified.update(event.tracking.get("qualified", []))
        self._submitted.update(event.tracking.get("submitted", []))
        self._failed.update(event.tracking.get("failed", []))
        self._requested_sites.update(int(site_id) for site_id in event.tracking.get("requested_sites", []))
        self._planned_sites.update(int(site_id) for site_id in event.tracking.get("planned_sites", []))
        for resource in _detail_objects(self.details, "resources"):
            self._resources[str(resource["resource_id"])] = resource
        for site in _detail_objects(self.details, "sites"):
            self._sites[int(cast(int, site["id"]))] = site
            if site.get("attempted", site["status"] != "waiting") is True:
                self._requested_sites.add(int(cast(int, site["id"])))
        self.details.pop("retry_at", None)
        self.details.pop("reason", None)
        self.details.pop("pending_site_ids", None)
        self.details.pop("phase", None)


_current_run: ContextVar[SubscriptionActivityRun | None] = ContextVar("subscription_activity_run", default=None)


def current_activity_run() -> SubscriptionActivityRun | None:
    """
    读取当前订阅作用域，普通搜索和下载不会生成订阅事件。
    """
    return _current_run.get()


class SubscriptionActivityService:
    """
    在业务执行边界记录轮次，不把运行日志文本解释为用户事件。
    """

    def __init__(self, repository: SubscriptionActivityRepository) -> None:
        """
        注入结构化业务活动持久化端口。
        """
        self.repository = repository

    def save(self, run: SubscriptionActivityRun) -> None:
        """
        尽力保存后台观测事实，观测故障不重复已提交的下载副作用。
        """
        if not run.subscribe.activity_key:
            return
        try:
            self.repository.save(run.snapshot())
        except Exception:
            logger.error("订阅活动保存失败", exc_info=True)

    @staticmethod
    def _finish_run(run: SubscriptionActivityRun) -> None:
        """
        按下载回执与站点事实收口结果；部分失败不会被隐去或冒充全站点成功。
        """
        if run.status != "running":
            return
        failures = run.snapshot().counts["failed_sites"]
        if run.counts["submitted"]:
            run.status = "completed"
        elif run.counts.get("download_failed"):
            run.status = "failed"
            run.details["reason"] = "本轮下载提交未成功，可在资源详情中查看处理结果"
        elif failures and failures == run.counts["sites"]:
            run.status = "failed"
            run.details["reason"] = "本轮站点请求均未成功，可在站点详情和运行日志中查看"
        else:
            run.status = "not_submitted" if run.counts["qualified"] else "empty"
        if failures and "reason" not in run.details:
            run.details["reason"] = f"本轮有 {failures} 个站点请求未成功，其余处理结果已保留"

    @contextmanager
    def scope(self, subscribe: SubscriptionSnapshot, kind: str, source: str, run_id: str | None = None) -> Iterator[SubscriptionActivityRun]:
        """
        保存开始与结束事实，等待和失败由具体业务结果明确设置。
        """
        run = SubscriptionActivityRun(subscribe, kind, source, run_id)
        if run_id and subscribe.activity_key:
            try:
                previous = self.repository.get(subscribe.activity_key, run_id)
                if previous is not None:
                    run.restore(previous)
            except Exception:
                logger.error("订阅活动进度读取失败", exc_info=True)
        attempt_started = activity_time()
        before = dict(run.counts)
        token = _current_run.set(run)
        self.save(run)
        try:
            yield run
            self._finish_run(run)
        except SubscriptionSearchDeferred as deferred:
            run.status = "waiting"
            run.details["retry_at"] = deferred.retry_at
            run.details["reason"] = "站点暂时忙，系统会自动继续搜索"
            run.details["pending_site_ids"] = list(deferred.site_ids)
            raise
        except SubscriptionSearchCancelled:
            run.status = "cancelled"
            run.details["reason"] = "本轮处理已停止，已提交的下载保持有效"
            raise
        except Exception:
            run.status = "failed"
            run.details["reason"] = "本轮处理出现异常，请查看运行日志了解原因"
            raise
        finally:
            run.finished_at = activity_time()
            attempts: list[JsonData] = list(_detail_objects(run.details, "attempts"))
            attempts.append({"started_at": attempt_started, "finished_at": run.finished_at,
                             "status": run.status, "counts": {key: value - before.get(key, 0) for key, value in run.counts.items()},
                             "rules": {key: subscribe.to_dict().get(key) for key in RULE_FIELDS}})
            run.details["attempts"] = attempts[-DETAIL_LIMIT:]
            self.save(run)
            _current_run.reset(token)


@contextmanager
def subscription_activity_scope(service: SubscriptionActivityService | None, subscribe: SubscriptionSnapshot,
                                kind: str, source: str, run_id: str | None = None) -> Iterator[SubscriptionActivityRun]:
    """
    兼容显式注入的旧测试宿主；生产组合根始终提供持久化服务。
    """
    if service is not None:
        with service.scope(subscribe, kind, source, run_id) as run:
            yield run
    else:
        run = SubscriptionActivityRun(subscribe, kind, source, run_id)
        token = _current_run.set(run)
        try:
            yield run
        finally:
            _current_run.reset(token)
