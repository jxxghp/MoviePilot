import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from packaging.version import Version

from app.application.plugin.catalog import PluginCatalogService


def _plugin(plugin_id: str, version: str, repo_url: str):
    """构造目录合并测试使用的最小插件 DTO。"""
    return SimpleNamespace(
        id=plugin_id,
        plugin_version=version,
        repo_url=repo_url,
    )


def _service(**overrides) -> PluginCatalogService:
    """构造完全依赖内存假对象的插件目录应用服务。"""
    defaults = {
        "market_loader": Mock(return_value={}),
        "async_market_loader": Mock(),
        "installed_plugins_provider": Mock(return_value=[]),
        "plugin_mapper": Mock(),
        "is_local_repo": lambda value: str(value).startswith("local://"),
        "version_compare": (
            lambda left, operator, right:
            operator == ">" and Version(left) > Version(right)
        ),
        "warning": Mock(),
        "error": Mock(),
    }
    defaults.update(overrides)
    return PluginCatalogService(**defaults)


def test_merge_prefers_higher_generation_over_same_base_entry():
    """高代际索引出现同 ID 同版本时不再保留基础索引副本。"""
    service = _service()
    higher = _plugin("Demo", "2.0.0", "https://market-a")
    base = _plugin("Demo", "2.0.0", "https://market-b")

    result = service.merge([higher], [base], ["https://market-a", "https://market-b"])

    assert result == [higher]


def test_merge_prefers_newer_version_and_remote_source():
    """相同插件保留最高版本，同版本时市场来源覆盖本地副本。"""
    service = _service()
    old_remote = _plugin("Demo", "1.0.0", "https://market-a")
    new_local = _plugin("Demo", "2.0.0", "local://Demo")
    new_remote = _plugin("Demo", "2.0.0", "https://market-b")

    result = service.merge(
        [old_remote, new_local, new_remote],
        [],
        ["https://market-a", "https://market-b"],
    )

    assert result == [new_remote]


def test_load_maps_market_entries_with_installed_snapshot():
    """单市场读取只获取一次已安装快照并按索引顺序映射 DTO。"""
    mapper = Mock(side_effect=lambda plugin_id, *_args: plugin_id)
    installed_provider = Mock(return_value=["Installed"])
    service = _service(
        market_loader=Mock(return_value={"First": {}, "Second": {}}),
        installed_plugins_provider=installed_provider,
        plugin_mapper=mapper,
    )

    result = service.load("https://market-a", "v3", True)

    assert result == ["First", "Second"]
    installed_provider.assert_called_once_with()
    assert mapper.call_args_list[0].args[3:] == (["Installed"], 2, "v3")
    assert mapper.call_args_list[1].args[3:] == (["Installed"], 1, "v3")


@pytest.mark.asyncio
async def test_async_collect_isolates_failure_and_completes_progress():
    """异步市场单任务失败时保留成功结果，并把进度推进到完成态。"""
    progress = Mock()
    error = Mock()
    service = _service(error=error)

    async def loader(market: str, package_version: str | None, _force: bool):
        """模拟一个失败代际和其余可正常完成的市场请求。"""
        await asyncio.sleep(0)
        if market == "https://market-a" and package_version == "v3":
            raise RuntimeError("unavailable")
        version = "2.0.0" if package_version else "1.0.0"
        return [_plugin(market, version, market)]

    result = await service.async_collect(
        markets=["https://market-a", "https://market-b"],
        compatible_flags=["v3"],
        force=True,
        loader=loader,
        progress_callback=progress,
    )

    assert {plugin.id for plugin in result} == {
        "https://market-a",
        "https://market-b",
    }
    error.assert_called_once()
    assert progress.call_args_list[0].kwargs["value"] == 0
    assert progress.call_args_list[-1].kwargs["value"] == 100


@pytest.mark.asyncio
async def test_async_collect_cancels_all_loaders_when_parent_is_cancelled():
    """请求取消时必须取消并回收全部市场 loader，不能把子任务遗留在事件循环。"""
    service = _service()
    blocker = asyncio.Event()
    all_started = asyncio.Event()
    started = 0
    cancelled = 0

    async def loader(_market: str, _package_version: str | None, _force: bool):
        """记录市场 loader 的启动和取消，并等待测试释放。"""
        nonlocal started, cancelled
        started += 1
        if started == 2:
            all_started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        return []

    collect_task = asyncio.create_task(
        service.async_collect(
            markets=["https://market-a", "https://market-b"],
            compatible_flags=[],
            force=True,
            loader=loader,
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=1)
    collect_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await collect_task
        await asyncio.sleep(0)
        assert cancelled == 2
    finally:
        blocker.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_async_collect_cleans_loaders_when_progress_callback_fails():
    """进度回调异常也必须取消并回收尚未完成的市场 loader。"""
    service = _service()
    blocker = asyncio.Event()
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def loader(market: str, _package_version: str | None, _force: bool):
        """让一个市场立即完成，另一个保持阻塞以验证异常清理。"""
        if market == "https://market-a":
            await slow_started.wait()
            return []
        slow_started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            slow_cancelled.set()
            raise
        return []

    def progress(*, value: float, **_kwargs) -> None:
        """首个市场完成时模拟进度消费者失败。"""
        if value > 0:
            raise RuntimeError("progress unavailable")

    with pytest.raises(RuntimeError, match="progress unavailable"):
        await asyncio.wait_for(
            service.async_collect(
                markets=["https://market-a", "https://market-b"],
                compatible_flags=[],
                force=True,
                loader=loader,
                progress_callback=progress,
            ),
            timeout=1,
        )
    assert slow_cancelled.is_set()
