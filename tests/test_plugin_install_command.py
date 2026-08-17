from unittest.mock import AsyncMock, Mock

import pytest

from app.application.plugin.install import PluginInstallCommand


def _command(
    *,
    installed=None,
    plugin_ids=None,
    compatibility=None,
    installer=None,
    reporter=None,
    writer=None,
    reloader=None,
    refresher=None,
    checkpointer=None,
    committer=None,
    rollback=None,
):
    """构造可观测每一步副作用的插件安装命令。"""
    return PluginInstallCommand(
        installed_plugins_reader=Mock(return_value=installed or []),
        installed_plugins_writer=writer or AsyncMock(),
        plugin_ids_provider=Mock(return_value=plugin_ids or []),
        compatibility_checker=compatibility or AsyncMock(return_value=None),
        package_installer=installer or AsyncMock(return_value=(True, "ok")),
        package_checkpointer=checkpointer or AsyncMock(return_value=object()),
        package_committer=committer or AsyncMock(),
        package_rollback=rollback or AsyncMock(),
        install_reporter=reporter or AsyncMock(),
        plugin_reloader=reloader or AsyncMock(),
        registration_refresher=refresher or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_install_failure_stops_before_report_persistence_and_reload():
    """包安装失败后恢复文件快照，且不得写配置、刷新或上报。"""
    reporter = AsyncMock()
    writer = AsyncMock()
    reloader = AsyncMock()
    rollback = AsyncMock()
    command = _command(
        installer=AsyncMock(return_value=(False, "download failed")),
        reporter=reporter,
        writer=writer,
        reloader=reloader,
        rollback=rollback,
    )

    result = await command.execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is False
    assert result.package_installed is False
    assert result.failure_stage == "package_install"
    assert result.rollback.file_restored is True
    assert result.rollback.dependency_supported is False
    rollback.assert_awaited_once()
    reporter.assert_not_awaited()
    writer.assert_not_awaited()
    reloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_records_completed_install_stages_in_order():
    """成功安装在提交文件快照后再执行非关键远程上报。"""
    calls = []

    async def install(*_args):
        calls.append("package")
        return True, "installed"

    checkpoint = object()

    async def create_checkpoint(_plugin_id):
        calls.append("checkpoint")
        return checkpoint

    async def commit(target):
        assert target is checkpoint
        calls.append("commit")

    async def report(*_args):
        calls.append("report")

    async def write(_plugins):
        calls.append("persist")

    async def reload(_plugin_id):
        calls.append("reload")

    async def refresh(_plugin_id):
        calls.append("registrations")

    result = await _command(
        installer=install,
        reporter=report,
        writer=write,
        reloader=reload,
        refresher=refresh,
        checkpointer=create_checkpoint,
        committer=commit,
    ).execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is True
    assert result.package_installed is True
    assert result.installed_list_persisted is True
    assert result.runtime_reloaded is True
    assert result.registrations_refreshed is True
    assert result.reported is True
    assert calls == [
        "checkpoint",
        "package",
        "persist",
        "reload",
        "registrations",
        "commit",
        "report",
    ]


@pytest.mark.asyncio
async def test_existing_plugin_checks_compatibility_without_reinstalling_package():
    """已存在插件只校验兼容性、上报和重载，不重复安装包。"""
    installer = AsyncMock()
    checkpointer = AsyncMock()
    command = _command(
        installed=["DemoPlugin"],
        plugin_ids=["DemoPlugin"],
        installer=installer,
        checkpointer=checkpointer,
    )

    result = await command.execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is True
    assert result.refreshed_only is True
    assert result.package_installed is False
    assert result.installed_list_persisted is False
    installer.assert_not_awaited()
    checkpointer.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistence_failure_restores_package_without_touching_runtime():
    """已安装列表保存失败时恢复文件，且运行态尚未开始切换。"""
    checkpoint = object()
    rollback = AsyncMock()
    reloader = AsyncMock()
    command = _command(
        checkpointer=AsyncMock(return_value=checkpoint),
        writer=AsyncMock(side_effect=RuntimeError("db unavailable")),
        rollback=rollback,
        reloader=reloader,
    )

    result = await command.execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is False
    assert result.failure_stage == "installed_list_persistence"
    assert result.rollback.file_restored is True
    assert result.rollback.installed_list_attempted is False
    assert result.rollback.runtime_attempted is False
    rollback.assert_awaited_once_with(checkpoint)
    reloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_failure_restores_list_files_and_previous_runtime():
    """重载失败时依次恢复已安装列表、包文件和旧运行态。"""
    calls = []
    checkpoint = object()
    reload_count = 0

    async def write(plugin_ids):
        calls.append(("persist", list(plugin_ids)))

    async def rollback(target):
        assert target is checkpoint
        calls.append(("rollback", target))

    async def reload(_plugin_id):
        nonlocal reload_count
        reload_count += 1
        calls.append(("reload", reload_count))
        if reload_count == 1:
            raise RuntimeError("route registration failed")

    async def refresh(_plugin_id):
        calls.append(("registrations", reload_count))

    result = await _command(
        installed=[],
        checkpointer=AsyncMock(return_value=checkpoint),
        writer=write,
        rollback=rollback,
        reloader=reload,
        refresher=refresh,
    ).execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is False
    assert result.failure_stage == "runtime_reload"
    assert result.rollback.file_restored is True
    assert result.rollback.installed_list_restored is True
    assert result.rollback.runtime_restored is True
    assert result.rollback.registrations_restored is True
    assert calls == [
        ("persist", ["DemoPlugin"]),
        ("reload", 1),
        ("persist", []),
        ("rollback", checkpoint),
        ("reload", 2),
        ("registrations", 2),
    ]


@pytest.mark.asyncio
async def test_registration_failure_restores_instance_files_and_routes() -> None:
    """动态路由刷新失败时恢复列表、文件、旧实例并再次刷新旧注册。"""
    calls = []
    checkpoint = object()
    refresh_count = 0

    async def write(plugin_ids):
        calls.append(("persist", list(plugin_ids)))

    async def rollback(target):
        assert target is checkpoint
        calls.append(("rollback", target))

    async def reload(_plugin_id):
        calls.append("reload")

    async def refresh(_plugin_id):
        nonlocal refresh_count
        refresh_count += 1
        calls.append(("registrations", refresh_count))
        if refresh_count == 1:
            raise RuntimeError("route registration failed")

    result = await _command(
        checkpointer=AsyncMock(return_value=checkpoint),
        writer=write,
        rollback=rollback,
        reloader=reload,
        refresher=refresh,
    ).execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is False
    assert result.failure_stage == "registration_refresh"
    assert result.rollback.file_restored is True
    assert result.rollback.installed_list_restored is True
    assert result.rollback.runtime_restored is True
    assert result.rollback.registrations_restored is True
    assert calls == [
        ("persist", ["DemoPlugin"]),
        "reload",
        ("registrations", 1),
        ("persist", []),
        ("rollback", checkpoint),
        "reload",
        ("registrations", 2),
    ]


@pytest.mark.asyncio
async def test_report_failure_does_not_rollback_completed_local_install():
    """统计上报失败属于非关键副作用，不得撤销已成功的本地安装。"""
    rollback = AsyncMock()
    result = await _command(
        reporter=AsyncMock(side_effect=RuntimeError("server unavailable")),
        rollback=rollback,
    ).execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is True
    assert result.runtime_reloaded is True
    assert result.reported is False
    assert result.report_error == "server unavailable"
    assert "不影响本地安装" in result.message
    rollback.assert_not_awaited()
