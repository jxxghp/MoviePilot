"""插件市场目录读取并发控制测试。"""

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from app.application.plugin.catalog import PluginCatalogService


def _catalog(
        max_concurrency: int = 2,
        error: Callable[[str], None] | None = None,
) -> PluginCatalogService:
    """构造不依赖真实插件目录和网络的目录服务。"""
    return PluginCatalogService(
        market_loader=lambda *_args: {},
        async_market_loader=lambda *_args: {},
        installed_plugins_provider=lambda: [],
        plugin_mapper=lambda *_args, **_kwargs: None,
        is_local_repo=lambda _repo_url: False,
        version_compare=lambda *_args: False,
        warning=lambda *_args: None,
        error=error if error is not None else lambda *_args: None,
        max_concurrency=max_concurrency,
    )


def test_sync_collect_limits_market_fetch_concurrency() -> None:
    """同步目录刷新不能因市场数量增长而创建无限线程。"""
    lock = threading.Lock()
    active = 0
    peak = 0
    calls: list[tuple[str, str | None, bool]] = []

    def loader(market: str, package_version: str | None, force: bool):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((market, package_version, force))
        time.sleep(0.01)
        with lock:
            active -= 1
        return []

    result = _catalog(max_concurrency=2).collect(
        markets=["market-a", "market-b", "market-c"],
        compatible_flags=["v3"],
        force=True,
        loader=loader,
    )

    assert result == []
    assert len(calls) == 6
    assert peak <= 2


def test_sync_collect_isolates_market_failure() -> None:
    """同步目录刷新遇到单个市场异常时仍保留其他市场结果。"""
    errors: list[str] = []

    def loader(
            market: str,
            package_version: str | None,
            _force: bool,
    ):
        if market == "market-b" and package_version == "v3":
            raise RuntimeError("market unavailable")
        return []

    result = _catalog(max_concurrency=2, error=errors.append).collect(
        markets=["market-a", "market-b"],
        compatible_flags=["v3"],
        force=True,
        loader=loader,
    )

    assert result == []
    assert len(errors) == 1
    assert "market unavailable" in errors[0]


@pytest.mark.asyncio
async def test_async_collect_limits_market_fetch_concurrency() -> None:
    """异步目录刷新必须用信号量限制同时在途的市场请求。"""
    active = 0
    peak = 0

    async def loader(_market: str, _package_version: str | None, _force: bool):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    result = await _catalog(max_concurrency=2).async_collect(
        markets=["market-a", "market-b", "market-c"],
        compatible_flags=["v3"],
        force=True,
        loader=loader,
    )

    assert result == []
    assert peak <= 2


def test_market_fetch_concurrency_must_be_positive() -> None:
    """无效并发值应在服务构造时快速失败。"""
    with pytest.raises(ValueError, match="大于 0"):
        _catalog(max_concurrency=0)
