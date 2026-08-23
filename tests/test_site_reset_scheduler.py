import threading
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.background import BackgroundTasks

from app.api.endpoints import site as site_endpoint
from app.application.site.mutation import SiteMutationResult


@pytest.mark.asyncio
async def test_reset_submits_cookiecloud_after_site_transaction(monkeypatch):
    """站点重置提交 CookieCloud 后台任务，不在请求事件循环内直接执行。"""
    command = Mock()
    command.reset = AsyncMock(return_value=SiteMutationResult(success=True))
    background_tasks = BackgroundTasks()
    scheduler = Mock()
    loop_thread = threading.get_ident()

    def start_scheduler(**_kwargs):
        assert threading.get_ident() != loop_thread

    scheduler.start.side_effect = start_scheduler
    system_config = Mock()
    system_config.async_set = AsyncMock()

    monkeypatch.setattr(site_endpoint, "Scheduler", Mock(return_value=scheduler))
    monkeypatch.setattr(
        site_endpoint,
        "get_configured_system_config",
        Mock(return_value=system_config),
    )

    response = await site_endpoint.reset(
        background_tasks=background_tasks,
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
    assert len(background_tasks.tasks) == 1

    await background_tasks()

    scheduler.start.assert_called_once_with(job_id="cookiecloud", manual=True)
