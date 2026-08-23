from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.runtime.extensions.plugin.dependency import (
    PluginDependencyInstallResult,
    PluginDependencyService,
)


def test_install_missing_skips_installer_when_environment_is_satisfied() -> None:
    """依赖均满足时只执行轻量检查，不进入包安装链。"""
    installer = SimpleNamespace(
        find_missing=MagicMock(return_value=[]),
        install=MagicMock(),
    )
    service = PluginDependencyService(
        system=lambda: SimpleNamespace(dependency=installer),
        log=MagicMock(),
    )

    result = service.install_missing_with_status()

    assert result.success is True
    assert result.missing == []
    installer.install.assert_not_called()


def test_install_missing_preserves_list_return_contract() -> None:
    """旧入口继续返回缺失项列表，供现有调用方按真值判断。"""
    installer = SimpleNamespace(
        find_missing=MagicMock(return_value=["demo>=1"]),
        install=MagicMock(return_value=(True, "")),
    )
    service = PluginDependencyService(
        system=lambda: SimpleNamespace(dependency=installer),
        log=MagicMock(),
    )

    assert service.install_missing() == ["demo>=1"]
    installer.install.assert_called_once_with(["demo>=1"])


@pytest.mark.asyncio
async def test_async_install_missing_uses_async_installer() -> None:
    """异步启动恢复必须调用可取消的依赖安装入口。"""
    installer = SimpleNamespace(
        async_find_missing=AsyncMock(return_value=["demo>=1"]),
        async_install=AsyncMock(return_value=(True, "")),
    )
    service = PluginDependencyService(
        system=lambda: SimpleNamespace(dependency=installer),
        log=MagicMock(),
    )

    result = await service.async_install_missing_with_status()

    assert result == PluginDependencyInstallResult(
        missing=["demo>=1"],
        success=True,
    )
    installer.async_find_missing.assert_awaited_once()
    installer.async_install.assert_awaited_once_with(["demo>=1"])
