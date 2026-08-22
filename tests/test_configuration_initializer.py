"""配置快照启动顺序测试。"""

from unittest.mock import AsyncMock

import pytest

from app.startup import modules_initializer
from app.startup.lifecycle import initialize_modules_component
from app.application.configuration import configure_runtime_settings
from app.runtime.settings import RuntimeSettingsCompat


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


def test_runtime_settings_compat_uses_legacy_settings_from_startup_root(monkeypatch) -> None:
    """组合根装配的兼容代理应读写原始部署配置而不是自身。"""
    legacy_settings = _MutableSettings()
    monkeypatch.setattr(modules_initializer, "legacy_settings", legacy_settings)

    service = modules_initializer._build_runtime_settings_service()
    configure_runtime_settings(service)
    compat = RuntimeSettingsCompat()

    assert compat.model_dump(include={"VALUE"}) == {"VALUE": "before"}
    assert compat.update_setting("VALUE", "after") == (True, "")
    assert compat.model_dump(include={"VALUE"}) == {"VALUE": "after"}


@pytest.mark.asyncio
async def test_configuration_services_publish_after_both_snapshots_load(
    monkeypatch,
) -> None:
    """两个完整快照加载成功前不发布任一配置服务。"""
    events = []

    class _SystemConfig:
        def load_snapshot(self):
            events.append("load-system")

    class _UserConfig:
        def load_snapshot(self):
            events.append("load-user")

    monkeypatch.setattr(modules_initializer, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(modules_initializer, "UserConfigOper", _UserConfig)
    monkeypatch.setattr(
        modules_initializer,
        "configure_system_config",
        lambda _service: events.append("publish-system"),
    )
    monkeypatch.setattr(
        modules_initializer,
        "configure_user_configuration",
        lambda _service: events.append("publish-user"),
    )

    await modules_initializer._initialize_configuration_services(_InlineWorker())

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
        def load_snapshot(self):
            return None

    class _UserConfig:
        def load_snapshot(self):
            raise RuntimeError("load failed")

    monkeypatch.setattr(modules_initializer, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(modules_initializer, "UserConfigOper", _UserConfig)
    monkeypatch.setattr(
        modules_initializer,
        "configure_system_config",
        lambda service: published.append(service),
    )
    monkeypatch.setattr(
        modules_initializer,
        "configure_user_configuration",
        lambda service: published.append(service),
    )

    with pytest.raises(RuntimeError, match="load failed"):
        await modules_initializer._initialize_configuration_services(_InlineWorker())

    assert published == []


@pytest.mark.asyncio
async def test_modules_startup_failure_stops_database_worker(monkeypatch) -> None:
    """模块启动失败时立即关闭已创建的数据库 worker。"""
    monkeypatch.setattr(
        "app.startup.lifecycle.init_modules",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    )
    stop_worker = AsyncMock()
    monkeypatch.setattr(modules_initializer, "stop_database_worker", stop_worker)

    with pytest.raises(RuntimeError, match="startup failed"):
        await initialize_modules_component(object())

    stop_worker.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_modules_startup_failure_preserves_original_error_when_cleanup_fails(
        monkeypatch,
) -> None:
    """数据库任务清理失败时仍向上层保留原始启动异常。"""
    monkeypatch.setattr(
        "app.startup.lifecycle.init_modules",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    )
    monkeypatch.setattr(
        modules_initializer,
        "stop_database_worker",
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        await initialize_modules_component(object())


@pytest.mark.asyncio
async def test_database_worker_owner_is_retained_when_shutdown_fails(monkeypatch) -> None:
    """数据库 worker 关闭失败时保留 owner，允许后续重试或诊断。"""

    class _FailingWorker:
        async def shutdown(self):
            raise RuntimeError("shutdown failed")

    worker = _FailingWorker()
    monkeypatch.setattr(modules_initializer, "_database_worker", worker)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await modules_initializer.stop_database_worker()

    assert modules_initializer._database_worker is worker
