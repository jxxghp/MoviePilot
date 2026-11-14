"""
自动订阅刷新按站点分配每日时间片，并合并错过的触发和失败冷却。

只保存可重建的排程事实，不作为执行队列；在途工作由调用方持有至真实结束。
"""

import hashlib
import math
import secrets
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, cast
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class RefreshPolicy:
    """
    单站每天的刷新预算和时间分布。

    :param timezone_name: 排程使用的本地时区
    :param daily_count: 每个排程日分配的时间片数，包含夜间次数
    :param night_count: 23 点至次日 7 点的次数；其余次数分配到白天
    :param concurrency: 不同站点同时处理的上限，同站始终串行
    :param match_interval: 两次共享候选匹配之间的最小间隔，单位秒
    """

    timezone_name: str = "Asia/Shanghai"
    daily_count: int = 36
    night_count: int = 4
    concurrency: int = 2
    match_interval: int = 300

    def slots(self, anchor: datetime, seed: str, site_id: int) -> list[datetime]:
        """
        在每个时间片中间的 40% 范围抖动，使相邻间隔有上界且每日重新分散。

        :param anchor: 排程日的日期，排程日从当地 7 点开始
        :param seed: 当前实例持久保存的随机盐，避免不同实例请求同步
        :param site_id: 站点稳定标识，不使用域名或认证信息
        :return: 按时间升序排列的当天全部计划时间
        """
        start = anchor.replace(hour=7, minute=0, second=0, microsecond=0)
        result = []
        for phase, count, hours in (("day", self.daily_count - self.night_count, 16),
                                    ("night", self.night_count, 8)):
            if count:
                # 使用时间戳划分时间片，夏令时切换时仍返回明确的真实时间。
                end = start + timedelta(hours=hours)
                width = (end.timestamp() - start.timestamp()) / count
                for index in range(count):
                    identity = f"{seed}:{site_id}:{anchor.date()}:{phase}:{index}"
                    fraction = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8]) / 2**64
                    offset = (index + 0.3 + fraction * 0.4) * width
                    result.append(datetime.fromtimestamp(start.timestamp() + offset, start.tzinfo))
            start += timedelta(hours=hours)
        return result


@dataclass(slots=True)
class RefreshSiteState:
    """
    一个站点的排程和健康事实，不包含凭据或资源内容。

    :param slot: 最近已实际访问的时间片，以 UTC 时间戳标识
    :param next_due: 最早再次检查的 UTC 时间戳
    :param failures: 连续失败次数；成功空列表也会清零
    :param last_attempt: 最近实际访问开始时间；0 表示尚未访问
    :param last_success: 最近成功完成时间；0 表示尚未成功
    """

    slot: float = 0
    next_due: float = 0
    failures: int = 0
    last_attempt: float = 0
    last_success: float = 0


