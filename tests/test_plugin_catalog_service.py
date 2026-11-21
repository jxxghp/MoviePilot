"""
验证插件目录来源查询与已安装插件信息汇总。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from packaging.version import Version

from app.application.plugin.catalog import (
    PluginCatalogService,
    PluginMarketUnavailableError,
)
from app.runtime.extensions.plugin.metadata import PluginMetadataMapper
from app.schemas.plugin import Plugin


def _plugin(
        plugin_id: str,
        version: str,
        repo_url: str,
        package_version: str | None = "v3",
) -> Plugin:
    """
    构造目录合并测试使用的最小插件 DTO。
    """
    plugin = Plugin(
        id=plugin_id,
        plugin_version=version,
        repo_url=repo_url,
    )
    plugin.package_version = package_version
    return plugin


def _service(**overrides) -> PluginCatalogService:
    """
    构造完全依赖内存假对象的插件目录应用服务。
    """
    defaults = {
        "market_loader": Mock(return_value={}),
        "async_market_loader": AsyncMock(return_value={}),
        "installed_plugins_provider": Mock(return_value=[]),
        "plugin_mapper": Mock(),
        "is_local_repo": lambda value: str(value).startswith("local://"),
        "version_compare": (
            lambda left, operator, right:
            operator == ">" and Version(left) > Version(right)
        ),
        "warning": Mock(),
        "error": Mock(),
        "info": Mock(),
    }
    defaults.update(overrides)
    return PluginCatalogService(**defaults)


def test_merge_prefers_higher_generation_over_same_base_entry():
    """
    高代际索引出现同 ID 同版本时不再保留基础索引副本。
    """
    service = _service()
    higher = _plugin("Demo", "2.0.0", "https://market-a")
    base = _plugin("Demo", "2.0.0", "https://market-b")

    result = service.merge([higher], [base], ["https://market-a", "https://market-b"])

    assert result == [higher]


def test_merge_prefers_newer_version_and_remote_source():
    """
    相同插件保留最高版本，同版本时市场来源覆盖本地副本。
    """
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


def test_merge_treats_plugin_id_casing_as_one_physical_plugin():
    """
    同一物理插件的市场与本地 ID 大小写差异不能产生两个目录条目。
    """
    service = _service()
    online = _plugin("DownloadCenter", "3.2.1", "https://market-a")
    local = _plugin("downloadcenter", "3.3.2", "local://DownloadCenter")

    result = service.merge(
        [online, local],
        [],
        ["https://market-a"],
    )

    assert result == [local]


def test_merge_by_source_deduplicates_plugin_id_casing_within_generation():
    """
    同一仓库和代际内的 ID 大小写差异不能产生重复候选。
    """
    service = _service()
    old = _plugin("DownloadCenter", "3.2.1", "https://market-a")
    new = _plugin("downloadcenter", "3.3.2", "https://market-a")

    result = service.merge_by_source(
        [old, new],
        [],
        ["https://market-a"],
    )

    assert result == [new]


def test_merge_by_source_preserves_candidates_across_generations():
    """
    跨代际候选必须保留，由来源准入按代际优先级继续决策。
    """
    service = _service()
    v3 = _plugin("DownloadCenter", "3.2.1", "https://market-a", "v3")
    v2 = _plugin("downloadcenter", "9.0.0", "https://market-a", "v2")

    result = service.merge_by_source(
        [v3, v2],
        [],
        ["https://market-a"],
    )

    assert result == [v3, v2]


def test_load_maps_market_entries_with_installed_snapshot():
    """
    单市场读取只获取一次已安装快照并按索引顺序映射 DTO。
    """
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


def test_metadata_mapper_preserves_package_generation_without_serializing_it():
    """
    市场映射必须保留候选代际，同时不能扩张插件 API 响应。
    """
    mapper = PluginMetadataMapper(
        plugin_instance=lambda _plugin_id: None,
        plugin_class=lambda _plugin_id: None,
        annotate_system_version=lambda info: info,
        annotate_runtime_compatibility=lambda info: info,
        is_package_compatible=lambda _info, _version: True,
        auth_checker=lambda _plugin, _source: True,
        version_compare=lambda _left, _operator, _right: False,
        log=Mock(),
    )

    plugin = mapper.map(
        plugin_id="Demo",
        plugin_info={"name": "Demo", "version": "3.0.0"},
        market="https://market-a",
        installed_plugins=[],
        add_time=1,
        package_version="v3",
    )

    assert plugin is not None
    assert plugin.package_version == "v3"
    assert "package_version" not in plugin.model_dump()
    assert "package_version" not in Plugin.model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_async_collect_isolates_failure_and_completes_progress():
    """
    异步市场单任务失败时保留成功结果，并把进度推进到完成态。
    """
    progress = Mock()
    error = Mock()
    async def loader(market: str, package_version: str | None, _force: bool):
        """
        模拟一个失败代际和其余可正常完成的市场请求。
        """
        await asyncio.sleep(0)
        if market == "https://market-a" and package_version == "v3":
            raise RuntimeError("unavailable")
        version = "2.0.0" if package_version else "1.0.0"
        plugin_id = "MarketA" if market.endswith("market-a") else "MarketB"
        return {plugin_id: {"version": version}}

    service = _service(
        async_market_loader=loader,
        plugin_mapper=lambda plugin_id, info, market, _installed, _time, generation: (
            _plugin(plugin_id, info["version"], market, generation)
        ),
        error=error,
    )
    result = await service.async_collect(
        markets=["https://market-a", "https://market-b"],
        compatible_flags=["v3"],
        force=True,
        require_available=True,
        progress_callback=progress,
    )

    assert {plugin.id for plugin in result} == {
        "MarketA",
        "MarketB",
    }
    error.assert_not_called()
    assert progress.call_args_list[0].kwargs["value"] == 0
    assert progress.call_args_list[-1].kwargs["value"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("base_index", [None, {}], ids=["base-absent", "base-empty"])
async def test_async_collect_reports_failure_without_candidates(base_index):
    """
    没有候选且存在读取失败时报告市场不可用，已确认缺少基础索引也不能掩盖失败。
    """
    errors = Mock()
    summary = Mock()
    service = _service(
        async_market_loader=AsyncMock(side_effect=[base_index, RuntimeError("timeout"), None]),
        error=errors,
        info=summary,
    )

    with pytest.raises(PluginMarketUnavailableError, match="插件市场索引暂时无法读取"):
        await service.async_collect(
            markets=["https://market-a"],
            compatible_flags=["v3", "v2"],
            force=False,
            require_available=True,
        )

    summary.assert_called_once()
    if base_index is None:
        errors.assert_called_once()
        assert "https://market-a" in errors.call_args.args[0]
    else:
        errors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_market_refresh_summarizes_index_probes_by_repository(mode):
    """
    每轮只汇总一次，回退和空索引无需报错，整个仓库不可用时只报一次。
    """
    v2_repo, v1_repo, empty_repo, broken_repo, missing_repo = (
        f"https://github.com/market-test/{name}"
        for name in ("v2", "v1", "empty", "broken", "missing")
    )
    markets = [v2_repo, v1_repo, empty_repo, broken_repo, missing_repo]
    outcomes = {
        (v2_repo, "v2"): {"Modern": {"version": "1.0.0"}},
        (v2_repo, None): {"Other": {"version": "1.0.0"}},
        (v1_repo, "v3"): TimeoutError("直连超时"),
        (v1_repo, None): {"Legacy": {"version": "1.0.0"}},
        (empty_repo, "v3"): {},
        (broken_repo, "v3"): RuntimeError("响应格式无效"),
        (broken_repo, "v2"): RuntimeError("HTTP 503"),
    }
    warning, error, info = Mock(), Mock(), Mock()

    def load(market: str, generation: str | None, force: bool):
        """
        为每个仓库代际返回索引、确认缺失，或抛出读取异常。
        """
        assert force
        outcome = outcomes.get((market, generation))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    service = _service(
        market_loader=Mock(side_effect=load),
        async_market_loader=AsyncMock(side_effect=load),
        plugin_mapper=lambda plugin_id, metadata, market, _installed, _time, generation: (
            _plugin(plugin_id, metadata["version"], market, generation)
        ),
        warning=warning,
        error=error,
        info=info,
    )
    kwargs = {"markets": markets, "compatible_flags": ["v3", "v2"], "force": True}
    result = await service.async_collect(**kwargs) if mode == "async" else service.collect(**kwargs)

    assert [(plugin.id, plugin.package_version) for plugin in result] == [
        ("Modern", "v2"), ("Other", None), ("Legacy", None),
    ]
    warning.assert_not_called()
    assert error.call_count == 2
    assert broken_repo in error.call_args_list[0].args[0]
    assert "v3 响应格式无效" in error.call_args_list[0].args[0]
    assert "v2 HTTP 503" in error.call_args_list[0].args[0]
    assert missing_repo in error.call_args_list[1].args[0]
    info.assert_called_once()
    summary = info.call_args.args[0]
    assert "共 5 个仓库，3 个可用，2 个无可用索引，2 个回退到较低代际" in summary
    assert "缺少索引 8 次（v3 2 / v2 3 / v1 3）" in summary
    assert "读取失败 3 次（v3 2 / v2 1）" in summary
    assert "获取到 3 个线上插件" in summary


@pytest.mark.asyncio
async def test_single_index_query_still_warns_without_a_refresh_summary():
    """
    单代查询失败继续提示，确认缺失的索引则直接返回空目录。
    """
    warning, info = Mock(), Mock()
    service = _service(
        market_loader=Mock(side_effect=RuntimeError("HTTP 503")),
        async_market_loader=AsyncMock(return_value=None),
        warning=warning,
        info=info,
    )

    assert service.load("https://market-a", "v3") == []
    assert await service.async_load("https://market-a", "v2") == []
    warning.assert_called_once()
    assert "HTTP 503" in warning.call_args.args[0]
    info.assert_not_called()


@pytest.mark.asyncio
async def test_async_collect_keeps_valid_empty_market_results():
    """
    成功读取到的空目录仍是有效结果，不能误报为网络不可用。
    """
    errors = Mock()
    service = _service(
        async_market_loader=AsyncMock(return_value={}),
        error=errors,
    )

    result = await service.async_collect(
        markets=["https://market-empty"],
        compatible_flags=["v3"],
        force=False,
        require_available=True,
    )

    assert result == []
    errors.assert_not_called()


@pytest.mark.asyncio
async def test_async_collect_preserves_each_repository_update_candidate():
    """
    来源准入读取应保留每个仓库的最高版本，不能先按插件 ID 全局去重。
    """
    async def loader(market: str, package_version: str | None, _force: bool):
        """
        按仓库返回不同版本，验证来源保留模式不会提前跨仓去重。
        """
        if package_version is None:
            return None
        version = "2.0.0" if market == "https://market-bound" else "3.0.0"
        return {"Demo": {"version": version}}

    service = _service(
        async_market_loader=loader,
        plugin_mapper=lambda plugin_id, info, market, _installed, _time, generation: (
            _plugin(plugin_id, info["version"], market, generation)
        ),
    )
    result = await service.async_collect(
        markets=["https://market-bound", "https://market-alternative"],
        compatible_flags=["v3"],
        force=False,
        preserve_sources=True,
    )

    assert [(plugin.repo_url, plugin.plugin_version) for plugin in result] == [
        ("https://market-bound", "2.0.0"),
        ("https://market-alternative", "3.0.0"),
    ]
    assert service.merge(result, [], ["https://market-bound", "https://market-alternative"]) == [result[1]]


@pytest.mark.asyncio
async def test_async_collect_cancels_all_loaders_when_parent_is_cancelled():
    """
    请求取消时必须取消并回收全部市场 loader，不能把子任务遗留在事件循环。
    """
    blocker = asyncio.Event()
    all_started = asyncio.Event()
    started = 0
    cancelled = 0

    async def loader(_market: str, _package_version: str | None, _force: bool):
        """
        记录市场 loader 的启动和取消，并等待测试释放。
        """
        nonlocal started, cancelled
        started += 1
        if started == 2:
            all_started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        return {}

    summary = Mock()
    service = _service(async_market_loader=loader, info=summary)
    collect_task = asyncio.create_task(
        service.async_collect(
            markets=["https://market-a", "https://market-b"],
            compatible_flags=[],
            force=True,
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=1)
    collect_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await collect_task
        await asyncio.sleep(0)
        assert cancelled == 2
        summary.assert_not_called()
    finally:
        blocker.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_async_collect_cleans_loaders_when_progress_callback_fails():
    """
    进度回调异常也必须取消并回收尚未完成的市场 loader。
    """
    blocker = asyncio.Event()
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def loader(market: str, _package_version: str | None, _force: bool):
        """
        让一个市场立即完成，另一个保持阻塞以验证异常清理。
        """
        if market == "https://market-a":
            await slow_started.wait()
            return {}
        slow_started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            slow_cancelled.set()
            raise
        return {}

    def progress(*, value: float, **_kwargs) -> None:
        """
        首个市场完成时模拟进度消费者失败。
        """
        if value > 0:
            raise RuntimeError("progress unavailable")

    service = _service(async_market_loader=loader)
    with pytest.raises(RuntimeError, match="progress unavailable"):
        await asyncio.wait_for(
            service.async_collect(
                markets=["https://market-a", "https://market-b"],
                compatible_flags=[],
                force=True,
                progress_callback=progress,
            ),
            timeout=1,
        )
    assert slow_cancelled.is_set()
