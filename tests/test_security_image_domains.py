"""图片域名快照的并发、失效和过期回归。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.application.security import image as image_policy
from app.application.security.image import SiteImageDomainCache, normalize_image_domain
from app.schemas.site import Site


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (" CDN.Example:8443/path ", "https://cdn.example:8443"),
        ("https://[2001:4860:4860::8888]:8443/path", "https://[2001:4860:4860::8888]:8443"),
        ("https://user@host/path", None),
        ("https://@host/path", None),
        ("https://[", None),
        ("file://host/path", None),
        (None, None),
    ],
)
def test_normalize_image_domain(address: str | None, expected: str | None) -> None:
    """只归一化有效图片主机，拒绝用户信息和非 HTTP 协议。"""
    assert normalize_image_domain(address) == expected


@pytest.mark.asyncio
async def test_concurrent_image_requests_share_one_site_query() -> None:
    """冷启动并发和后续命中均共享一次数据库查询，空结果也缓存。"""
    cache = SiteImageDomainCache()
    source = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def load() -> list[Site]:
        """挂起查询以模拟同一页面并发加载图片。"""
        entered.set()
        await release.wait()
        return []

    source.list.side_effect = load
    tasks = [asyncio.create_task(cache.get(source)) for _ in range(30)]
    await entered.wait()
    release.set()
    assert await asyncio.gather(*tasks) == [frozenset()] * 30
    assert await cache.get(source) == frozenset()
    assert source.list.await_count == 1


@pytest.mark.asyncio
async def test_invalidated_inflight_query_cannot_restore_old_domain() -> None:
    """删除或改址发生在查询途中时，旧结果不能重新写入安全白名单。"""
    cache = SiteImageDomainCache()
    source = AsyncMock()

    async def load() -> list[Site]:
        """第一次查询模拟读取旧快照后发生提交失效。"""
        if source.list.await_count == 1:
            cache.invalidate()
            return [Site(url="https://old.example/")]
        return [Site(url="https://new.example/")]

    source.list.side_effect = load
    assert await cache.get(source) == frozenset({"https://new.example"})
    assert source.list.await_count == 2
    assert await cache.get(source) == frozenset({"https://new.example"})
    assert source.list.await_count == 2


@pytest.mark.asyncio
async def test_expiry_and_source_replacement_refresh_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """短期过期兜底及 lifespan 查询服务更换均重新加载站点。"""
    clock = [100.0]
    monkeypatch.setattr(image_policy, "monotonic", lambda: clock[0])
    cache = SiteImageDomainCache(ttl=60)
    source = AsyncMock()
    source.list.return_value = [Site(domain="first.example")]
    assert await cache.get(source) == frozenset({"https://first.example"})
    clock[0] = 159
    await cache.get(source)
    assert source.list.await_count == 1
    clock[0] = 160
    source.list.return_value = []
    assert await cache.get(source) == frozenset()
    assert source.list.await_count == 2
    replacement = AsyncMock()
    replacement.list.return_value = [Site(domain="second.example")]
    assert await cache.get(replacement) == frozenset({"https://second.example"})


@pytest.mark.asyncio
async def test_failed_query_is_retried() -> None:
    """查询失败不缓存结果，恢复后重新获取配置。"""
    cache = SiteImageDomainCache()
    source = AsyncMock()
    source.list.side_effect = [RuntimeError("unavailable"), []]
    with pytest.raises(RuntimeError, match="unavailable"):
        await cache.get(source)
    assert await cache.get(source) == frozenset()
    assert source.list.await_count == 2
