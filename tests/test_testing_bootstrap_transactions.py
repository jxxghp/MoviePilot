"""共享测试引导的数据库事务能力回归测试。"""

import pytest
from sqlalchemy import text

from app.application import service
from app.db import uow
from app.schemas.types import SystemConfigKey
from app.testing.bootstrap import prepare_backend


def test_prepare_backend_configures_sync_transaction_runner(monkeypatch):
    """插件仓只调用共享引导时，无 Session Oper 仍应获得隔离事务执行器。"""
    monkeypatch.setattr(uow, "_sync_transaction_runner", None)

    prepare_backend()

    result = uow.run_sync_transaction(
        lambda session: session.execute(text("SELECT 1")).scalar_one()
    )
    assert result == 1


@pytest.mark.asyncio
async def test_prepare_backend_configures_async_transaction_runner(monkeypatch):
    """共享引导同时提供异步短事务，保持与生产组合根的装配契约一致。"""
    monkeypatch.setattr(uow, "_async_transaction_runner", None)

    prepare_backend()

    async def select_one(session):
        """在共享引导创建的隔离异步会话中执行最小查询。"""
        return (await session.execute(text("SELECT 1"))).scalar_one()

    assert await uow.run_async_transaction(select_one) == 1


def test_prepare_backend_configures_empty_service_directory(monkeypatch):
    """未启动真实模块时，插件通知查询应得到确定性的空服务配置。"""
    monkeypatch.setattr(service, "_config_loader", service._unconfigured_configs)
    monkeypatch.setattr(service, "_module_loader", service._unconfigured_modules)

    prepare_backend()

    assert service.get_service_configs(SystemConfigKey.Notifications, object) == []
