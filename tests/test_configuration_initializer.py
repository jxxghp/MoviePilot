"""配置快照启动顺序测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.configuration import configure_runtime_settings
from app.startup.composition import configuration as configuration_composition
from app.startup.composition import database as database_composition
from app.startup.initializers import modules as modules_initializer


class _InlineWorker:
    """按提交顺序执行配置加载操作。"""

    async def run(self, operation):
        """执行并返回操作结果。"""
        return operation()


class _MutableSettings:
    """提供运行时配置代理回归测试所需的最小可变设置实现。"""

    def __init__(self) -> None:
        """初始化一项可读写的部署设置。"""
        self.VALUE = "before"

    def model_dump(self, *, include=None, exclude=None):
        """导出测试设置快照。"""
        values = {"VALUE": self.VALUE}
        if include is not None:
            values = {key: value for key, value in values.items() if key in include}
        if exclude is not None:
            values = {key: value for key, value in values.items() if key not in exclude}
        return values

    def update_settings(self, env):
        """批量更新测试设置。"""
        for key, value in env.items():
            setattr(self, key, value)
        return {key: (True, "") for key in env}

    def update_setting(self, key, value):
        """更新单项测试设置。"""
        setattr(self, key, value)
        return True, ""


def _isolate_startup_failure_cleanup(monkeypatch) -> None:
    """隔离启动失败回滚中的非目标进程 owner，避免关闭其他用例共享资源。"""
    absent_owner = SimpleNamespace(get_existing_instance=lambda: None)
    for name in (
        "ModuleManager",
        "EventManager",
        "ThreadHelper",
        "RedisHelper",
        "AsyncRedisHelper",
    ):
        monkeypatch.setattr(modules_initializer, name, absent_owner)
    monkeypatch.setattr(
        modules_initializer,
        "stop_doh_composition",
        MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        modules_initializer,
        "close_image_proxy_block_log_coalescer",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(modules_initializer, "close_browser_sessions", MagicMock())
    monkeypatch.setattr(
        modules_initializer,
        "stop_managed_resources",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        modules_initializer,
        "shutdown_web_agent_background_tasks",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        modules_initializer,
        "get_configured_agent_chat_persistence",
        lambda: None,
    )
    monkeypatch.setattr(modules_initializer, "stop_frontend", MagicMock())
    monkeypatch.setattr(modules_initializer, "clear_temp", MagicMock())


@pytest.mark.asyncio
async def test_runtime_settings_service_uses_legacy_settings_from_startup_root(
    monkeypatch,
) -> None:
    """组合根装配的设置服务应直接读写唯一部署配置对象。"""
    legacy_settings = _MutableSettings()

    class _SystemConfig:
        """提供无需数据库的系统配置快照桩。"""

        def load_snapshot(self):
            """模拟系统配置快照加载。"""

        def publish_many(self, _values):
            """模拟事务提交后的系统配置快照发布。"""

    class _UserConfig:
        """提供无需数据库的用户配置快照桩。"""

        def load_snapshot(self):
            """模拟用户配置快照加载。"""

    monkeypatch.setattr(configuration_composition, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(
        configuration_composition,
        "TransactionalUserConfigurationRepository",
        lambda _session_factory: _UserConfig(),
    )
    monkeypatch.setattr(configuration_composition, "configure_system_config", lambda _service: None)
    monkeypatch.setattr(configuration_composition, "configure_user_configuration", lambda _service: None)

    composition = await configuration_composition.compose_configuration(
        executor=_InlineWorker(),
        settings=legacy_settings,
    )
    service = composition.settings
    configure_runtime_settings(service)

    assert service.snapshot(include={"VALUE"}) == {"VALUE": "before"}
    assert service.update("VALUE", "after") == (True, "")
    assert service.snapshot(include={"VALUE"}) == {"VALUE": "after"}


@pytest.mark.asyncio
async def test_configuration_services_publish_after_both_snapshots_load(
    monkeypatch,
) -> None:
    """两个完整快照加载成功前不发布任一配置服务。"""
    events = []

    class _SystemConfig:
        """记录系统配置快照加载顺序。"""

        def load_snapshot(self):
            """登记系统快照已加载。"""
            events.append("load-system")

        def publish_many(self, _values):
            """模拟事务提交后的系统配置快照发布。"""

    class _UserConfig:
        """记录用户配置快照加载顺序。"""

        def load_snapshot(self):
            """登记用户快照已加载。"""
            events.append("load-user")

    monkeypatch.setattr(configuration_composition, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(
        configuration_composition,
        "TransactionalUserConfigurationRepository",
        lambda _session_factory: _UserConfig(),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_system_config",
        lambda _service: events.append("publish-system"),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_user_configuration",
        lambda _service: events.append("publish-user"),
    )

    composition = await configuration_composition.compose_configuration(
        executor=_InlineWorker(),
        settings=_MutableSettings(),
    )

    assert events == ["load-system", "load-user"]

    configuration_composition.publish_configuration(
        composition,
        _MutableSettings(),
    )

    assert events == [
        "load-system",
        "load-user",
        "publish-system",
        "publish-user",
    ]


@pytest.mark.asyncio
async def test_configuration_load_failure_does_not_publish_partial_service(
    monkeypatch,
) -> None:
    """任一快照加载失败时不发布半套配置服务。"""
    published = []

    class _SystemConfig:
        """提供成功的系统配置快照加载桩。"""

        def load_snapshot(self):
            """模拟系统配置加载成功。"""
            return None

    class _UserConfig:
        """提供失败的用户配置快照加载桩。"""

        def load_snapshot(self):
            """模拟用户配置加载失败。"""
            raise RuntimeError("load failed")

    monkeypatch.setattr(configuration_composition, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(
        configuration_composition,
        "TransactionalUserConfigurationRepository",
        lambda _session_factory: _UserConfig(),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_system_config",
        lambda service: published.append(service),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_user_configuration",
        lambda service: published.append(service),
    )

    with pytest.raises(RuntimeError, match="load failed"):
        await configuration_composition.compose_configuration(
            executor=_InlineWorker(),
            settings=_MutableSettings(),
        )

    assert published == []


def test_publish_configuration_reuses_composed_runtime_and_settings(monkeypatch) -> None:
    """正式运行时与兼容入口必须共享同一组配置对象。"""
    events = {}
    system_service = object()
    user_service = object()
    runtime = object()
    settings_service = SimpleNamespace(update=lambda key, value: (key, value))
    legacy_settings = SimpleNamespace(VALUE="configured")
    composition = SimpleNamespace(
        system_service=system_service,
        user_service=user_service,
        runtime=runtime,
        settings=settings_service,
    )

    monkeypatch.setattr(
        configuration_composition,
        "configure_system_config",
        lambda value: events.update(system=value),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_user_configuration",
        lambda value: events.update(user=value),
    )

    monkeypatch.setattr(
        configuration_composition,
        "configure_runtime_configuration",
        lambda value: events.update(runtime=value),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_runtime_settings",
        lambda value: events.update(settings=value),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_runtime_setting_provider",
        lambda value: events.update(provider=value),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_runtime_setting_updater",
        lambda value: events.update(updater=value),
    )
    monkeypatch.setattr(
        configuration_composition,
        "configure_token_runtime_config",
        lambda value: events.update(token=value),
    )

    configuration_composition.publish_configuration(composition, legacy_settings)

    assert events["system"] is system_service
    assert events["user"] is user_service
    assert events["runtime"] is runtime
    assert events["settings"] is settings_service
    assert events["provider"]("VALUE") == "configured"
    assert events["updater"] is settings_service.update
    assert callable(events["token"])


@pytest.mark.asyncio
async def test_modules_startup_failure_stops_database_runtime(monkeypatch) -> None:
    """首次启动失败时关闭 worker、撤销 provider 并释放数据库引擎。"""
    _isolate_startup_failure_cleanup(monkeypatch)
    monkeypatch.setattr(
        modules_initializer,
        "_initialize_modules",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    )
    monkeypatch.setattr(modules_initializer, "stop_message", lambda: True)
    stop_worker = AsyncMock()
    monkeypatch.setattr(modules_initializer, "stop_database_runtime", stop_worker)
    monkeypatch.setattr(modules_initializer, "database_runtime_active", lambda: False)
    reset_database = MagicMock()
    reset_configuration = MagicMock()
    close_database = AsyncMock()
    monkeypatch.setattr(modules_initializer, "reset_database_services", reset_database)
    monkeypatch.setattr(modules_initializer, "reset_configuration", reset_configuration)
    monkeypatch.setattr(modules_initializer, "close_database", close_database)

    with pytest.raises(RuntimeError, match="startup failed"):
        await modules_initializer.init_modules()

    stop_worker.assert_awaited_once_with()
    reset_database.assert_called_once_with()
    reset_configuration.assert_called_once_with()
    close_database.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_modules_startup_failure_preserves_original_error_when_cleanup_fails(
    monkeypatch,
) -> None:
    """数据库任务清理失败时仍向上层保留原始启动异常。"""
    _isolate_startup_failure_cleanup(monkeypatch)
    startup_error = RuntimeError("startup failed")
    monkeypatch.setattr(
        modules_initializer,
        "_initialize_modules",
        AsyncMock(side_effect=startup_error),
    )
    monkeypatch.setattr(modules_initializer, "stop_message", lambda: True)
    monkeypatch.setattr(
        modules_initializer,
        "stop_database_runtime",
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    monkeypatch.setattr(modules_initializer, "database_runtime_active", lambda: True)
    reset_database = MagicMock()
    reset_configuration = MagicMock()
    close_database = AsyncMock()
    monkeypatch.setattr(modules_initializer, "reset_database_services", reset_database)
    monkeypatch.setattr(modules_initializer, "reset_configuration", reset_configuration)
    monkeypatch.setattr(modules_initializer, "close_database", close_database)

    with pytest.raises(RuntimeError) as raised:
        await modules_initializer.init_modules()

    assert raised.value is startup_error
    reset_database.assert_not_called()
    reset_configuration.assert_not_called()
    close_database.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_worker_owner_is_retained_when_shutdown_fails(monkeypatch) -> None:
    """数据库 worker 关闭失败时保留 owner，允许后续重试或诊断。"""

    class _FailingWorker:
        """模拟无法完成关闭的数据库 worker。"""

        async def shutdown(self):
            """抛出关闭错误以验证 owner 保留。"""
            raise RuntimeError("shutdown failed")

    worker = _FailingWorker()
    runtime = SimpleNamespace(worker=worker)
    monkeypatch.setattr(database_composition, "_database_runtime", runtime)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await database_composition.stop_database_runtime()

    assert database_composition._database_runtime is runtime
    with pytest.raises(RuntimeError, match="已由当前进程持有"):
        await database_composition.start_database_runtime()


@pytest.mark.asyncio
async def test_database_runtime_rejects_parallel_start_and_allows_next_lifespan(
    monkeypatch,
) -> None:
    """数据库 runtime 在启动中拒绝重入，成功关闭后才允许新 lifespan。"""
    started = asyncio.Event()
    release = asyncio.Event()
    created = []

    class _Worker:
        """阻塞首次启动以验证并行重入保护。"""

        def __init__(self) -> None:
            """记录构造出的唯一 worker。"""
            self.stopped = False
            created.append(self)

        async def start(self) -> None:
            """等待测试允许后完成启动。"""
            started.set()
            await release.wait()

        async def shutdown(self) -> None:
            """记录 worker 已完成关闭。"""
            self.stopped = True

    monkeypatch.setattr(database_composition, "DatabaseWorker", _Worker)
    monkeypatch.setattr(database_composition, "_database_runtime", None)
    monkeypatch.setattr(database_composition, "_database_runtime_starting", False)
    monkeypatch.setattr(
        database_composition,
        "configure_transaction_runners",
        lambda **_kwargs: None,
    )

    first_start = asyncio.create_task(database_composition.start_database_runtime())
    await started.wait()
    with pytest.raises(RuntimeError, match="已由当前进程持有"):
        await database_composition.start_database_runtime()
    release.set()
    first_runtime = await first_start

    with pytest.raises(RuntimeError, match="已由当前进程持有"):
        await database_composition.start_database_runtime()
    await database_composition.stop_database_runtime()
    assert first_runtime.worker.stopped is True

    started.clear()
    release.clear()
    next_start = asyncio.create_task(database_composition.start_database_runtime())
    await started.wait()
    release.set()
    second_runtime = await next_start
    assert second_runtime is not first_runtime
    assert len(created) == 2
    await database_composition.stop_database_runtime()


@pytest.mark.asyncio
async def test_database_runtime_start_failure_does_not_publish_transaction_runners(
    monkeypatch,
) -> None:
    """Worker 启动失败时不得留下可调用的无会话事务入口。"""
    published = []

    class _FailingWorker:
        """模拟线程池准备阶段失败的数据库 worker。"""

        async def start(self) -> None:
            """在 owner 发布前抛出启动错误。"""
            raise RuntimeError("worker start failed")

    monkeypatch.setattr(database_composition, "DatabaseWorker", _FailingWorker)
    monkeypatch.setattr(database_composition, "_database_runtime", None)
    monkeypatch.setattr(database_composition, "_database_runtime_starting", False)
    monkeypatch.setattr(
        database_composition,
        "configure_transaction_runners",
        lambda **kwargs: published.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="worker start failed"):
        await database_composition.start_database_runtime()

    assert published == []
    assert database_composition.database_runtime_active() is False
    assert database_composition._database_runtime_starting is False


@pytest.mark.asyncio
async def test_database_service_reset_revokes_transaction_runners() -> None:
    """数据库服务撤销后同步和异步无会话写入口都必须恢复未装配错误。"""
    from app.db.uow import (
        configure_transaction_runners,
        run_async_transaction,
        run_sync_transaction,
    )

    async def async_runner(_operation):
        """返回固定值以证明 reset 前异步 runner 已发布。"""
        return "async"

    configure_transaction_runners(
        sync=lambda _operation: "sync",
        async_=async_runner,
    )
    assert run_sync_transaction(lambda _session: None) == "sync"
    assert await run_async_transaction(lambda _session: None) == "async"

    database_composition.reset_database_services()

    with pytest.raises(RuntimeError, match="同步事务执行器尚未配置"):
        run_sync_transaction(lambda _session: None)
    with pytest.raises(RuntimeError, match="异步事务执行器尚未配置"):
        await run_async_transaction(lambda _session: None)


@pytest.mark.asyncio
async def test_startup_failure_revokes_published_database_services(monkeypatch) -> None:
    """后续启动阶段失败并关闭 worker 后不得留下已失效的全局服务。"""
    _isolate_startup_failure_cleanup(monkeypatch)
    from app.application.configuration import get_configured_system_config
    from app.application.plugin.transaction import get_plugin_persistence
    from app.application.query import get_configured_data_query_service
    from app.application.security.userconfig import get_configured_user_configuration
    from app.application.workflow import get_configured_workflow_query

    configuration = SimpleNamespace(
        system_service=object(),
        user_service=object(),
        runtime=object(),
        settings=SimpleNamespace(update=lambda _key, _value: (True, "")),
    )
    database = SimpleNamespace(
        data_query=object(),
        plugin_persistence=object(),
        workflow_query=object(),
    )
    settings = SimpleNamespace(VALUE="configured")
    configuration_composition.publish_configuration(configuration, settings)
    database_composition.publish_database_services(database)
    startup_error = RuntimeError("post-publication failure")
    monkeypatch.setattr(
        modules_initializer,
        "_initialize_modules",
        AsyncMock(side_effect=startup_error),
    )
    monkeypatch.setattr(modules_initializer, "stop_message", lambda: True)
    monkeypatch.setattr(modules_initializer, "stop_database_runtime", AsyncMock())
    monkeypatch.setattr(modules_initializer, "database_runtime_active", lambda: False)

    with pytest.raises(RuntimeError) as raised:
        await modules_initializer.init_modules()

    assert raised.value is startup_error
    for getter in (
        get_configured_system_config,
        get_configured_user_configuration,
        get_configured_data_query_service,
        get_plugin_persistence,
        get_configured_workflow_query,
    ):
        with pytest.raises(RuntimeError):
            getter()
