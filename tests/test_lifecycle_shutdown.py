import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app.startup import lifecycle


def test_lifespan_closes_logger_when_early_shutdown_step_fails(monkeypatch):
    """前置关闭步骤失败时仍应关闭 Logger"""
    monkeypatch.setattr(lifecycle.settings, "MOVIEPILOT_SAFE_MODE", False)
    monkeypatch.setattr(lifecycle.global_vars, "set_loop", MagicMock())
    for name in (
        "init_routers",
        "init_modules",
        "init_plugins",
        "init_scheduler",
        "init_monitor",
        "init_command",
        "init_workflow",
        "stop_workflow",
        "stop_command",
        "stop_monitor",
        "stop_scheduler",
        "stop_plugins",
    ):
        monkeypatch.setattr(lifecycle, name, MagicMock())

    system_chain = MagicMock()
    system_chain.backup_plugins.side_effect = RuntimeError("backup failed")
    monkeypatch.setattr(lifecycle, "SystemChain", MagicMock(return_value=system_chain))
    monkeypatch.setattr(lifecycle, "init_extra", AsyncMock())
    monkeypatch.setattr(lifecycle, "stop_modules", AsyncMock())
    monkeypatch.setattr(lifecycle, "aclose_shared_async_transports", AsyncMock())
    logger_shutdown = MagicMock()
    monkeypatch.setattr(lifecycle.LoggerManager, "shutdown", logger_shutdown)

    async def run_lifespan():
        with pytest.raises(RuntimeError, match="backup failed"):
            async with lifecycle.lifespan(FastAPI()):
                pass

    asyncio.run(run_lifespan())

    logger_shutdown.assert_called_once_with()
