"""
自动订阅刷新在虚拟时间下的预算、覆盖、冷却和重启恢复合同。
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.application.subscription.refresh import RefreshPolicy, RefreshSchedule, RefreshSiteState

NOW = datetime(2026, 11, 14, 4, tzinfo=timezone.utc)


def _schedule(policy: RefreshPolicy = RefreshPolicy()) -> RefreshSchedule:
    """
    使用固定实例盐，保留真实的跨日和跨站时间片算法。
    """
    return RefreshSchedule(policy, {"version": 1, "seed": "synthetic-instance", "sites": {}})


def test_daily_slots_cover_day_and_night_with_bounded_gaps() -> None:
    """
    每天固定预算覆盖完整时段，连续多日的最长间隔受时间片抖动限制。
    """
    policy = RefreshPolicy()
    anchor = NOW.astimezone(ZoneInfo(policy.timezone_name))
    slots = [slot for day in range(20) for slot in policy.slots(anchor + timedelta(days=day), "instance", 1)]
    assert len(slots) == 20 * 36
    assert len(set(slots)) == len(slots)
    for day in range(20):
        daily = slots[day * 36:(day + 1) * 36]
        daytime = [slot for slot in daily if 7 <= slot.hour < 23]
        assert len(daytime) == 32
        assert sum(slot.hour < 7 or slot.hour >= 23 for slot in daily) == 4
        assert all((right - left).total_seconds() < 42 * 60 for left, right in zip(daytime, daytime[1:]))
    gaps = [(right - left).total_seconds() for left, right in zip(slots, slots[1:])]
    assert max(gaps) < 168 * 60
    assert min(gaps) > 18 * 60


@pytest.mark.parametrize("timezone_name", ["Asia/Shanghai", "America/New_York", "Europe/Berlin"])
def test_timezones_and_dst_keep_unique_ordered_real_instants(timezone_name: str) -> None:
    """
    夏令时切换周仍保持预算，所有触发时刻在真实时间上严格递增。
    """
    policy = RefreshPolicy(timezone_name=timezone_name)
    for month, day in ((3, 7), (10, 31)):
        anchor = datetime(2026, month, day, tzinfo=ZoneInfo(timezone_name))
        slots = [slot.timestamp() for offset in range(3)
                 for slot in policy.slots(anchor + timedelta(days=offset), "instance", 1)]
        assert len(slots) == 108
        assert all(right > left for left, right in zip(slots, slots[1:]))


def test_instances_sites_and_days_have_distinct_stable_phases() -> None:
    """
    同一实例重算不改变时间，站点、实例及日期之间均分散请求。
    """
    policy = RefreshPolicy()
    assert policy.slots(NOW, "a", 1) == policy.slots(NOW, "a", 1)
    assert policy.slots(NOW, "a", 1) != policy.slots(NOW, "b", 1)
    assert policy.slots(NOW, "a", 1) != policy.slots(NOW, "a", 2)
    first = policy.slots(NOW, "a", 1)
    following = policy.slots(NOW + timedelta(days=1), "a", 1)
    assert [item.minute for item in first] != [item.minute for item in following]


def test_cold_start_spreads_sites_then_consumes_at_most_one_current_slot() -> None:
    """
    新站首轮五分钟内分散；同一时间片的重复扫描不重复访问。
    """
    schedule = _schedule()
    site_ids = list(range(1, 51))
    schedule.due(site_ids, NOW)
    assert all(NOW.timestamp() <= state.next_due < NOW.timestamp() + 300 for state in schedule.states.values())
    assert len({state.next_due for state in schedule.states.values()}) > 30
    now = NOW + timedelta(minutes=6)
    assert set(schedule.due(site_ids, now)) == set(site_ids)
    for site_id in site_ids:
        schedule.finish(site_id, now, {"status": "success", "attempted": True}, now)
    assert schedule.due(site_ids, now) == []
    restored = RefreshSchedule(schedule.policy, schedule.snapshot())
    assert restored.due(site_ids, now) == []
    assert restored.seed == schedule.seed


def test_long_outage_coalesces_to_one_current_opportunity() -> None:
    """
    停机多日不回放历史时间片，本次完成后只等待下一次计划。
    """
    schedule = _schedule()
    schedule.states[1] = RefreshSiteState(slot=NOW.timestamp() - 86400 * 20)
    assert schedule.due([1], NOW) == [1]
    schedule.finish(1, NOW, {"status": "success"}, NOW)
    assert schedule.due([1], NOW + timedelta(seconds=1)) == []


def test_missed_slots_during_work_produce_only_one_new_due() -> None:
    """
    长工作跨越多个时间片后只合并一次新机会，不按漏掉次数积压请求。
    """
    schedule = _schedule()
    schedule.states[1] = RefreshSiteState()
    finished = NOW + timedelta(hours=4)
    schedule.finish(1, NOW, {"status": "success"}, finished)
    assert schedule.due([1], finished) == [1]
    schedule.finish(1, finished, {"status": "success"}, finished)
    assert schedule.due([1], finished) == []


def test_deferred_work_preserves_budget_and_honors_cooldown() -> None:
    """
    未发请求的限流不消费时间片，冷却到期可继续一次。
    """
    schedule = _schedule()
    schedule.states[1] = RefreshSiteState()
    schedule.finish(1, NOW, {"status": "deferred", "attempted": False, "retry_after": 600}, NOW)
    assert schedule.states[1].slot == 0
    assert schedule.states[1].failures == 0
    assert schedule.due([1], NOW + timedelta(seconds=599)) == []
    assert schedule.due([1], NOW + timedelta(seconds=600)) == [1]


def test_failures_back_off_per_site_and_successful_empty_page_recovers() -> None:
    """
    单站失败冷却持久化且不影响其他站；成功空页不能被当成失败。
    """
    schedule = _schedule()
    schedule.states = {1: RefreshSiteState(failures=4), 2: RefreshSiteState()}
    schedule.finish(1, NOW, {"status": "failed"}, NOW)
    assert schedule.states[1].failures == 5
    assert schedule.states[1].next_due >= NOW.timestamp() + 7200
    assert schedule.due([1, 2], NOW) == [2]
    restored = RefreshSchedule(schedule.policy, schedule.snapshot())
    assert restored.due([1, 2], NOW) == [2]
    later = NOW + timedelta(hours=3)
    restored.finish(1, later, {"status": "success", "candidates": 0}, later)
    assert restored.states[1].failures == 0
    assert restored.states[1].last_success == later.timestamp()


def test_partial_request_deferred_keeps_minimum_gap_between_dense_day_slots() -> None:
    """
    白天加密后，首页已访问但后页限流仍保留 20 分钟间隔，不能在相邻短时间片追加访问。
    """
    schedule = _schedule()
    anchor = NOW.astimezone(ZoneInfo(schedule.policy.timezone_name))
    slots = [slot for day in range(20)
             for slot in schedule.policy.slots(anchor + timedelta(days=day), schedule.seed, 1)]
    started, following = next((left, right) for left, right in zip(slots, slots[1:])
                              if (right - left).total_seconds() < 20 * 60)
    schedule.states[1] = RefreshSiteState()
    schedule.finish(1, started, {"status": "deferred", "attempted": True, "retry_after": 60}, started)
    restored = RefreshSchedule(schedule.policy, schedule.snapshot())
    assert restored.due([1], following) == []
    assert restored.due([1], started + timedelta(minutes=20)) == [1]


def test_removed_sites_are_pruned_without_exposing_resource_or_credentials() -> None:
    """
    排程只保存当前站点的时间事实，移除站点后回收对应记录。
    """
    schedule = _schedule()
    schedule.states = {1: RefreshSiteState(), 2: RefreshSiteState()}
    schedule.due([2], NOW)
    assert set(schedule.snapshot()["sites"]) == {"2"}
    assert schedule.next_due() is not None


def test_remote_cooldown_longer_than_local_backoff_is_preserved() -> None:
    """
    实际请求遇到长 Retry-After 时不被本地退避上限缩短，重启后也保留该时间。
    """
    schedule = _schedule()
    schedule.states[1] = RefreshSiteState()
    schedule.finish(1, NOW, {"status": "rate_limited", "attempted": True, "retry_after": 43200}, NOW)
    restored = RefreshSchedule(schedule.policy, schedule.snapshot())
    assert restored.due([1], NOW + timedelta(hours=11)) == []
    assert restored.due([1], NOW + timedelta(hours=12)) == [1]


@pytest.mark.parametrize("snapshot", [None, [], {"version": 0}, {
    "version": 1, "seed": "synthetic", "sites": {"1": {"next_due": float("nan")}, "bad": {}, "2": {"bad": 1}},
}])
def test_invalid_cache_reconstructs_schedule(snapshot) -> None:
    """
    损坏的内部排程重建有效站点状态，不沿用非有限时间或未知字段。
    """
    schedule = RefreshSchedule(RefreshPolicy(), snapshot)
    assert schedule.states == {}
    schedule.due([1], NOW)
    assert 1 in schedule.states
