"""系统管理应用服务的用例分支与端口委托测试。"""

from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.configuration import SystemConfigWriteResult
from app.application.system import (
    LogFileData,
    MarketFetchResult,
    SystemService,
)
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.system import SystemUpdateStatus
from app.schemas.types import SystemConfigKey


def _build_service() -> tuple[SystemService, SimpleNamespace]:
    """构造全部外部端口可观测的系统应用服务。"""
    settings = Mock()
    settings.get.return_value = None
    system_config = Mock()
    system_config.normalize_value.side_effect = lambda _key, value: value
    system_config.async_set = AsyncMock(return_value=True)
    system_config.async_set_with_normalized_value = AsyncMock(
        side_effect=lambda _key, value: SystemConfigWriteResult(
            changed=True,
            normalized_value=value,
        )
    )
    logs = Mock()
    logs.read = AsyncMock(return_value="first\nsecond")
    logs.follow = AsyncMock(return_value=iter(("tail",)))
    logs.collect = AsyncMock(return_value=[LogFileData("app.log", b"log")])
    market = Mock()
    market.fetch = AsyncMock()
    events = Mock()
    events.publish = AsyncMock()
    server = Mock()
    server.user_global = AsyncMock(return_value={"level": 2})
    server.usage = AsyncMock(return_value={"count": 3})
    releases = Mock()
    releases.list = AsyncMock(return_value=[{"tag_name": "v3.1.0"}])
    features = Mock()
    features.snapshot.return_value = {"rust": True}
    control = Mock()
    control.can_restart.return_value = True
    control.restart.return_value = (True, "restarting")
    control.upgrade_dev.return_value = (True, "upgrading")
    updates = Mock()
    updates.status.return_value = SystemUpdateStatus(
        state="idle", current_version="v3.0.0"
    )
    updates.check.return_value = SystemUpdateStatus(
        state="available", current_version="v3.0.0", version="v3.1.0"
    )
    updates.download.return_value = SystemUpdateStatus(
        state="downloading", current_version="v3.0.0"
    )
    updates.prepare_install.return_value = (True, "prepared")
    llm = Mock()
    llm.validate.return_value = None
    mutation = AsyncMock()

    @asynccontextmanager
    async def rule_group_mutation():
        """提供规则组原子变更端口的异步上下文。"""
        yield mutation

    dependencies = SimpleNamespace(
        settings=settings,
        system_config=system_config,
        logs=logs,
        market=market,
        events=events,
        server=server,
        releases=releases,
        features=features,
        control=control,
        updates=updates,
        llm=llm,
        mutation=mutation,
    )
    service = SystemService(
        settings=settings,
        system_config=system_config,
        logs=logs,
        market=market,
        events=events,
        server=server,
        releases=releases,
        features=features,
        control=control,
        updates=updates,
        llm=llm,
        plugin_mutation=lambda _key: nullcontext(),
        rule_group_mutation=rule_group_mutation,
    )
    return service, dependencies


@pytest.mark.anyio
async def test_system_service_delegates_read_only_queries() -> None:
    """日志、服务端、Release 和运行能力查询只委托对应端口。"""
    service, dependencies = _build_service()
    disconnected = AsyncMock(return_value=False)

    assert await service.read_log("app.log") == "second\nfirst"
    assert list(await service.follow_log("app.log", 20, disconnected)) == ["tail"]
    assert await service.collect_logs("app") == [LogFileData("app.log", b"log")]
    assert await service.user_global() == {"level": 2}
    assert await service.usage() == {"count": 3}
    assert await service.releases() == [{"tag_name": "v3.1.0"}]
    assert service.runtime_features() == {"rust": True}
    assert service.update_status().state == "idle"
    assert service.check_update().state == "available"
    dependencies.logs.follow.assert_awaited_once_with("app.log", 20, disconnected)


