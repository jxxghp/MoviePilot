import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.schemas.exception import (
    DatabaseWorkerClosedError,
    DatabaseWorkerOverloadedError,
    PersistenceUnavailableError,
)
from app.application.plugin.install import PluginInstallCommand
from app.runtime.extensions.plugin.admission import PluginMutationAdmission


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
    mutation=None,
    package_write_guard=None,
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
        mutation=mutation or (lambda _operation: nullcontext()),
        package_write_guard=package_write_guard
        or (lambda _plugin_id: nullcontext()),
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
    assert "插件文件已恢复" not in result.message
    assert "Python依赖变更不支持自动回滚" not in result.message
    rollback.assert_awaited_once()
    reporter.assert_not_awaited()
    writer.assert_not_awaited()
    reloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_sealed_install_rejects_before_package_guard_and_checkpoint() -> None:
    """安装事务在封口后不进入监控抑制，也不创建文件快照。"""
    admission = PluginMutationAdmission()
    admission.seal()
    package_guard = Mock(return_value=nullcontext())
    checkpointer = AsyncMock()

    result = await _command(
        mutation=admission.hold,
        package_write_guard=package_guard,
        checkpointer=checkpointer,
    ).execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is False
    assert result.failure_stage == "admission"
    assert "停机阶段" in result.message
    package_guard.assert_not_called()
    checkpointer.assert_not_awaited()


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
async def test_cancelled_existing_plugin_refresh_restores_runtime_and_registrations():
    """已存在插件刷新被取消时，必须重新收敛运行态和注册。"""
    registration_started = asyncio.Event()
    calls: list[str] = []

    async def reload_plugin(_plugin_id: str) -> None:
        calls.append("reload")

    async def refresh_registrations(_plugin_id: str) -> None:
        calls.append("registrations")
        if calls.count("registrations") == 1:
            registration_started.set()
            await asyncio.Event().wait()

    with patch("app.application.plugin.install.logger.warning") as warning:
        task = asyncio.create_task(
            _command(
                installed=["DemoPlugin"],
                plugin_ids=["DemoPlugin"],
                reloader=reload_plugin,
                refresher=refresh_registrations,
            ).execute(
                plugin_id="DemoPlugin",
                repo_url="https://github.com/demo/plugins",
            )
        )
        await registration_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls == ["reload", "registrations", "reload", "registrations"]
    warning.assert_not_called()


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
    assert result.rollback.installed_list_attempted is True
    assert result.rollback.runtime_attempted is False
    rollback.assert_awaited_once_with(checkpoint)
    reloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistence_exception_after_write_restores_installed_list():
    """清单写入已提交后抛异常时，文件和清单必须一起恢复。"""
    persisted: list[list[str]] = []
    checkpoint = object()
    rollback = AsyncMock()

    async def write(plugin_ids: list[str]) -> None:
        persisted.append(list(plugin_ids))
        if len(persisted) == 1:
            raise RuntimeError("write acknowledgement lost")

    result = await _command(
        checkpointer=AsyncMock(return_value=checkpoint),
        writer=write,
        rollback=rollback,
    ).execute(
        plugin_id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
    )

    assert result.success is False
    assert result.failure_stage == "installed_list_persistence"
    assert result.rollback.installed_list_attempted is True
    assert result.rollback.installed_list_restored is True
    assert persisted == [["DemoPlugin"], []]
    rollback.assert_awaited_once_with(checkpoint)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [DatabaseWorkerClosedError, DatabaseWorkerOverloadedError],
)
async def test_persistence_unavailable_rolls_back_and_reaches_api_boundary(
        error_type: type[PersistenceUnavailableError],
) -> None:
    """持久化能力暂不可用时完成补偿并交由 API 映射为 503。"""
    checkpoint = object()
    rollback = AsyncMock()
    command = _command(
        checkpointer=AsyncMock(return_value=checkpoint),
        writer=AsyncMock(side_effect=error_type("persistence unavailable")),
        rollback=rollback,
    )

    with pytest.raises(error_type):
        await command.execute(
            plugin_id="DemoPlugin",
            repo_url="https://github.com/demo/plugins",
        )

    rollback.assert_awaited_once_with(checkpoint)


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
async def test_same_plugin_install_lifecycle_is_serialized() -> None:
    """同一插件的两个安装调用不得同时修改包、运行态和注册信息。"""
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def install(plugin_id, *_args):
        calls.append(plugin_id)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return True, "ok"

    command = _command(installer=install)
    first = asyncio.create_task(
        command.execute(
            plugin_id="DemoPlugin",
            repo_url="https://github.com/demo/plugins",
        )
    )
    await first_started.wait()
    second = asyncio.create_task(
        command.execute(
            plugin_id="DemoPlugin",
            repo_url="https://github.com/demo/plugins",
        )
    )
    await asyncio.sleep(0.02)
    assert calls == ["DemoPlugin"]

    release_first.set()
    results = await asyncio.gather(first, second)
    assert all(result.success for result in results)
    assert calls == ["DemoPlugin", "DemoPlugin"]


@pytest.mark.asyncio
async def test_cancelled_install_waits_for_rollback_before_releasing_lifecycle() -> None:
    """取消安装后先完成包快照补偿，再允许同一插件的新调用进入。"""
    install_started = asyncio.Event()
    release_install = asyncio.Event()
    rollback = AsyncMock()

    async def install(*_args):
        install_started.set()
        await release_install.wait()
        return True, "ok"

    command = _command(installer=install, rollback=rollback)
    task = asyncio.create_task(
        command.execute(
            plugin_id="DemoPlugin",
            repo_url="https://github.com/demo/plugins",
        )
    )
    await install_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_persisted_list_is_restored_conservatively() -> None:
    """清单写入已产生副作用但尚未返回时取消，也必须恢复原清单。"""
    persisted: list[list[str]] = []
    writer_started = asyncio.Event()
    rollback = AsyncMock()

    async def writer(plugin_ids: list[str]) -> None:
        persisted.append(list(plugin_ids))
        if len(persisted) == 1:
            writer_started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        _command(writer=writer, rollback=rollback).execute(
            plugin_id="DemoPlugin",
            repo_url="https://github.com/demo/plugins",
        )
    )
    await writer_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persisted == [["DemoPlugin"], []]
    rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_snapshot_cleanup_does_not_rollback_committed_plugin() -> None:
    """运行态提交后清理快照期间取消，不得删除已生效插件。"""
    cleanup_started = asyncio.Event()
    rollback = AsyncMock()

    async def committer(_checkpoint) -> None:
        cleanup_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        _command(committer=committer, rollback=rollback).execute(
            plugin_id="DemoPlugin",
            repo_url="https://github.com/demo/plugins",
        )
    )
    await cleanup_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_lifecycle_lock_blocks_plugin_install_until_settlement() -> None:
    """启动同步持有全局资格时，插件安装不得穿过启动收口。"""
    from app.application.plugin.lifecycle import plugin_lifecycle

    entered = asyncio.Event()
    release = asyncio.Event()

    async def startup_scope():
        async with plugin_lifecycle.hold_startup():
            entered.set()
            await release.wait()

    startup = asyncio.create_task(startup_scope())
    await entered.wait()
    plugin_context = plugin_lifecycle.hold("DemoPlugin")
    plugin_scope = asyncio.create_task(plugin_context.__aenter__())
    await asyncio.sleep(0.02)
    assert plugin_scope.done() is False

    release.set()
    await plugin_scope
    await plugin_context.__aexit__(None, None, None)
    await startup


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
