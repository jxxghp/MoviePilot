"""用户配置 API 的异步应用端口测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.endpoints import user as user_endpoint


@pytest.mark.asyncio
async def test_set_config_waits_for_async_configuration_write(monkeypatch) -> None:
    """更新接口等待用户配置事务完成后再返回成功。"""
    service = MagicMock()
    service.async_set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        user_endpoint,
        "get_configured_user_configuration",
        lambda: service,
    )

    response = await user_endpoint.set_config(
        "theme",
        "dark",
        current_user=SimpleNamespace(name="alice"),
    )

    assert response.success is True
    service.async_set.assert_awaited_once_with(
        username="alice",
        key="theme",
        value="dark",
    )


@pytest.mark.asyncio
async def test_get_config_reads_loaded_snapshot(monkeypatch) -> None:
    """查询接口直接读取已加载的用户配置快照。"""
    service = MagicMock()
    service.get.return_value = "dark"
    monkeypatch.setattr(
        user_endpoint,
        "get_configured_user_configuration",
        lambda: service,
    )

    response = await user_endpoint.get_config(
        "theme",
        current_user=SimpleNamespace(name="alice"),
    )

    assert response.success is True
    assert response.data == {"value": "dark"}
    service.get.assert_called_once_with(username="alice", key="theme")
