"""配置快照启动顺序测试。"""

from unittest.mock import AsyncMock

import pytest

from app.startup import modules_initializer
from app.startup.lifecycle import initialize_modules_component


class _InlineWorker:
    """按提交顺序执行配置加载操作。"""

    async def run(self, operation):
        """执行并返回操作结果。"""
        return operation()


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
