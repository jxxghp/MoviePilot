"""
自动刷新以真实线程屏障验证慢站隔离、到期合并和关闭所有权。
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.subscription.refresh import RefreshPolicy, RefreshSchedule, RefreshSiteState
from app.chain.subscribe import poll as poll_module
from app.chain.subscribe import refresh as refresh_module
from app.chain.subscribe.poll import SubscriptionRefreshPoll
from app.chain.subscribe.refresh import SubscribeRefreshOwner
from app.runtime.tasks import TaskRegistry

NOW = datetime(2026, 11, 14, 4, tzinfo=timezone.utc)
SITES = [{"id": site_id, "name": f"站点{site_id}", "domain": f"https://site{site_id}.example/"}
         for site_id in (1, 2)]


def _owner() -> SimpleNamespace:
    """
    提供已到期的合成站点和独立排程缓存，不连接真实站点、数据库或下载器。
    """
    schedule = RefreshSchedule(RefreshPolicy(), {"version": 1, "seed": "test-instance", "sites": {}})
    schedule.states = {site_id: RefreshSiteState() for site_id in (1, 2)}
    storage = {"schedule": schedule.snapshot()}

    def save(snapshot, *_args, **_kwargs):
        """
        保存快照副本，模拟真实缓存序列化后的隔离语义。
        """
        storage["schedule"] = deepcopy(snapshot)

    return SimpleNamespace(get_subscribed_sites=Mock(return_value=[1, 2]),
                           has_music_subscribe=Mock(return_value=False), match=Mock(),
                           load_cache=lambda _name: deepcopy(storage["schedule"]), save_cache=Mock(side_effect=save),
                           storage=storage)


def _result(indexer, *_args):
    """
    返回一次真实已访问且成功空页的合成事实。
    """
    return f"site{indexer['id']}.example", [], {"id": indexer["id"], "status": "success", "attempted": True}


def _isolate(monkeypatch, pool, refresh_site):
    """
    替换站点和共享线程池边界，保留轮询器真实 Future 等待与收尾。
    """
    clock = SimpleNamespace(now=NOW)
    torrents = SimpleNamespace(refresh_indexers=lambda _sites: SITES, refresh_site=refresh_site,
                               get_torrents=lambda: {"site1.example": [], "site2.example": []})
    monkeypatch.setattr(poll_module, "TorrentsChain", lambda: torrents)
    monkeypatch.setattr(poll_module, "ThreadHelper", lambda: pool)
    monkeypatch.setattr(poll_module, "datetime", SimpleNamespace(now=lambda _tz: clock.now))
    monkeypatch.setattr(poll_module, "runtime_stop_state", SimpleNamespace(is_system_stopped=False))
    monkeypatch.setattr(poll_module, "_POLL_SECONDS", 0.01)
    return clock


def test_healthy_site_matches_and_runs_next_slot_before_slow_site_finishes(monkeypatch):
    """
    一个站点阻塞时，另一个站点可先匹配并继续下一到期时间片；最多同时两个工作。
    """
    slow_started, release_slow, twice_matched = threading.Event(), threading.Event(), threading.Event()
    first_saved = threading.Event()
    owner = _owner()
    save = owner.save_cache.side_effect

    def saved(snapshot, *args, **kwargs):
        """
        首次匹配及其合并窗口保存后通知主测试线程，避免在匹配中途推进虚拟时间。
        """
        save(snapshot, *args, **kwargs)
        if snapshot["match_after"] > NOW.timestamp() and not snapshot["needs_match"]:
            first_saved.set()

    owner.save_cache.side_effect = saved
    active = set()
    guard = threading.Lock()
    counts = {1: 0, 2: 0}

    def fetch(indexer, *args):
        """
        首站由屏障阻塞，记录真实在途站点以检查同站互斥和并发上限。
        """
        site_id = indexer["id"]
        with guard:
            assert site_id not in active
            active.add(site_id)
            counts[site_id] += 1
            assert len(active) <= 2
        try:
            if site_id == 1:
                slow_started.set()
                assert release_slow.wait(5)
            return _result(indexer, *args)
        finally:
            with guard:
                active.remove(site_id)

    with ThreadPoolExecutor(max_workers=2) as workers, ThreadPoolExecutor(max_workers=1) as caller:
        clock = _isolate(monkeypatch, workers, fetch)

        def matched(*_args, **_kwargs):
            """
            第二次健康结果匹配后结束本轮新提交。
            """
            if owner.match.call_count == 2:
                owner.get_subscribed_sites.return_value = None
                twice_matched.set()

        owner.match.side_effect = matched
        poll = SubscriptionRefreshPoll(owner, RefreshPolicy())
        future = caller.submit(poll.run)
        try:
            assert slow_started.wait(2)
            assert first_saved.wait(2)
            clock.now += timedelta(hours=3)
            assert twice_matched.wait(2)
            assert counts[2] == 2
            assert counts[1] == 1
            assert not future.done()
            assert len(poll.pending) == 1
        finally:
            release_slow.set()
        future.result(timeout=2)
    assert not active
    assert not poll.pending


def test_single_site_failure_does_not_abort_healthy_work(monkeypatch):
    """
    单站抛错会记录该站冷却，其他站仍完成匹配，保存的事实不包含错误中的凭据。
    """
    owner = _owner()

    def fetch(indexer, *args):
        """
        用含合成敏感文本的异常检查排程快照不会存入原始错误。
        """
        if indexer["id"] == 1:
            raise RuntimeError("synthetic-secret-value")
        return _result(indexer, *args)

    with ThreadPoolExecutor(max_workers=2) as workers:
        _isolate(monkeypatch, workers, fetch)
        poll = SubscriptionRefreshPoll(owner, RefreshPolicy())
        poll.run()
    assert owner.match.called
    snapshot = owner.storage["schedule"]
    assert snapshot["sites"]["1"]["failures"] == 1
    assert snapshot["sites"]["2"]["last_success"] == NOW.timestamp()
    assert "synthetic-secret-value" not in str(snapshot)
    assert not poll.pending


@pytest.mark.parametrize("stop_kind", ["runtime", "cancellation"])
def test_stop_drains_real_workers_before_returning(monkeypatch, stop_kind):
    """
    停止信号后不再匹配或提交，调用方等待同步 worker 真正结束才返回。
    """
    started, release = threading.Event(), threading.Event()
    cancelled = threading.Event()
    owner = _owner()

    def fetch(indexer, *args):
        """
        在真实 Future 内等待释放，模拟不可强制取消的同步请求。
        """
        started.set()
        assert release.wait(5)
        return _result(indexer, *args)

    with ThreadPoolExecutor(max_workers=2) as workers, ThreadPoolExecutor(max_workers=1) as caller:
        _isolate(monkeypatch, workers, fetch)
        stop = SimpleNamespace(is_system_stopped=False)
        monkeypatch.setattr(poll_module, "runtime_stop_state", stop)
        poll = SubscriptionRefreshPoll(owner, RefreshPolicy(), stop_event=cancelled)
        future = caller.submit(poll.run)
        try:
            assert started.wait(2)
            if stop_kind == "runtime":
                stop.is_system_stopped = True
            else:
                cancelled.set()
            assert not future.done()
        finally:
            release.set()
        future.result(timeout=2)
    owner.match.assert_not_called()
    assert not poll.pending


def test_empty_scan_never_submits_or_matches(monkeypatch):
    """
    到期前扫描只检查排程，不能把每分钟轮询变成每分钟访问站点。
    """
    owner = _owner()
    for state in owner.storage["schedule"]["sites"].values():
        state["next_due"] = NOW.timestamp() + 3600
    with ThreadPoolExecutor(max_workers=2) as workers:
        fetch = Mock(side_effect=_result)
        _isolate(monkeypatch, workers, fetch)
        SubscriptionRefreshPoll(owner, RefreshPolicy()).run()
    fetch.assert_not_called()
    owner.match.assert_not_called()
    owner.save_cache.assert_not_called()


@pytest.mark.parametrize("scheduled,mode,uses_plan", [(True, "spider", True), (False, "spider", False),
                                                      (True, "rss", False), (False, "rss", False)])
def test_only_scheduled_spider_uses_due_plan(monkeypatch, scheduled, mode, uses_plan):
    """
    自动 spider 使用到期计划，手工刷新和 RSS 仍直接刷新所选站点并匹配。
    """
    owner = object.__new__(SubscribeRefreshOwner)
    owner.runtime_config = SimpleNamespace(subscribe_mode=mode)
    owner.get_subscribed_sites = Mock(return_value=[1])
    owner.has_music_subscribe = Mock(return_value=False)
    owner.match = Mock()
    poll, torrents = Mock(), Mock()
    monkeypatch.setattr(refresh_module, "SubscriptionRefreshPoll", poll)
    monkeypatch.setattr(refresh_module, "TorrentsChain", lambda: torrents)
    monkeypatch.setattr(refresh_module, "get_scheduler_runtime_config", lambda: SimpleNamespace(timezone="Asia/Shanghai"))
    owner.refresh(scheduled=scheduled)
    assert poll.return_value.run.called is uses_plan
    assert torrents.refresh.called is not uses_plan
    assert owner.match.called is not uses_plan


def test_failed_match_is_recovered_from_cache_without_refetching(monkeypatch):
    """
    抓取完成后匹配抛错，重建轮询器仍从缓存完成匹配，不额外消耗站点请求。
    """
    owner = _owner()
    owner.match.side_effect = RuntimeError("synthetic matching failure")
    fetch = Mock(side_effect=_result)
    with ThreadPoolExecutor(max_workers=2) as workers:
        _isolate(monkeypatch, workers, fetch)
        with pytest.raises(RuntimeError, match="synthetic matching failure"):
            SubscriptionRefreshPoll(owner, RefreshPolicy()).run()
        assert owner.storage["schedule"]["needs_match"] is True
        assert fetch.call_count == 2
        owner.match.side_effect = None
        SubscriptionRefreshPoll(owner, RefreshPolicy()).run()
    assert fetch.call_count == 2
    assert owner.storage["schedule"]["needs_match"] is False


def test_matching_window_coalesces_site_results_and_survives_restart(monkeypatch):
    """
    五分钟内完成的其他站点集中匹配，跨轮次恢复安全计数且不重复抓取资源。
    """
    release_second = threading.Event()
    owner = _owner()

    def fetch(indexer, *args):
        """
        等待首站完成匹配后才返回第二站，制造同一合并窗口的后续结果。
        """
        if indexer["id"] == 2:
            assert release_second.wait(5)
        return _result(indexer, *args)

    owner.match.side_effect = lambda *_args, **_kwargs: release_second.set()
    with ThreadPoolExecutor(max_workers=2) as workers:
        request = Mock(side_effect=fetch)
        clock = _isolate(monkeypatch, workers, request)
        SubscriptionRefreshPoll(owner, RefreshPolicy()).run()
        assert owner.match.call_count == 1
        snapshot = owner.storage["schedule"]
        assert snapshot["needs_match"] is True
        assert [fact["id"] for fact in snapshot["pending_facts"]] == [2]
        assert "name" not in snapshot["pending_facts"][0]
        clock.now += timedelta(minutes=2)
        SubscriptionRefreshPoll(owner, RefreshPolicy()).run()
        assert owner.match.call_count == 1
        clock.now += timedelta(minutes=3)
        SubscriptionRefreshPoll(owner, RefreshPolicy()).run()
    assert request.call_count == 2
    assert owner.match.call_count == 2
    assert owner.match.call_args.kwargs["site_facts"] == ({"id": 2, "status": "success", "attempted": True, "name": "站点2"},)
    assert owner.storage["schedule"]["needs_match"] is False
    assert "pending_facts" not in owner.storage["schedule"]


def test_failed_schedule_save_still_waits_for_accepted_real_work(monkeypatch):
    """
    排程保存失败也必须等待已接受的同步抓取退出，不能泄漏站点执行所有权。
    """
    started, release = threading.Event(), threading.Event()
    owner = _owner()
    owner.save_cache.side_effect = OSError("synthetic disk failure")
    # 使派发前的排程状态发生变化，触发确认保存路径。
    owner.storage["schedule"]["sites"]["3"] = {}

    def fetch(indexer, *args):
        """
        将已接受请求保持在真实线程内，供保存失败后的收尾检查。
        """
        started.set()
        assert release.wait(5)
        return _result(indexer, *args)

    with ThreadPoolExecutor(max_workers=2) as workers, ThreadPoolExecutor(max_workers=1) as caller:
        _isolate(monkeypatch, workers, fetch)
        poll = SubscriptionRefreshPoll(owner, RefreshPolicy())
        future = caller.submit(poll.run)
        try:
            assert started.wait(2)
            assert not future.done()
        finally:
            release.set()
        with pytest.raises(OSError, match="synthetic disk failure"):
            future.result(timeout=2)
    assert not poll.pending


@pytest.mark.asyncio
async def test_async_refresh_cancellation_retains_real_work_owner(monkeypatch):
    """
    取消异步外壳后仍保留线程工作，真实结束后才向 Scheduler 传播取消。
    """
    started, release, sealed = threading.Event(), threading.Event(), threading.Event()
    registry = TaskRegistry()
    monkeypatch.setattr(refresh_module, "get_task_registry", lambda: registry)
    owner = object.__new__(SubscribeRefreshOwner)

    def refresh(**kwargs):
        """
        模拟仍在执行的同步刷新入口。
        """
        started.set()
        assert kwargs["_stop_event"].wait(2)
        sealed.set()
        assert release.wait(5)

    owner.refresh = refresh
    task = asyncio.create_task(owner.async_refresh(scheduled=True))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(sealed.wait, 2)
        assert not task.done()
        assert registry.records
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert not registry.records
    finally:
        release.set()
        await registry.shutdown()
