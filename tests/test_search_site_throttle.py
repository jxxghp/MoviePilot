"""站点搜索请求间隔调度测试。"""

import pytest

from app.chain.search import provider


@pytest.fixture(autouse=True)
def reset_site_request_schedule():
    """隔离进程级站点预约状态。"""
    provider._site_next_request_at.clear()  # pylint: disable=protected-access
    yield
    provider._site_next_request_at.clear()  # pylint: disable=protected-access


def test_site_without_interval_does_not_wait():
    assert provider._reserve_site_request({"id": 1, "name": "普通站点"}) == 0


def test_configured_requests_are_reserved_twenty_seconds_apart(monkeypatch):
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    site = {"id": 6, "name": "观众", "limit_seconds": 20}

    assert provider._reserve_site_request(site) == 0
    assert provider._reserve_site_request(site) == 20
    assert provider._reserve_site_request(site) == 40


def test_invalid_interval_does_not_throttle(monkeypatch):
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    site = {"id": 6, "name": "观众", "limit_seconds": "invalid"}

    assert provider._reserve_site_request(site) == 0


@pytest.mark.asyncio
async def test_async_wait_only_sleeps_for_throttled_site(monkeypatch):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(provider.asyncio, "sleep", fake_sleep)
    site = {"id": 6, "name": "观众", "limit_seconds": 20}

    await provider._async_wait_for_site_request({"id": 1, "name": "普通站点"})
    await provider._async_wait_for_site_request(site)
    await provider._async_wait_for_site_request(site)

    assert delays == [20]