@pytest.mark.anyio
async def test_update_environment_rejects_validation_and_partial_failure() -> None:
    """批量设置必须在校验失败或部分写入失败时返回结构化失败。"""
    service, dependencies = _build_service()
    dependencies.llm.validate.return_value = "LLM 工具不可用"

    rejected = await service.update_environment({"LLM_SERVER_TOOLS": True})
    assert rejected.success is False
    assert rejected.message == "LLM 工具不可用"
    dependencies.settings.update_many.assert_not_called()

    dependencies.llm.validate.return_value = None
    dependencies.settings.update_many.return_value = {
        "PORT": (True, "ok"),
        "HOST": (False, "invalid host"),
    }
    failed = await service.update_environment({"PORT": 3001, "HOST": ""})
    assert failed.success is False
    assert failed.message == "invalid host"
    assert failed.data["success_updates"] == {"PORT": (True, "ok")}
    dependencies.events.publish.assert_not_awaited()


@pytest.mark.anyio
async def test_update_environment_publishes_successful_batch() -> None:
    """全部设置写入成功后只发布一次成功键集合。"""
    service, dependencies = _build_service()
    dependencies.settings.update_many.return_value = {"PORT": (True, "ok")}

    result = await service.update_environment({"PORT": 3001})

    assert result.success is True
    assert result.data == {"success_updates": {"PORT": (True, "ok")}}
    published_keys = dependencies.events.publish.await_args.args[0]
    assert list(published_keys) == ["PORT"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "fetched", "message"),
    [
        ("http://example.com/plugin.md", None, "不支持的 Wiki 同步地址"),
        (None, MarketFetchResult(None, []), "无法访问 Wiki 插件仓库清单"),
        (None, MarketFetchResult(503, []), "状态码：503"),
        (None, MarketFetchResult(200, []), "未在 Wiki 中识别到插件仓库地址"),
    ],
)
async def test_sync_plugin_market_rejects_invalid_sources(
    url: str | None, fetched: MarketFetchResult | None, message: str
) -> None:
    """Wiki 同步拒绝非受信地址、传输失败和空清单。"""
    service, dependencies = _build_service()
    if fetched is not None:
        dependencies.market.fetch.return_value = fetched

    result = await service.sync_plugin_market(url)

    assert result.success is False
    assert message in str(result.message)


@pytest.mark.anyio
async def test_sync_plugin_market_merges_case_insensitive_repositories() -> None:
    """市场仓库按来源顺序去重，持久化成功后发布规范化值。"""
    service, dependencies = _build_service()
    dependencies.settings.get.return_value = "https://github.com/A/repo/, local/repo"
    dependencies.settings.update.return_value = (True, "saved")
    dependencies.market.fetch.return_value = MarketFetchResult(
        200, ["https://github.com/a/repo", "https://github.com/B/repo/"]
    )

    result = await service.sync_plugin_market(None)

    assert result.success is True
    assert result.data["repos"] == [
        "https://github.com/A/repo",
        "local/repo",
        "https://github.com/B/repo",
    ]
    assert result.data["added_count"] == 1
    dependencies.events.publish.assert_awaited_once_with(
        "PLUGIN_MARKET",
        "https://github.com/A/repo,local/repo,https://github.com/B/repo",
    )


@pytest.mark.anyio
async def test_update_setting_routes_runtime_and_persistent_values() -> None:
    """运行设置走 settings，系统配置走持久化端口并过滤空列表项。"""
    service, dependencies = _build_service()
    dependencies.settings.contains.side_effect = lambda key: key == "PORT"
    dependencies.settings.update.return_value = (None, "unchanged")

    runtime_result = await service.update_setting("PORT", 3001)
    unknown_result = await service.update_setting("UNKNOWN_KEY", True)
    persistent_result = await service.update_setting(
        SystemConfigKey.IndexerSites.value, [1, None, 2]
    )

    assert runtime_result.success is True
    assert unknown_result.success is False
    assert persistent_result.success is True
    dependencies.system_config.async_set_with_normalized_value.assert_awaited_once_with(
        SystemConfigKey.IndexerSites.value, [1, 2]
    )
    dependencies.events.publish.assert_awaited_once_with(
        SystemConfigKey.IndexerSites.value, [1, 2]
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("persisted_result", "expected_success", "publishes_event"),
    [(True, True, True), (None, True, False), (False, False, False)],
)
async def test_update_setting_preserves_persistent_write_result(
    persisted_result: bool | None,
    expected_success: bool,
    publishes_event: bool,
) -> None:
    """通用系统配置写入只把明确 False 映射为失败并抑制失败事件。"""
    service, dependencies = _build_service()
    dependencies.settings.contains.return_value = False
    dependencies.system_config.async_set_with_normalized_value.side_effect = None
    dependencies.system_config.async_set_with_normalized_value.return_value = (
        SystemConfigWriteResult(
            changed=persisted_result,
            normalized_value=[1, 2],
        )
    )

    result = await service.update_setting(
        SystemConfigKey.IndexerSites.value, [1, None, 2]
    )

    assert result.success is expected_success
    if publishes_event:
        dependencies.events.publish.assert_awaited_once_with(
            SystemConfigKey.IndexerSites.value, [1, 2]
        )
    else:
        dependencies.events.publish.assert_not_awaited()