class RefreshSchedule:
    """
    在有界时间片预算内选择到期站点，重启和热重载继续使用已保存的进度。
    """

    def __init__(self, policy: RefreshPolicy, snapshot: Any = None) -> None:
        """
        读取内部缓存；旧版或损坏快照重建排程，不影响资源缓存。

        :param policy: 当前自动刷新策略
        :param snapshot: 上次保存的内部排程快照，无快照时生成实例随机盐
        """
        self.policy = policy
        self.timezone = ZoneInfo(policy.timezone_name)
        data = snapshot if isinstance(snapshot, dict) and snapshot.get("version") == 1 else {}
        seed = data.get("seed")
        self.seed = seed if isinstance(seed, str) else secrets.token_hex(16)
        self.needs_match = data.get("needs_match") is True
        try:
            match_after = float(data.get("match_after") or 0)
            self.match_after = match_after if math.isfinite(match_after) and match_after >= 0 else 0
        except (TypeError, ValueError, OverflowError):
            self.match_after = 0
        self.states: dict[int, RefreshSiteState] = {}
        self._windows: dict[int, tuple[date, list[float]]] = {}
        sites = data.get("sites")
        for site_id, raw in (sites if isinstance(sites, dict) else {}).items():
            try:
                state = RefreshSiteState(**raw)
                if not all(math.isfinite(value) and value >= 0 for value in asdict(state).values()):
                    continue
                self.states[int(site_id)] = state
            except (TypeError, ValueError, OverflowError):
                continue

    def snapshot(self) -> dict[str, Any]:
        """
        返回可持久化的内部快照，不保存线程或在途所有权。
        """
        return {"version": 1, "seed": self.seed, "needs_match": self.needs_match, "match_after": self.match_after,
                "sites": {str(site_id): asdict(state) for site_id, state in self.states.items()}}

    def matching_due(self, now: datetime) -> bool:
        """
        只有待匹配且合并窗口结束时才检查全部订阅，避免站点数放大全量匹配次数。
        """
        return self.needs_match and now.timestamp() >= self.match_after

    def matched(self, now: datetime) -> None:
        """
        匹配成功后清除意图并开启合并窗口；失败时调用方保留意图与原到期时间。
        """
        self.needs_match = False
        self.match_after = now.timestamp() + self.policy.match_interval

    def window(self, site_id: int, now: datetime) -> tuple[float, float]:
        """
        返回最近已到期与下一时间片；多日停机只保留最近一次机会。

        :param site_id: 当前站点的稳定标识
        :param now: 需要定位的真实时间，按策略时区确定排程日
        :return: 最近已到期及下一时间片的 UTC 时间戳
        """
        local = now.astimezone(self.timezone)
        cached = self._windows.get(site_id)
        if cached is None or cached[0] != local.date():
            slots = sorted(slot.timestamp() for days in (-1, 0, 1)
                           for slot in self.policy.slots(local + timedelta(days=days), self.seed, site_id))
            self._windows[site_id] = local.date(), slots
        else:
            slots = cached[1]
        index = bisect_right(slots, now.timestamp())
        return slots[index - 1], slots[index]

    def due(self, site_ids: list[int], now: datetime) -> list[int]:
        """
        按等待时间选择到期站点，新站在五分钟内分散首次检查。

        :param site_ids: 当前有效和仍在途站点，其余排程状态回收
        :param now: 当前真实时间，调用方在每轮扫描读取
        :return: 按到期时间和站点 ID 稳定排序的站点清单
        """
        selected = set(site_ids)
        self.states = {site_id: state for site_id, state in self.states.items() if site_id in selected}
        self._windows = {site_id: window for site_id, window in self._windows.items() if site_id in selected}
        timestamp = now.timestamp()
        due = []
        for site_id in site_ids:
            state = self.states.get(site_id)
            if state is None:
                offset = int(hashlib.sha256(f"{self.seed}:{site_id}:initial".encode()).hexdigest()[:8], 16) % 300
                state = self.states[site_id] = RefreshSiteState(next_due=timestamp + offset)
            previous, following = self.window(site_id, now)
            if state.slot >= previous:
                state.next_due = max(state.next_due, following)
            elif state.next_due <= timestamp:
                due.append(site_id)
        return sorted(due, key=lambda site_id: (self.states[site_id].next_due, site_id))

    def finish(self, site_id: int, started: datetime, fact: dict[str, object], now: datetime) -> None:
        """
        只有真实访问消耗时间片；限流延后保留一次机会，失败按站点退避。

        :param site_id: 本次工作所属站点
        :param started: 实际工作提交时间，计算本次消费的时间片
        :param fact: 抓取结果，包含 attempted、status 及可选 retry_after 秒数
        :param now: 工作真实完成时间
        """
        state = self.states[site_id]
        previous, following = self.window(site_id, started)
        attempted = fact.get("attempted", True)
        status = fact.get("status", "failed")
        if attempted:
            state.slot = previous
            state.last_attempt = started.timestamp()
            self.needs_match = True
        if status == "success":
            state.failures = 0
            state.last_success = now.timestamp()
        elif attempted:
            state.failures += 1
        if status == "deferred" or not attempted:
            state.next_due = now.timestamp() + float(cast(float, fact.get("retry_after") or 60))
            if attempted:
                state.next_due = max(state.next_due, started.timestamp() + 1200)
        else:
            # 不追加预算外重试；失败冷却可以跳过若干时间片，恢复时不补排历史请求。
            cooldown = min(7200, 300 * 2 ** min(state.failures, 5)) if state.failures else 0
            remote_cooldown = float(cast(float, fact.get("retry_after") or 0))
            state.next_due = max(following, started.timestamp() + 1200,
                                 now.timestamp() + max(cooldown, remote_cooldown))

    def next_due(self) -> Optional[str]:
        """
        返回最早计划检查时间，供日志和进度显示。
        """
        if not self.states:
            return None
        return datetime.fromtimestamp(min(state.next_due for state in self.states.values()), timezone.utc).isoformat()
