from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path

import pytest

from app.schemas.plugin import PluginInstance
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


def test_classification_uses_each_instance_bound_directory() -> None:
    """本体与分身绑定不同版本时，各自状态不能互相覆盖。"""
    host = PluginInstance(
        instance_id="DemoPlugin",
        source_plugin_id="DemoPlugin",
        mode="host",
        pinned_version="1.0.0",
    )
    clone = PluginInstance(
        instance_id="DemoPluginCopy",
        source_plugin_id="DemoPlugin",
        pinned_version="2.0.0",
    )
    paths = {
        "DemoPlugin": Path("/plugins/demo/v1_0_0"),
        "DemoPluginCopy": Path("/plugins/demo/v2_0_0"),
    }
    installer = SimpleNamespace(
        classify_plugins=MagicMock(return_value=([], ["DemoPlugin"], [])),
        classify_plugin_directory=MagicMock(
            side_effect=lambda path: (True, path == paths["DemoPlugin"])
        ),
    )
    service = PluginDependencyService(
        system=lambda: SimpleNamespace(dependency=installer),
        instances=lambda: {clone.instance_id: clone},
        host_instance=lambda _source: host,
        instance_directory=lambda _source, instance: paths[
            instance.instance_id if instance else "DemoPlugin"
        ],
        log=MagicMock(),
    )

    result = service.classify_plugins()

    assert result.ready == ("DemoPlugin",)
    assert result.missing_dependencies == ("DemoPluginCopy",)
    assert result.missing_source == ()