@pytest.mark.anyio
async def test_update_setting_rejects_invalid_normalized_value() -> None:
    """旧设置入口必须把目录规范化错误作为可见失败返回且不写库。"""
    service, dependencies = _build_service()
    dependencies.settings.contains.return_value = False
    dependencies.system_config.async_set_with_normalized_value.side_effect = ValueError(
        "分类 ID 已失效"
    )

    result = await service.update_setting(SystemConfigKey.Directories.value, [{}])

    assert result.success is False
    assert result.message == "分类 ID 已失效"
    dependencies.system_config.async_set_with_normalized_value.assert_awaited_once()
    dependencies.events.publish.assert_not_awaited()


@pytest.mark.anyio
async def test_update_setting_applies_rule_groups_and_reports_mutation_rejection() -> None:
    """规则组使用原子 mutation，插件写入拒绝应转换为稳定结果。"""
    service, dependencies = _build_service()
    dependencies.settings.contains.return_value = False
    key = SystemConfigKey.UserFilterRuleGroups.value
    dependencies.system_config.get.return_value = [{"name": "old"}, "ignored"]

    result = await service.update_setting(key, [{"name": "new"}, None])

    assert result.success is True
    dependencies.mutation.apply.assert_awaited_once_with(
        [{"name": "new"}], expected_rule_groups=[{"name": "old"}]
    )
    dependencies.events.publish.assert_awaited_once_with(key, [{"name": "new"}])

    service._plugin_mutation = Mock(
        side_effect=PluginMutationRejectedError("plugin busy")
    )
    rejected = await service.update_setting(SystemConfigKey.IndexerSites.value, [1])
    assert rejected.success is False
    assert rejected.message is not None
    assert "plugin busy" in rejected.message


def test_system_control_operations_cover_supported_and_rejected_paths() -> None:
    """重启与开发更新必须同时校验模式和受管运行环境。"""
    service, dependencies = _build_service()

    assert service.restart().success is True
    assert service.upgrade("stable").success is False
    assert service.upgrade("dev").success is True

    dependencies.control.can_restart.return_value = False
    assert service.restart().success is False
    assert service.upgrade("dev").success is False
    assert service.download_update().success is False
    assert service.install_update().success is False


def test_update_download_and_install_report_state_machine_results() -> None:
    """下载与安装应映射状态机失败，并在重启失败时撤销安装请求。"""
    service, dependencies = _build_service()
    dependencies.updates.download.return_value = SystemUpdateStatus(
        state="failed", current_version="v3.0.0", error="download failed"
    )
    assert service.download_update().success is False

    dependencies.updates.prepare_install.return_value = (False, "not ready")
    assert service.install_update().message == "not ready"

    dependencies.updates.prepare_install.return_value = (True, "prepared")
    dependencies.control.restart.return_value = (False, "restart failed")
    failed = service.install_update()
    assert failed.success is False
    dependencies.updates.cancel_install.assert_called_once_with("restart failed")

    dependencies.control.restart.return_value = (True, "restarting")
    installed = service.install_update()
    assert installed.success is True
    assert installed.message == "prepared"


def test_system_service_normalizes_repository_and_rule_group_inputs() -> None:
    """仓库与规则组辅助函数必须复制有效输入并保持稳定去重顺序。"""
    assert SystemService._split_repos(" A/,a,B " ) == ["A", "B"]
    assert SystemService._merge_repos(["A"], ["a/", "B/"]) == ["A", "B"]
    assert SystemService._dict_list(None) == []
    source = {"name": "rule"}
    copied = SystemService._dict_list([source, None])
    assert copied == [source]
    assert copied[0] is not source
