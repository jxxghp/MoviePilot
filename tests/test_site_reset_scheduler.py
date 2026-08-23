from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints import site as site_endpoint
from app.application.site.mutation import SiteMutationResult
from app.runtime.tasks import TaskRegistry


class _TaskRegistry(TaskRegistry):
    """记录站点重置提交的同步后台任务。"""

    def __init__(self) -> None:
        """初始化任务记录。"""
        super().__init__()
        self.calls: list[tuple] = []

    def create_sync(self, function, *args, owner: str, **kwargs) -> None:
        """保存函数、参数和 owner，避免端点测试启动真实任务。"""
        self.calls.append((function, args, kwargs, owner))


@pytest.mark.asyncio
async def test_reset_submits_cookiecloud_after_site_transaction(monkeypatch):
    """站点重置提交 CookieCloud 后台任务，不在请求事件循环内直接执行。"""
    command = Mock()
    command.reset = AsyncMock(return_value=SiteMutationResult(success=True))
    task_registry = _TaskRegistry()
    scheduler = Mock()
    system_config = Mock()
    system_config.async_set = AsyncMock()

    monkeypatch.setattr(site_endpoint, "Scheduler", Mock(return_value=scheduler))
    monkeypatch.setattr(
        site_endpoint,
        "get_configured_system_config",
        Mock(return_value=system_config),
    )

    response = await site_endpoint.reset(
        task_registry=task_registry,
        command=command,
        _=Mock(),
    )

    assert response.success is True
    command.reset.assert_awaited_once_with()
    scheduler.start.assert_not_called()
    system_config.async_set.assert_any_await(
        site_endpoint.SystemConfigKey.IndexerSites, []
    )
    system_config.async_set.assert_any_await(site_endpoint.SystemConfigKey.RssSites, [])
    assert task_registry.calls == [
        (
            scheduler.start,
            (),
            {"job_id": "cookiecloud", "manual": True},
            "api.site.reset",
        )
    ]
