"""
站点搜索请求间隔调度测试。
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.application.site import throttle
from app.chain.search import provider
from app.runtime.rate import RequestRateLimiter


@pytest.fixture(autouse=True)
def reset_site_request_schedule(monkeypatch):
    """
    隔离进程级站点预约状态。
    """
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())


def test_site_without_interval_does_not_wait(monkeypatch):
    """
    未配置请求间隔的站点不需要等待。
    """
    monkeypatch.setattr(throttle.time, "sleep", lambda _delay: pytest.fail("未配置流控不应等待"))
    assert provider._wait_for_site_request({"id": 1, "name": "普通站点"}) is None


def test_configured_requests_are_reserved_twenty_seconds_apart(monkeypatch):
    """
    配置二十秒间隔后，连续站点请求按该间隔预留执行时间。
    """
    clock = SimpleNamespace(now=100.0, starts=[])

    def advance(delay):
        """
        推进虚拟时间以模拟真实等待。
        """
        clock.now += delay

    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(throttle.time, "sleep", advance)
    site = {"id": 6, "name": "观众", "limit_seconds": 20}

    for _ in range(3):
        assert provider._wait_for_site_request(site) is None
        clock.starts.append(clock.now)
    assert clock.starts == [100, 120, 140]


def test_invalid_interval_does_not_throttle(monkeypatch):
    """
    无效请求间隔不启用站点限速。
    """
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    site = {"id": 6, "name": "观众", "limit_seconds": "invalid"}

    assert provider._wait_for_site_request(site) is None


@pytest.mark.parametrize("interval, reason, elapsed, expected", [
    (20, "访问间隔不得小于 20 秒", 7, 14),
    (30, "访问间隔不得小于 30 秒", 7, 24),
    (20, "3600 秒内访问次数不得超过 25 次", 7, 3594),
    (20, "3600 秒内访问次数不得超过 25 次", 3600, 1),
    (20, "访问间隔不得小于 20 秒", 25, 1),
    (20, "访问间隔不得小于 20 秒", -5, 21),
    (20, "未知限制", 7, 3601),
])
def test_local_limit_backoff_uses_actual_rule_and_remaining_time(monkeypatch, interval, reason, elapsed, expected):
    """
    访问间隔和计数窗口分别扣除已等待时间，保留边界余量，未知原因沿用保守窗口。
    """
    now = datetime(2026, 10, 26, 12, 0, 0).timestamp()
    monkeypatch.setattr(provider.time, "time", lambda: now)
    last_visit = datetime.fromtimestamp(now - elapsed).strftime("%Y-%m-%d %H:%M:%S")
    message = f"触发流控规则，{reason}，上次访问时间：{last_visit}"
    site = {"limit_seconds": interval, "limit_interval": 3600, "limit_count": 25}

    assert provider._site_rate_limit_backoff(site, message) == expected


@pytest.mark.parametrize("message, expected", [
    ("触发流控规则，访问间隔不得小于 20 秒", 21),
    ("触发流控规则，访问间隔不得小于 20 秒，上次访问时间：invalid", 21),
    ("触发流控规则，3600 秒内访问次数不得超过 25 次，上次访问时间：invalid", 3601),
    (None, 3601),
])
def test_local_limit_without_valid_timestamp_keeps_rule_duration(message, expected):
    """
    旧资源缺少有效时间时等待对应规则全长，缺少原因时保留既有窗口回退。
    """
    site = {"limit_seconds": 20, "limit_interval": 3600, "limit_count": 25}
    assert provider._site_rate_limit_backoff(site, message) == expected


@pytest.mark.parametrize("site, reason", [
    ({"limit_seconds": 0, "limit_interval": 3600, "limit_count": 25}, "访问间隔不得小于 20 秒"),
    ({"limit_seconds": "invalid", "limit_interval": 3600, "limit_count": 25}, "访问间隔不得小于 20 秒"),
    ({"limit_seconds": 20, "limit_interval": "invalid", "limit_count": 25}, "3600 秒内访问次数不得超过 25 次"),
    ({"limit_seconds": 20, "limit_interval": 3600, "limit_count": 0}, "3600 秒内访问次数不得超过 25 次"),
])
def test_inactive_or_invalid_reported_rule_does_not_invent_cooldown(site, reason):
    """
    资源包说明与当前配置不一致时，已停用或不可读的规则不产生额外冷却，沿用调用方的默认间隔。
    """
    assert provider._site_rate_limit_backoff(site, f"触发流控规则，{reason}") == 0


@pytest.mark.asyncio
async def test_async_wait_only_sleeps_for_throttled_site(monkeypatch):
    """
    异步入口只为受限站点等待预留间隔。
    """
    delays = []
    clock = SimpleNamespace(now=100.0)

    async def fake_sleep(delay):
        """
        记录异步等待并推进虚拟时间。
        """
        delays.append(delay)
        clock.now += delay

    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(throttle.asyncio, "sleep", fake_sleep)
    site = {"id": 6, "name": "观众", "limit_seconds": 20}

    await provider._async_wait_for_site_request({"id": 1, "name": "普通站点"})
    await provider._async_wait_for_site_request(site)
    await provider._async_wait_for_site_request(site)

    assert delays == [20]
